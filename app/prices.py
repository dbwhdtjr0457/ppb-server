"""Content-addressed immutable price pairs. Collection happens outside DB locks."""

import hashlib
import json
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from weakref import WeakKeyDictionary

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PriceSnapshot, ServerJob

PRICED_COMMANDS = {
    "buy_packs",
    "sell_spares",
    "sell_bulk",
    "pull_oripa",
    "refresh_oripa",
    "rotation_buy",
}


def resources(executable: str) -> Path:
    binary = Path(executable).resolve()
    candidates = [
        binary,
        binary.parent / "PokePackBar_PokePackBar.bundle",
        binary.parent.parent / "Resources/PokePackBar_PokePackBar.bundle",
    ]
    for path in candidates:
        if (path / "card-prices.json").is_file():
            return path
    raise ValueError("Matching rules resources unavailable")


@lru_cache(maxsize=4)
def bundled(executable: str) -> dict:
    root = resources(executable)
    return {
        "schemaVersion": 1,
        "cardPrices": json.loads((root / "card-prices.json").read_text()),
        "packPrices": json.loads((root / "pack-prices.json").read_text()),
    }


def currency_conversion(cards: dict):
    # Bundled Swift JSON uses krwPerUsd; accept the historical test/import alias
    # without silently treating two missing values as a valid exchange rate.
    values = [cards[key] for key in ("krwPerUsd", "krwPerUSD") if key in cards]
    if not values or any(
        type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        for value in values
    ):
        raise ValueError("Invalid currency conversion")
    if any(value != values[0] for value in values[1:]):
        raise ValueError("Conflicting currency conversion fields")
    return values[0]


def validate(payload: dict, previous: dict):
    if payload.get("schemaVersion") != 1:
        raise ValueError("Invalid price schema")
    cards, packs = payload["cardPrices"], payload["packPrices"]
    if cards.get("currency") != "USD" or packs.get("currency") != "USD":
        raise ValueError("USD price data required")
    for field in ["prices", "printingPrices"]:
        prices = cards[field]
        if not set(previous["cardPrices"][field]).issubset(prices):
            raise ValueError("Incomplete card price coverage")
        if any(
            type(v) not in (int, float) or not math.isfinite(v) or not 0 < v < 10_000_000
            for v in prices.values()
        ):
            raise ValueError("Invalid card price")
    if not set(previous["packPrices"]["packs"]).issubset(packs["packs"]):
        raise ValueError("Incomplete pack price coverage")
    for entry in packs["packs"].values():
        value = entry["usd"]
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 < value < 10_000_000
        ):
            raise ValueError("Invalid pack price")
    if currency_conversion(cards) != currency_conversion(previous["cardPrices"]):
        raise ValueError("Automatic refresh must preserve the game's currency conversion")


def encode(payload: dict) -> tuple[str, str]:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(data.encode()) > 25_000_000:
        raise ValueError("Price snapshot too large")
    return hashlib.sha256(data.encode()).hexdigest(), data


@lru_cache(maxsize=4)
def bundled_version(executable: str) -> str:
    return bundled_snapshot(executable).version


@lru_cache(maxsize=2)
def decode_snapshot(data: str) -> dict:
    return json.loads(data)


@dataclass(frozen=True)
class Snapshot:
    """Published versions never change; consumers must not mutate the shared payload."""

    version: str
    payload: dict
    data: str


_snapshots = WeakKeyDictionary()
_snapshot_lock = threading.RLock()


@lru_cache(maxsize=4)
def bundled_snapshot(source: str) -> Snapshot:
    payload = bundled(source)
    version, data = encode(payload)
    return Snapshot(version, payload, data)


def resource_source(rules):
    return getattr(rules, "resources_dir", None) or getattr(rules, "executable", None)


def current_version(db: Session, rules) -> str | None:
    # Read only the small active-version pointer, not the multi-megabyte price row.
    active = db.scalar(select(ServerJob.active_snapshot).where(ServerJob.name == "prices"))
    if active:
        return active
    source = resource_source(rules)
    return bundled_version(str(source)) if source else None


def current_snapshot(db: Session, rules) -> Snapshot | None:
    active = db.scalar(select(ServerJob.active_snapshot).where(ServerJob.name == "prices"))
    if active:
        # Isolate databases (including in-memory test engines), bound versions, and
        # serialize only cold loads. Stable payload identity also permits compiled
        # native price indexes to be cached once per immutable version.
        bind = db.get_bind()
        engine = getattr(bind, "engine", bind)
        with _snapshot_lock:
            cache = _snapshots.setdefault(engine, OrderedDict())
            if active not in cache:
                data = db.scalar(select(PriceSnapshot.data).where(PriceSnapshot.id == active))
                if data is None:
                    raise HTTPException(503, "prices_unavailable")
                cache[active] = Snapshot(active, json.loads(data), data)
                if len(cache) > 4:
                    cache.popitem(last=False)
            cache.move_to_end(active)
            return cache[active]
    # Pure test rules have no executable/resources; production always has them.
    source = resource_source(rules)
    return bundled_snapshot(str(source)) if source else None


def current(db: Session, rules) -> tuple[str | None, dict | None]:
    snapshot = current_snapshot(db, rules)
    return (snapshot.version, snapshot.payload) if snapshot else (None, None)


def current_encoded(db: Session, rules) -> tuple[str | None, str | None]:
    snapshot = current_snapshot(db, rules)
    return (snapshot.version, snapshot.data) if snapshot else (None, None)


def apply(rules, state, command, payload=None, protected=None):
    if getattr(rules, "supports_context", False):
        return rules.apply(state, command, prices=payload, protected=protected)
    return rules.apply(state, command)


def check_version(kind: str, supplied: str | None, actual: str | None):
    if actual and kind in PRICED_COMMANDS and supplied != actual:
        raise HTTPException(409, "price_version_changed")
