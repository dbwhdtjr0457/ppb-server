from copy import deepcopy
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi import HTTPException

from app import native_rewards as rewards


def dex(identifier="theme", **changes):
    return {
        "id": identifier,
        "kind": "theme",
        "name": {"ko": identifier, "en": identifier},
        "blurb": {"ko": "", "en": ""},
        "homeSet": "a",
        "cards": ["a-1"],
        "tier": 1,
        "medianPacks": 0,
        "medianTokens": 0,
        "valueUSD": 0,
        "reward": {"packs": 0, "tokens": 0, "perks": [], "coupons": []},
        "milestones": [],
        **changes,
    }


def context(dexes=(), ladder=(), cards=None, sets=None):
    return SimpleNamespace(
        data={
            "dexes": dexes,
            "ladder": ladder,
            "cards": cards
            or [
                {"id": "a-1", "set_id": "a", "tier": "C"},
                {"id": "a-2", "set_id": "a", "tier": "UR"},
                {"id": "a-3", "set_id": "a", "tier": "UR"},
            ],
            "sets": sets if sets is not None else [{"id": "a"}, {"id": "b"}],
        },
        now=1_790_753_400,
    )


def state(**changes):
    return {
        "cards": {},
        "packs": {},
        "printingCards": {},
        "cardFirstAt": {},
        "claimedDex": [],
        "coupons": [],
        "perkTokens": 0,
        "packGrantedInstances": {},
        "packGrantTier": {},
        "packGrantSeeded": False,
        **changes,
    }


def window(instance="a" * 64, utilization=100, key="weekly"):
    return {
        "key": key,
        "name": key,
        "kind": "weekly",
        "utilization": utilization,
        "instance": instance,
    }


@pytest.fixture
def fixed_bonus(monkeypatch):
    from app import native_economy

    monkeypatch.setattr(native_economy, "base_pack_price", lambda set_id, ctx: 5_000_000)
    monkeypatch.setattr(rewards.secrets, "randbelow", lambda upper: upper - 1)


def test_perks_use_only_final_set_milestone_and_preserve_theme_perks():
    first = dex(reward={"perks": [{"kind": "tokenGain", "value": 0.1}]})
    second = dex("set-a", kind="set", cards=[], milestones=[{}, {}])
    ctx = context(
        [first, second],
        [
            {"completed": 1, "perks": [{"kind": "dustBonus", "value": 0.05}]},
            {"completed": 2, "perks": [{"kind": "packDiscount", "value": 0.1}]},
        ],
    )
    intermediate = state(claimedDex=["theme", "set-a#0", "deleted-dex"])
    assert rewards.perks(intermediate, ctx) == {
        "tokenGain": 0.1,
        "dustBonus": 0.05,
        "packDiscount": 0.0,
        "hitOdds": 0.0,
    }
    intermediate["claimedDex"].append("set-a#1")
    assert rewards.perks(intermediate, ctx)["packDiscount"] == 0.1


def test_perk_caps_apply_after_summing_all_sources():
    ctx = context(
        [dex(reward={"perks": [{"kind": kind, "value": 1.0} for kind in rewards.PERK_CAPS]})]
    )
    assert rewards.perks(state(claimedDex=["theme"]), ctx) == rewards.PERK_CAPS


def test_completions_are_new_positive_owned_themes_in_stable_order():
    ctx = context(
        [
            dex("z", tier=2),
            dex("a", tier=2),
            dex("easy", tier=1),
            dex("old", cards=["a-2"]),
            dex("claimed"),
            dex("zero", cards=["a-3"]),
            dex("set-a", kind="set", cards=[], milestones=[{"need": 1}]),
        ]
    )
    current = state(cards={"a-1": 1, "a-2": 1, "a-3": 0}, claimedDex=["claimed"])
    found = rewards.completions({"a-2"}, current, ctx)
    assert [row["dexID"] for row in found] == ["a", "z", "easy"]
    assert current["claimedDex"] == ["claimed"]


def test_claim_dex_merges_only_identical_set_coupon_and_is_once_only():
    reward = {
        "packs": 3,
        "tokens": 50,
        "perks": [],
        "coupons": [
            {"value": 0.5, "count": 2},
            {"value": 0.25, "count": 1},
            {"value": 0.75, "count": 0},
        ],
    }
    ctx = context([dex(reward=reward)])
    current = state(
        cards={"a-1": 1},
        coupons=[
            {"setID": "a", "value": 0.5, "left": 1},
            {"setID": "b", "value": 0.5, "left": 8},
        ],
    )
    result = rewards.claim_dex(current, {"dex_id": "theme"}, ctx)
    assert current["claimedDex"] == ["theme"]
    assert current["packs"] == {"a": 3}
    assert current["perkTokens"] == 50
    assert current["coupons"] == [
        {"setID": "a", "value": 0.5, "left": 3},
        {"setID": "b", "value": 0.5, "left": 8},
        {"setID": "a", "value": 0.25, "left": 1},
    ]
    assert "card" not in result["dex"]
    before = deepcopy(current)
    with pytest.raises(HTTPException):
        rewards.claim_dex(current, {"dex_id": "theme"}, ctx)
    assert current == before


def test_set_dex_counts_species_and_allows_reached_steps_out_of_order():
    ctx = context(
        [
            dex(
                "set-a",
                kind="set",
                cards=[],
                milestones=[
                    {"need": 1, "reward": {"packs": 1}},
                    {"need": 2, "reward": {"tokens": 4}},
                ],
            )
        ]
    )
    current = state(cards={"a-1": 100, "a-2": 1, "a-3": 0})
    rewards.claim_dex(current, {"dex_id": "set-a", "step": 1}, ctx)
    assert current["claimedDex"] == ["set-a#1"]
    assert current["perkTokens"] == 4
    rewards.claim_dex(current, {"dex_id": "set-a", "step": 0}, ctx)
    assert current["packs"] == {"a": 1}


@pytest.mark.parametrize(
    "command",
    [
        {"dex_id": "missing"},
        {"dex_id": "theme", "step": 1},
        {"dex_id": "theme"},
    ],
)
def test_invalid_dex_claim_does_not_modify_state(command):
    current = state()
    before = deepcopy(current)
    with pytest.raises(HTTPException):
        rewards.claim_dex(current, command, context([dex()]))
    assert current == before


def test_card_reward_prefers_unowned_with_tier_floor_and_original_order_ties(monkeypatch):
    from app import native_economy

    monkeypatch.setattr(native_economy, "usd", lambda card_id, ctx: 100)
    monkeypatch.setattr(native_economy, "default_finish", lambda card_id, ctx: "gold")
    reward = {"card": {"tierFloor": "UR", "targetUSD": 100}}
    ctx = context([dex(reward=reward)])
    current = state(cards={"a-1": 1, "a-2": 1}, cardFirstAt={"a-1": 10})
    result = rewards.claim_dex(current, {"dex_id": "theme"}, ctx)
    assert result["dex"]["card"] == "a-3"
    assert current["printingCards"] == {"a-3#gold": 1}
    assert current["cardFirstAt"]["a-3"] == int(ctx.now)
    # Already owned fallback picks the first equally priced card, preserving
    # its original acquisition time rather than resetting collection sorting.
    current = state(cards={"a-1": 1, "a-2": 1, "a-3": 1}, cardFirstAt={"a-2": 10})
    result = rewards.claim_dex(current, {"dex_id": "theme"}, ctx)
    assert result["dex"]["card"] == "a-2"
    assert current["cardFirstAt"]["a-2"] == 10


def test_reward_result_detaches_immutable_manifest():
    entry = dex()
    entry["name"] = MappingProxyType(entry["name"])
    ctx = context([MappingProxyType(entry)])
    result = rewards.claim_dex(state(cards={"a-1": 1}), {"dex_id": "theme"}, ctx)
    result["dex"]["dex"]["name"]["ko"] = "changed"
    assert entry["name"]["ko"] == "theme"


@pytest.mark.parametrize("eligible_field", ["packsOpened", "spentTokens"])
def test_initialize_grants_each_gift_once_for_eligible_accounts(eligible_field):
    current = state(**{eligible_field: 1})
    rewards.initialize_gifts(current, context())
    assert current["perkTokens"] == 4 * 213_370_000
    assert current["packs"] == {"a": 1, "b": 1}
    assert current["grantedGifts"] == list(rewards.GIFTS)
    before = deepcopy(current)
    rewards.initialize_gifts(current, context())
    assert current == before


def test_ineligible_gifts_stay_claimed_after_later_purchase():
    current = state(marketSpentTokens=10)
    rewards.initialize_gifts(current, context())
    assert current["perkTokens"] == 0
    assert current["packs"] == {}
    current["spentTokens"] = 10
    rewards.initialize_gifts(current, context())
    assert current["perkTokens"] == 0
    with pytest.raises(HTTPException):
        rewards.claim_gift(current, {"gift_id": "future-gift"}, context())


def test_preferences_require_owned_favorite_and_unlocked_title_then_clear_optional_fields():
    ctx = context([dex()], [{"completed": 1, "perks": []}])
    current = state(cards={"a-1": 1}, claimedDex=["theme"])
    rewards.set_preferences(
        current,
        {
            "opening_mode": "realistic",
            "favorite_card_id": "a-1",
            "title": 1,
        },
        ctx,
    )
    assert current["openingMode"] == "realistic"
    assert current["favoriteCardID"] == "a-1" and current["title"] == 1
    before = deepcopy(current)
    for command in [
        {"opening_mode": "game", "favorite_card_id": "a-2"},
        {"opening_mode": "game", "title": 2},
        {"opening_mode": "unknown"},
    ]:
        with pytest.raises(HTTPException):
            rewards.set_preferences(current, command, ctx)
        assert current == before
    rewards.set_preferences(current, {"opening_mode": "game"}, ctx)
    assert "favoriteCardID" not in current and "title" not in current


def test_initial_full_windows_are_seeded_without_retroactive_payout(fixed_bonus):
    current = state()
    report = {"windows": [window(), window(instance="", key="session")]}
    rewards.report_bonus(current, report, context())
    assert current["packs"] == {}
    assert current["packGrantSeeded"] is True
    assert current["packGrantedInstances"] == {"weekly": ["a" * 64]}
    assert current["packGrantTier"] == {"session": 1}
    rewards.report_bonus(current, report, context())
    assert current["packs"] == {}


def test_hashed_instances_survive_account_switch_and_temporary_missing_instance(fixed_bonus):
    current = state()
    rewards.report_bonus(current, {"windows": [window(utilization=50)]}, context())
    rewards.report_bonus(current, {"windows": [window()]}, context())
    assert current["packs"] == {"b": 4}
    rewards.report_bonus(
        current, {"windows": [window(instance="b" * 64, utilization=0)]}, context()
    )
    rewards.report_bonus(current, {"windows": [window()]}, context())
    rewards.report_bonus(current, {"windows": [window(instance="")]}, context())
    assert current["packs"] == {"b": 4}
    rewards.report_bonus(current, {"windows": [window(instance="b" * 64)]}, context())
    rewards.report_bonus(current, {"windows": [window()]}, context())
    assert current["packs"] == {"b": 8}
    assert current["packGrantedInstances"]["weekly"] == ["a" * 64, "b" * 64]


def test_instance_less_window_rearms_only_below_full_and_deduplicates_same_report(fixed_bonus):
    current = state(packGrantSeeded=True)
    rewards.report_bonus(
        current, {"windows": [window(instance=""), window(instance="")]}, context()
    )
    assert current["packs"] == {"b": 4}
    rewards.report_bonus(current, {"windows": [window(instance="", utilization=99.9)]}, context())
    assert current["packGrantTier"] == {}
    rewards.report_bonus(current, {"windows": [window(instance="", utilization=100)]}, context())
    assert current["packs"] == {"b": 8}


def test_legacy_paid_marker_adopts_known_instance_but_not_existing_empty_ledger(fixed_bonus):
    current = state(packGrantSeeded=True, packGrantTier={"weekly": 1})
    rewards.report_bonus(current, {"windows": [window()]}, context())
    assert current["packs"] == {}
    assert current["packGrantedInstances"] == {"weekly": ["a" * 64]}
    current = state(
        packGrantSeeded=True, packGrantTier={"weekly": 1}, packGrantedInstances={"weekly": []}
    )
    rewards.report_bonus(current, {"windows": [window()]}, context())
    assert current["packs"] == {"b": 4}


def test_bonus_keeps_last_24_instances_and_does_not_rehash_them(fixed_bonus):
    old = [str(i).zfill(64) for i in range(24)]
    current = state(packGrantSeeded=True, packGrantedInstances={"weekly": old})
    rewards.report_bonus(current, {"windows": [window()]}, context())
    assert current["packGrantedInstances"]["weekly"] == old[1:] + ["a" * 64]


def test_bonus_without_available_sets_does_not_seed_or_mutate(fixed_bonus):
    current = state()
    before = deepcopy(current)
    rewards.report_bonus(current, {"windows": [window()]}, context(sets=[]))
    assert current == before


def test_bonus_payout_filters_over_budget_and_caps_quantity(monkeypatch):
    monkeypatch.setattr(rewards.secrets, "randbelow", lambda upper: 0)
    assert rewards.bonus_payout([("zero", 0), ("huge", 20_000_001), ("cheap", 1)]) == ("cheap", 10)
    assert rewards.bonus_payout([("a", 30_000_000), ("b", 25_000_000)]) == ("b", 1)
    assert rewards.bonus_payout([("a", 3_000_000)]) == ("a", 6)
