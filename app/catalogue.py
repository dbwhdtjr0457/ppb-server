import json
import subprocess
from functools import lru_cache

from fastapi import HTTPException

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


def describe(key, rules):
    card, _, finish = key.rpartition("#")
    return {
        **cards(rules).get(card, {"id": card, "name": card, "name_ko": card}),
        "printing": key,
        "finish": finish,
    }
