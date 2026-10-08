"""Trainer levels and the rotation market must match the app's Swift rules exactly."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import native_levels as levels
from app import native_rewards as rewards
from app import native_rotation as rotation
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


def test_rotation_lineup_matches_the_app(real_context):
    # RotationMarketTests / the app's lineup for this date with the same catalogue.
    assert rotation.seed("2026-10-08") == 8232385724070197664
    assert rotation.lineup("2026-10-08", real_context) == [
        "swsh5-181",
        "sv6-173",
        "swsh45sv-SV072",
        "sv2-231",
        "ex12-89",
        "xy6-5",
        "hgss3-7",
        "sm11-60",
    ]
    assert rotation.date_key(1_791_502_200) == "2026-10-08"


def test_rotation_price_beats_the_best_sale(real_context):
    from app.native_economy import default_finish, tokens, usd

    for card_id in rotation.lineup("2026-10-08", real_context):
        finish = default_finish(card_id, real_context)
        sale = tokens(usd(card_id, real_context, finish), real_context)
        assert rotation.price(card_id, real_context) > sale * 1.15


def test_rotation_buy_sells_each_card_once(real_context):
    date = rotation.date_key(real_context.now)
    card_id = rotation.lineup(date, real_context)[-1]
    cost = rotation.price(card_id, real_context)
    state = {
        "cards": {},
        "printingCards": {},
        "cardFirstAt": {},
        "claimedDex": [],
        "usedSinceInstall": cost * 2,
        "spentTokens": 0,
        "refundedTokens": 0,
        "perkTokens": 0,
        "marketEarnedTokens": 0,
        "marketSpentTokens": 0,
        "rotationPurchases": ["2000-01-01|old-card"],
    }
    command = {"kind": "rotation_buy", "card_id": card_id, "date": date}
    result = rotation.buy(state, command, real_context)
    assert result["rotation"]["id"] == card_id
    assert state["marketSpentTokens"] == cost
    assert state["cards"][card_id] == 1
    assert state["rotationPurchases"] == [f"{date}|{card_id}"]
    with pytest.raises(HTTPException) as again:
        rotation.buy(state, command, real_context)
    assert again.value.detail == "rotation_changed"
    with pytest.raises(HTTPException) as stale:
        rotation.buy(state, {**command, "date": "2000-01-01"}, real_context)
    assert stale.value.detail == "rotation_changed"


def test_new_commands_pass_the_strict_schema():
    from uuid import uuid4

    from app.game_schemas import CommandRequest

    base = {"request_id": str(uuid4()), "expected_revision": 0}
    assert CommandRequest(**base, command={"kind": "claim_levels"}).command.kind == "claim_levels"
    buy = CommandRequest(
        **base, command={"kind": "rotation_buy", "card_id": "sv1-1", "date": "2026-10-08"}
    )
    assert buy.command.date == "2026-10-08"
    with pytest.raises(ValueError):
        CommandRequest(**base, command={"kind": "rotation_buy", "card_id": "sv1-1", "date": "x"})
    prefs = CommandRequest(
        **base, command={"kind": "set_preferences", "opening_mode": "game", "level_title": 10}
    )
    assert prefs.command.level_title == 10
