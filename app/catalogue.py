import json
import subprocess
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException

from app.observability import logger

FINISHES = {
    "normal",
    "holo",
    "reverseHolo",
    "patternedReverse",
    "fullArt",
    "etched",
    "radiant",
    "amazingRare",
    "rainbow",
    "gold",
    "shiny",
    "shinyFullArt",
    "aceSpec",
    "pokeBall",
    "masterBall",
    "celebrationsClassic",
    "radiantCollection",
    "prism",
    "breakFoil",
    "blackWhite",
    "megaAttack",
}


@lru_cache(maxsize=4)
def load(executable):
    # Swift owns subset parents and corrected rarities, not a Python copy.
    raw = subprocess.check_output([executable, "--export-online-catalogue"], timeout=60)
    return {entry["id"]: entry for entry in json.loads(raw)}


def cards(rules):
    return rules.catalogue if hasattr(rules, "catalogue") else load(rules.executable)


def validate_printing(key, rules):
    card, _, finish = key.rpartition("#")
    if card not in cards(rules) or finish not in FINISHES:
        raise HTTPException(422, "unknown_printing")


@lru_cache(maxsize=4)
def korean_names(directory: str) -> dict[str, str]:
    """Card ID -> Korean name, shipped beside the Python rules data.

    The Swift catalogue export carried `name_ko`; the Python rules data does not.
    A missing file only costs Korean search, so fall back instead of failing.
    """
    path = Path(directory) / "card-names-ko.json"
    try:
        names = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("card-names-ko.json unavailable in %s; using English card names", directory)
        return {}
    return names if isinstance(names, dict) else {}


def describe(key, rules):
    card, _, finish = key.rpartition("#")
    entry = cards(rules).get(card) or {"id": card, "name": card}
    name = entry.get("name") or card
    directory = getattr(rules, "resources_dir", None)
    names = korean_names(str(directory)) if directory else {}
    name_ko = entry.get("name_ko") or names.get(card)
    return {**entry, "name": name, "name_ko": name_ko or name, "printing": key, "finish": finish}
