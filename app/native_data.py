"""Validated, process-lifetime immutable public rules catalogue.

The Swift application is an optional *build-time* oracle only. Request handling
reads this exported catalogue once and never launches a native executable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any

DEFAULT_RESOURCE_DIR = Path(__file__).resolve().parents[1] / "data"
MAX_CATALOGUE_BYTES = 25_000_000
_load_lock = RLock()


def freeze(value: Any) -> Any:
    """Keep shared price-independent data safe from accidental request mutation."""
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Make JSON-safe owned output when a static reward enters mutable state."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def load(resources_dir: str | Path | None = None) -> Mapping[str, Any]:
    """Load one release directory once; failed validation is never cached.

    A deployment uses an immutable release directory. Updating catalogue data
    requires a new release/restart, not mutation of an active request's inputs.
    The lock prevents duplicate first-load IO under simultaneous requests.
    """
    directory = Path(resources_dir or DEFAULT_RESOURCE_DIR).expanduser().resolve()
    file = directory if directory.is_file() else directory / "native-rules.json"
    with _load_lock:
        return _load(file)


@lru_cache(maxsize=4)
def _load(file: Path) -> Mapping[str, Any]:
    if not 0 < file.stat().st_size <= MAX_CATALOGUE_BYTES:
        raise ValueError("Native rules catalogue has an invalid size")
    with file.open("rb") as source:
        manifest = json.load(source)
    validate(manifest)
    return freeze(manifest)


def validate(data: Any) -> None:
    """Reject partial/stale-shape exports before the server accepts commands."""
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported native rules schema")
    for field in ("rules_version", "opening_rules_version", "catalogue_digest"):
        if not isinstance(data.get(field), str) or not data[field]:
            raise ValueError(f"Missing native rules {field}")
    cards, sets, dexes = data.get("cards"), data.get("sets"), data.get("dexes")
    if not all(isinstance(rows, list) and rows for rows in (cards, sets, dexes)):
        raise ValueError("Native rules require nonempty cards, sets and dexes")
    try:
        card_ids = {card["id"] for card in cards}
        set_ids = {row["id"] for row in sets}
        tiers = set(data["tiers"])
        finishes = set(data["finishes"])
        pack_rules = data["pack_rules"]
        if len(card_ids) != len(cards) or len(set_ids) != len(sets):
            raise ValueError("Duplicate native catalogue identity")
        if set(pack_rules) != set_ids:
            raise ValueError("Native pack rules must cover every set")
        if set(data["tier_ranks"]) != tiers or set(data["fallback_chains"]) != tiers:
            raise ValueError("Incomplete tier rules")
        for card in cards:
            if card["set_id"] not in set_ids or card["tier"] not in tiers:
                raise ValueError("Unknown native card set or tier")
            if card["default_finish"] not in finishes:
                raise ValueError("Unknown native card finish")
            if not set(card["finish_by_hint"].values()) <= finishes:
                raise ValueError("Invalid native finish resolution")
        for rule in pack_rules.values():
            if (
                not rule["slots"]
                or sum(slot["count"] for slot in rule["slots"])
                != rule["contents"]["game_card_count"]
            ):
                raise ValueError("Native slot count does not match physical pack")
            for slot in rule["slots"]:
                if not slot["weights"] or any(
                    entry["tier"] not in tiers
                    or not isinstance(entry["weight"], int)
                    or entry["weight"] <= 0
                    for entry in slot["weights"]
                ):
                    raise ValueError("Invalid native slot weights")
                if any(card_id not in card_ids for ids in slot["pool"].values() for card_id in ids):
                    raise ValueError("Native slot references an unknown card")
            if any(card_id not in card_ids for ids in rule["pool"].values() for card_id in ids):
                raise ValueError("Native pool references an unknown card")
    except (KeyError, TypeError) as error:
        raise ValueError("Incomplete native rules catalogue") from error
