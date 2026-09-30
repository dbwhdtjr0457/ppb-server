import json
from pathlib import Path

import pytest

from app.native_data import DEFAULT_RESOURCE_DIR, load, thaw, validate


def test_export_has_normalized_subsets_finishes_and_every_pack():
    data = load()
    cards = {card["id"]: card for card in data["cards"]}
    assert len(cards) > 18_000
    assert len(data["sets"]) == len(data["pack_rules"])
    assert cards["sv8pt5-74"]["finish_by_hint"]["masterBallParallel"] == "masterBall"
    assert cards["cel25c-4_A"]["set_id"] == "cel25"
    assert cards["cel25c-4_A"]["default_finish"] == "celebrationsClassic"
    assert data["pack_rules"]["sv3pt5"]["special_rules"][0]["one_in"] == 1300
    assert data["pack_rules"]["base1"]["contents"]["game_card_count"] == 11


def test_load_reuses_one_deeply_immutable_snapshot():
    data = load()
    assert load(DEFAULT_RESOURCE_DIR) is data
    with pytest.raises(TypeError):
        data["rules_version"] = "changed"
    with pytest.raises(TypeError):
        data["cards"][0]["tier"] = "changed"
    with pytest.raises(AttributeError):
        data["cards"].append({})
    owned = thaw(data["cards"][0])
    owned["tier"] = "changed"
    assert data["cards"][0]["tier"] != "changed"


def test_validation_rejects_partial_rule_export():
    data = thaw(load())
    data["pack_rules"].pop("base1")
    with pytest.raises(ValueError, match="every set"):
        validate(data)


def test_validation_rejects_unknown_card_in_slot():
    data = thaw(load())
    data["pack_rules"]["base1"]["slots"][0]["pool"]["C"].append("unknown-1")
    with pytest.raises(ValueError, match="unknown card"):
        validate(data)


def test_failed_load_is_not_cached(tmp_path: Path):
    file = tmp_path / "native-rules.json"
    file.write_text("{}")
    with pytest.raises(ValueError, match="schema"):
        load(tmp_path)
    file.write_bytes((DEFAULT_RESOURCE_DIR / file.name).read_bytes())
    assert load(tmp_path)["rules_version"] == load()["rules_version"]


def test_oracle_covers_every_set_and_both_opening_modes():
    oracle = json.loads((DEFAULT_RESOURCE_DIR / "native-rules-oracle.json").read_bytes())
    expected = {(item["id"], mode) for item in load()["sets"] for mode in ("game", "realistic")}
    assert {(item["set_id"], item["mode"]) for item in oracle["openings"]} == expected
    assert {item["set_id"] for item in oracle["prices"]} == {item["id"] for item in load()["sets"]}
