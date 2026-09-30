"""Content-addressed immutable price pairs. Collection happens outside DB locks."""

import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import PriceSnapshot, ServerJob

PRICED_COMMANDS = {"buy_packs", "sell_spares", "sell_bulk", "pull_oripa", "refresh_oripa"}


def resources(executable: str) -> Path:
    binary = Path(executable).resolve()
    candidates = [
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
    if cards.get("krwPerUSD") != previous["cardPrices"].get("krwPerUSD"):
        raise ValueError("Automatic refresh must preserve the game's currency conversion")


def encode(payload: dict) -> tuple[str, str]:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(data.encode()) > 25_000_000:
        raise ValueError("Price snapshot too large")
    return hashlib.sha256(data.encode()).hexdigest(), data


@lru_cache(maxsize=4)
def bundled_version(executable: str) -> str:
    return encode(bundled(executable))[0]


@lru_cache(maxsize=2)
def decode_snapshot(data: str) -> dict:
    return json.loads(data)


def current(db: Session, rules) -> tuple[str | None, dict | None]:
    job = db.get(ServerJob, "prices")
    if job and job.active_snapshot:
        row = db.get(PriceSnapshot, job.active_snapshot)
        return row.id, decode_snapshot(row.data)
    # Pure test rules have no executable/resources; production always has them.
    if not getattr(rules, "executable", None):
        return None, None
    payload = bundled(rules.executable)
    return bundled_version(rules.executable), payload


def apply(rules, state, command, payload=None, protected=None):
    if getattr(rules, "supports_context", False):
        return rules.apply(state, command, prices=payload, protected=protected)
    return rules.apply(state, command)


def check_version(kind: str, supplied: str | None, actual: str | None):
    if actual and kind in PRICED_COMMANDS and supplied != actual:
        raise HTTPException(409, "price_version_changed")
