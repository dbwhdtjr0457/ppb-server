"""Trainer levels must match the app's Swift rules; the rotation lineup is server-drawn."""

import random
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import native_levels as levels
from app import native_rewards as rewards
from app import native_rotation as rotation
from app.native_economy import sale_unit
from app.native_rules import PythonRules


def test_level_curve_matches_the_app():
    assert levels.packs_required(1) == 0
    assert levels.packs_required(2) == 4
    assert levels.packs_required(10) == 180
    assert levels.packs_required(100) == 19_800
    assert levels.level_for(0) == 1
    assert levels.level_for(3) == 1
    assert levels.level_for(4) == 2
    assert levels.level_for(179) == 9
    assert levels.level_for(180) == 10
    assert levels.level_for(10**9) == levels.MAX_LEVEL


def test_level_rewards_match_the_app():
    # Same values as LevelRulesTests in the app.
    assert levels.reward(2) == {"level": 2, "setID": "sv3", "packs": 1, "coupons": 0}
    assert levels.reward(5) == {"level": 5, "setID": "me2", "packs": 1, "coupons": 2}
    assert levels.reward(13) == {"level": 13, "setID": "sv8", "packs": 2, "coupons": 0}
    assert levels.reward(50)["packs"] == 5


def context():
    return SimpleNamespace(sets_by_id={set_id: {} for set_id in levels.REWARD_SET_IDS})


def test_claim_levels_grants_once():
    state = {"packsOpened": levels.packs_required(5), "packs": {}, "coupons": []}
    result = levels.claim_levels(state, {"kind": "claim_levels"}, context())
    assert result == {"levels": [2, 3, 4, 5]}
    assert sum(state["packs"].values()) == 4
    assert state["coupons"] == [{"setID": "me2", "value": 0.5, "left": 2}]
    with pytest.raises(HTTPException):
        levels.claim_levels(state, {"kind": "claim_levels"}, context())


def test_level_title_needs_the_level():
    ctx = SimpleNamespace(data={"dexes": [], "ladder": []})
    state = {"packsOpened": levels.packs_required(10), "cards": {}, "claimedDex": []}
    command = {"kind": "set_preferences", "opening_mode": "game", "level_title": 10}
    rewards.set_preferences(state, command, ctx)
    assert state["levelTitle"] == 10
    with pytest.raises(HTTPException):
        rewards.set_preferences(state, {**command, "level_title": 25}, ctx)
    with pytest.raises(HTTPException):
        rewards.set_preferences(state, {**command, "title": 10}, ctx)


@pytest.fixture(scope="module")
def real_context():
    return PythonRules(clock=lambda: 1_791_500_000.0, seed_source=lambda: 42)._context(None)


def test_rotation_lineup_follows_the_tier_plan(real_context):
    picked = rotation.lineup(real_context, random.Random(7))
    tiers = [real_context.cards_by_id[card_id]["tier"] for card_id in picked]
    assert len(picked) == len(set(picked)) == 8
    assert tiers[0] in rotation.TOP
    assert all(tier in rotation.MIDDLE for tier in tiers[1:4])
    assert all(tier in rotation.LOW for tier in tiers[4:])
    # Drawn by the server, not derived from the date: another draw differs.
    assert rotation.lineup(real_context, random.Random(8)) != picked


def test_rotation_price_beats_the_best_sale(real_context):
    for printing, unit in rotation.offers(real_context, random.Random(3)):
        card_id, finish = printing.rsplit("#", 1)
        # Buying here and selling straight back to the game never pays.
        assert unit >= sale_unit(card_id, finish, real_context, 0.15)


def test_new_commands_pass_the_strict_schema():
    from uuid import uuid4

    from app.game_schemas import CommandRequest

    base = {"request_id": str(uuid4()), "expected_revision": 0}
    assert CommandRequest(**base, command={"kind": "claim_levels"}).command.kind == "claim_levels"
    # The shop rotation became server listings on the online market.
    with pytest.raises(ValueError):
        CommandRequest(
            **base, command={"kind": "rotation_buy", "card_id": "sv1-1", "date": "2026-10-08"}
        )
    prefs = CommandRequest(
        **base, command={"kind": "set_preferences", "opening_mode": "game", "level_title": 10}
    )
    assert prefs.command.level_title == 10
