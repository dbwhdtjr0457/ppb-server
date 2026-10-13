"""Frozen server Oripa vectors for the full catalogue-price-range policy.

Generated once from the reviewed e30ed06 policy, never recomputed as expected
values during a test. Keep the historical Swift export separate. These checks
run in ordinary pytest, including hosts without a Swift executable.
"""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.native_opening import SplitMix64, make_oripa_box
from app.native_rules import PythonRules

FIXTURE = json.loads((Path(__file__).parent / "fixtures/oripa-full-range.json").read_text())


@pytest.fixture(scope="module")
def rules():
    rules = PythonRules()
    assert FIXTURE["policy"] == "catalogue-price-range-v1"
    assert FIXTURE["catalogue_digest"] == rules.data["catalogue_digest"]
    prices = (rules.resources_dir / "card-prices.json").read_bytes()
    assert FIXTURE["card_prices_sha256"] == hashlib.sha256(prices).hexdigest()
    cases = FIXTURE["cases"]
    assert len(cases) == 4
    assert {(row["seed"], row["owned_all"]) for row in cases} == {
        ("42", False),
        ("42", True),
        ("2026", False),
        ("2026", True),
    }
    return rules


@pytest.mark.parametrize(
    "row",
    FIXTURE["cases"],
    ids=lambda row: f"seed-{row['seed']}-owned-all-{row['owned_all']}",
)
def test_seeded_oripa_boxes_match_current_server_policy(rules, row):
    before = {
        "cards": dict.fromkeys(rules.catalogue, 1) if row["owned_all"] else {},
        "oripa": {"serial": row["serial"] - 1},
    }
    original = deepcopy(before)
    actual = make_oripa_box(before, rules._context(None), rng=SplitMix64(int(row["seed"])))
    assert actual == row["box"]
    assert len(actual["cards"]) == len(set(actual["cards"])) == 40
    assert all(card in rules.catalogue for card in actual["cards"])
    assert actual["opened"] == [] and actual["serial"] == row["serial"]
    assert before == original
