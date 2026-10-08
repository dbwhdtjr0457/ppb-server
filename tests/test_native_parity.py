"""Opt-in differential tests against the pinned, isolated Swift oracle.

Run scripts/verify-native-rules.py. Neither implementation touches the API or a
user database. Deterministic commands compare the full returned state/result;
random commands are checked against independent conservation/schema invariants.
"""

import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.game_service import initial_state
from app.prices import bundled, resources
from app.rules import SwiftRules

pytestmark = pytest.mark.skipif(
    not os.getenv("PPB_TEST_RULES_EXECUTABLE"),
    reason="Set PPB_TEST_RULES_EXECUTABLE to the pinned macOS Swift oracle",
)


@pytest.fixture(scope="module")
def pair():
    from app.native_rules import PythonRules

    executable = Path(os.environ["PPB_TEST_RULES_EXECUTABLE"]).resolve()
    assert executable.is_file(), "The requested Swift oracle is missing"
    root = Path(
        os.getenv("PPB_NATIVE_RULES_RESOURCES", str(Path(__file__).resolve().parents[1] / "data"))
    )
    assert (root / "native-rules.json").is_file(), "The requested native resources are missing"
    return SwiftRules(str(executable)), PythonRules(root), resources(str(executable)), root


@pytest.fixture(scope="module")
def catalog(pair):
    return json.loads((pair[2] / "card-index.json").read_text())


@pytest.fixture(scope="module")
def dexes(pair):
    return json.loads((pair[2] / "dex.json").read_text())["dexes"]


def state(**updates):
    return initial_state() | {"language": "en", "usedSinceInstall": 10**12} | updates


def complete_keys(dexes):
    return [
        f"{dex['id']}#{len(dex['milestones']) - 1}" if dex["kind"] == "set" else dex["id"]
        for dex in dexes
    ]


def assert_tree_equal(actual, expected, path="output"):
    """Keep integer ledgers exact; permit only sub-cent floating-point noise."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        assert actual.keys() == expected.keys(), f"{path}: field mismatch"
        for key, value in expected.items():
            assert_tree_equal(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), path
        for index, value in enumerate(expected):
            assert_tree_equal(actual[index], value, f"{path}[{index}]")
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, rel=1e-12, abs=1e-8), path
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def normalize_new_timestamps(output, before, started):
    """Only first-acquisition timestamps introduced by this call are volatile."""
    output = deepcopy(output)
    previous = before.get("cardFirstAt", {})
    for card, stamp in output[0].get("cardFirstAt", {}).items():
        if card not in previous:
            assert type(stamp) is int and started - 2 <= stamp <= time.time() + 2
            output[0]["cardFirstAt"][card] = "new-acquisition-timestamp"
    # Swift's OripaBox.opened is Set<Int>; JSON array order is unspecified.
    # Keep its membership and uniqueness strict while ignoring encoding order.
    if box := output[0].get("oripa"):
        assert len(box["opened"]) == len(set(box["opened"]))
        box["opened"] = sorted(box["opened"])
    return output


def compare(pair, before, command, **context):
    started = time.time()
    expected = pair[0].apply(deepcopy(before), deepcopy(command), **deepcopy(context))
    actual = pair[1].apply(deepcopy(before), deepcopy(command), **deepcopy(context))
    assert_tree_equal(
        list(normalize_new_timestamps(actual, before, started)),
        list(normalize_new_timestamps(expected, before, started)),
        path=f"{command}",
    )
    return actual


@pytest.mark.parametrize(
    "before",
    [
        {"cards": {}},
        {"cards": {}, "language": "en"},
        state(cards={"base1-4": 3}, cardFirstAt={"base1-4": 100}),
        state(
            cards={"base1-4": 4},
            printingCards={"base1-4#holo": 2, "base1-4#reverseHolo": 2},
            cardFirstAt={"base1-4": 100},
        ),
        state(
            cards={"cel25-25": 1, "swsh45sv-SV107": 2},
            completedDex=["legacy-completion"],
            claimedTodayTokensByProvider={"codex": 1234},
            grantedGifts=["v0.4.1-apology"],
            packGrantSeeded=True,
            packGrantedInstances={"provider.weekly": ["existing-instance"]},
        ),
    ],
)
def test_inspect_preserves_legacy_and_current_state(pair, before):
    compare(pair, before, {"kind": "inspect"})


def test_every_set_pack_quote_matches_swift(pair, catalog):
    assert len(catalog["sets"]) >= 127, "Parity run is missing the current set catalogue"
    for entry in catalog["sets"]:
        compare(
            pair,
            state(),
            {"kind": "quote", "quote_kind": "buy_packs", "set_id": entry["id"], "count": 37},
        )


@pytest.mark.parametrize("set_id", ["base1", "sv8pt5", "me2pt5", "cel30"])
@pytest.mark.parametrize("count", [1, 2, 5, 37, 1000])
def test_coupon_consumption_and_perk_discount_match(pair, dexes, set_id, count):
    before = state(
        claimedDex=complete_keys(dexes),
        coupons=[
            {"setID": set_id, "value": 0.50, "left": 2},
            {"setID": set_id, "value": 0.10, "left": 2},
            {"setID": set_id, "value": 0.25, "left": 1},
            {"setID": "sv1", "value": 0.75, "left": 3},
        ],
    )
    compare(
        pair, before, {"kind": "quote", "quote_kind": "buy_packs", "set_id": set_id, "count": count}
    )
    compare(pair, before, {"kind": "buy_packs", "set_id": set_id, "count": count})


@pytest.mark.parametrize("claimed", [0, 10, 25, 100, 277])
@pytest.mark.parametrize("tokens", [0, 1, 103, 1_000_001])
def test_reported_token_delta_and_perk_rounding_match(pair, dexes, claimed, tokens):
    compare(
        pair,
        state(claimedDex=complete_keys(dexes)[:claimed]),
        {"kind": "apply_tokens", "collected_total": tokens},
    )


@pytest.mark.parametrize("claimed", [False, True])
def test_exact_printing_sales_protection_and_valuation_match(pair, dexes, claimed):
    before = state(
        cards={"base1-4": 8, "sv8pt5-5": 6},
        printingCards={
            "base1-4#holo": 4,
            "base1-4#reverseHolo": 4,
            "sv8pt5-5#holo": 3,
            "sv8pt5-5#pokeBall": 3,
        },
        cardFirstAt={"base1-4": 100, "sv8pt5-5": 200},
        claimedDex=complete_keys(dexes) if claimed else [],
    )
    protected = {"base1-4#holo": 2, "sv8pt5-5#pokeBall": 1}
    for command in [
        {"kind": "valuation"},
        {"kind": "quote", "quote_kind": "sell_spares", "card_id": "base1-4", "count": 3},
        {"kind": "quote", "quote_kind": "sell_bulk", "card_ids": ["base1-4", "sv8pt5-5"]},
        {"kind": "sell_spares", "card_id": "base1-4", "count": 3},
        {"kind": "sell_bulk", "card_ids": ["base1-4", "sv8pt5-5"]},
    ]:
        compare(pair, before, command, protected=protected)


def test_transfer_preserves_cards_printings_and_market_ledgers(pair):
    before = state(
        cards={"base1-4": 5}, printingCards={"base1-4#holo": 5}, cardFirstAt={"base1-4": 100}
    )
    compare(
        pair,
        before,
        {
            "kind": "transfer",
            "remove": {"base1-4#holo": 2},
            "add": {"sv8pt5-5#holo": 1},
            "market_credit": 123,
            "market_debit": 456,
        },
    )


@pytest.mark.parametrize("eligible", [False, True])
@pytest.mark.parametrize("gift", ["v0.4.1-apology", "v0.6.0-patch", "v0.7.0-patch", "v0.8.0-patch"])
def test_gifts_match_and_cannot_be_claimed_twice(pair, eligible, gift):
    before = state(spentTokens=1 if eligible else 0)
    after, _, _ = compare(pair, before, {"kind": "claim_gift", "gift_id": gift})
    repeated, _, _ = compare(pair, after, {"kind": "claim_gift", "gift_id": gift})
    assert repeated == after


def test_dex_reward_channels_and_milestone_claims_match(pair, catalog, dexes):
    # Cover every material channel without changing the published reward design.
    chosen = {}
    for dex in dexes:
        rewards = (
            [m["reward"] for m in dex.get("milestones", [])]
            if dex["kind"] == "set"
            else [dex["reward"]]
        )
        for step, reward in enumerate(rewards):
            for field in ("packs", "tokens", "coupons", "card", "perks"):
                if reward.get(field):
                    chosen.setdefault((dex["kind"], field), (dex, step))
    assert {field for _, field in chosen} == {"packs", "tokens", "coupons", "card", "perks"}
    for dex, step in chosen.values():
        cards = (
            dex["cards"]
            if dex["kind"] != "set"
            else [
                card[0] for card in catalog["cards"] if card[0].split("-", 1)[0] == dex["homeSet"]
            ]
        )
        before = state(cards=dict.fromkeys(cards, 1), cardFirstAt=dict.fromkeys(cards, 100))
        compare(pair, before, {"kind": "claim_dex", "dex_id": dex["id"], "step": step})


def test_existing_oripa_price_and_specific_envelope_match(pair, catalog, dexes):
    cards = [row[0] for row in catalog["cards"] if row[2] == "RR"][:40]
    before = state(
        oripa={"cards": cards, "serial": 3, "opened": [1, 3, 5]}, claimedDex=complete_keys(dexes)
    )
    compare(pair, before, {"kind": "quote", "quote_kind": "pull_oripa"})
    compare(pair, before, {"kind": "pull_oripa", "envelope": 0})


def test_bonus_seed_and_repeat_suppression_match(pair):
    window = {
        "key": "parity.weekly",
        "name": "Parity",
        "kind": "weekly",
        "utilization": 100.0,
        "instance": "hashed-window",
    }
    command = {"kind": "report_bonus", "windows": [window]}
    seeded, _, _ = compare(pair, state(), command)
    repeated, _, _ = compare(pair, seeded, command)
    assert repeated == seeded
    legacy = state(packGrantSeeded=True, packGrantTier={window["key"]: 1})
    adopted, _, _ = compare(pair, legacy, command)
    assert adopted["packGrantedInstances"][window["key"]] == [window["instance"]]


def test_new_bonus_window_awards_only_the_existing_budget_once(pair):
    for engine in pair[:2]:
        window = {
            "key": "parity.weekly",
            "name": "Parity",
            "kind": "weekly",
            "utilization": 100.0,
            "instance": "new-window",
        }
        before = state(
            packGrantSeeded=True, packGrantedInstances={window["key"]: ["previous-window"]}
        )
        command = {"kind": "report_bonus", "windows": [window]}
        after, _, _ = engine.apply(deepcopy(before), command)
        assert len(after["packs"]) == 1
        set_id, count = next(iter(after["packs"].items()))
        _, quote, _ = pair[0].apply(
            state(), {"kind": "quote", "quote_kind": "buy_packs", "set_id": set_id, "count": 1}
        )
        # Published PackConfig budget/cap, independently evaluated by Swift quote.
        assert count == min(10, max(1, 20_000_000 // quote["tokens"]))
        assert count * quote["tokens"] <= 20_000_000
        assert after["packGrantedInstances"][window["key"]] == ["previous-window", "new-window"]
        repeated, _, _ = engine.apply(deepcopy(after), command)
        assert repeated == after


def test_preferences_and_clearing_optional_values_match(pair, dexes):
    before = state(
        cards={"base1-4": 1}, cardFirstAt={"base1-4": 100}, claimedDex=complete_keys(dexes)
    )
    selected, _, _ = compare(
        pair,
        before,
        {
            "kind": "set_preferences",
            "opening_mode": "realistic",
            "favorite_card_id": "base1-4",
            "title": 10,
        },
    )
    cleared, _, _ = compare(pair, selected, {"kind": "set_preferences", "opening_mode": "game"})
    assert "favoriteCardID" not in cleared and "title" not in cleared


def test_initialize_and_oripa_refresh_preserve_wallet_and_create_valid_boxes(pair):
    # Use the tiers both engines actually apply. The app corrects a few upstream
    # rows while loading card-index.json (Black Bolt/White Flare Victini BWR and
    # Archen IR), so the raw file disagrees with the exported rules for them.
    data = pair[1]._context(None).data
    tier_of = {card["id"]: card["tier"] for card in data["cards"]}
    tier_rank = data["tier_ranks"]
    for engine in pair[:2]:
        before = state()
        initialized, result, _ = engine.apply(deepcopy(before), {"kind": "initialize"})
        assert result == {}
        assert initialized["perkTokens"] == 0 and initialized["packs"] == {}
        assert len(initialized["grantedGifts"]) == 4
        refreshed, result, _ = engine.apply(deepcopy(initialized), {"kind": "refresh_oripa"})
        assert result == {}
        assert refreshed["oripa"]["serial"] == initialized["oripa"]["serial"] + 1
        for key in set(initialized) - {"oripa"}:
            assert refreshed[key] == initialized[key], f"Refresh unexpectedly changed {key}"
        for after in (initialized, refreshed):
            box = after["oripa"]
            assert len(box["cards"]) == len(set(box["cards"])) == 40
            assert box["opened"] == []
            assert all(card in tier_of for card in box["cards"])
            assert all(tier_rank[tier_of[card]] >= tier_rank["RR"] for card in box["cards"])


def test_price_snapshot_override_changes_quotes_and_sale_values_consistently(pair):
    snapshot = deepcopy(bundled(pair[0].executable))
    snapshot["packPrices"]["packs"]["base1"]["usd"] *= 2
    snapshot["cardPrices"]["prices"]["base1-4"] *= 1.75
    snapshot["cardPrices"]["printingPrices"]["base1-4#holo"] *= 1.75
    before = state(cards={"base1-4": 3}, cardFirstAt={"base1-4": 100})
    for command in [
        {"kind": "quote", "quote_kind": "buy_packs", "set_id": "base1", "count": 5},
        {"kind": "sell_spares", "card_id": "base1-4", "count": 1},
    ]:
        compare(pair, before, command, prices=snapshot)


def test_parallel_accounts_keep_price_snapshots_and_wallets_isolated(pair, dexes):
    cases = []
    for number in range(8):
        snapshot = deepcopy(bundled(pair[0].executable))
        snapshot["packPrices"]["packs"]["base1"]["usd"] *= 2 + number
        before = state(
            usedSinceInstall=10**12 + number, claimedDex=complete_keys(dexes)[: number * 10]
        )
        command = {"kind": "buy_packs", "set_id": "base1", "count": number + 1}
        expected = pair[0].apply(deepcopy(before), command, prices=snapshot)
        cases.append((before, command, snapshot, expected))

    def evaluate(case):
        before, command, snapshot, expected = case
        actual = pair[1].apply(deepcopy(before), command, prices=snapshot)
        assert_tree_equal(list(actual), list(expected), f"parallel/{command['count']}")

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(evaluate, cases))


@pytest.mark.parametrize(
    "command",
    [
        {"kind": "buy_packs", "set_id": "missing", "count": 1},
        {"kind": "buy_packs", "set_id": "base1", "count": 0},
        {"kind": "open_packs", "set_id": "base1", "count": 1},
        {"kind": "apply_tokens", "collected_total": -1},
        {"kind": "sell_spares", "card_id": "base1-4", "count": 1},
        {"kind": "claim_dex", "dex_id": "missing", "step": 0},
        {"kind": "claim_gift", "gift_id": "missing"},
        {"kind": "set_preferences", "opening_mode": "missing"},
        {"kind": "set_preferences", "opening_mode": "game", "title": 10},
        {"kind": "set_preferences", "opening_mode": "game", "favorite_card_id": "base1-4"},
        {"kind": "transfer", "remove": {"base1-4#holo": 1}},
    ],
)
def test_invalid_commands_reject_without_mutating_input(pair, command):
    before = state()
    for engine in pair[:2]:
        candidate = deepcopy(before)
        with pytest.raises(HTTPException) as rejected:
            engine.apply(candidate, deepcopy(command))
        assert rejected.value.status_code == 409
        assert candidate == before


@pytest.mark.parametrize("mode", ["game", "realistic"])
@pytest.mark.parametrize(
    "set_id",
    [
        "base1",
        "ex10",
        "xy11",
        "sm115",
        "swsh12pt5",
        "sv8pt5",
        "me2pt5",
        "cel30",
    ],
)
def test_random_openings_conserve_inventory_and_history(pair, catalog, mode, set_id):
    # A random server command intentionally accepts no client-supplied seed.
    # Check each independent draw, not equality between unrelated random draws.
    collectible_ids = {card[0] for card in catalog["cards"]}
    expected_metadata = None
    metadata_keys = {
        "rulesVersion",
        "catalogueDigest",
        "mode",
        "hitOddsBonus",
        "cardPriceDate",
        "printingPriceDate",
        "priceSnapshotDigest",
        "packQuote",
    }
    for engine in pair[:2]:
        before = state(packs={set_id: 20}, openingMode=mode)
        after, result, version = engine.apply(
            deepcopy(before),
            {
                "kind": "open_packs",
                "set_id": set_id,
                "count": 20,
            },
        )
        packs = result["packs"]["packs"]
        assert len(packs) == 20
        assert after["packs"].get(set_id, 0) == 0
        assert after["packsOpened"] == 20
        assert after["usedSinceInstall"] == before["usedSinceInstall"]
        assert after["spentTokens"] == before["spentTokens"]
        cards, printings = Counter(), Counter()
        for pack in packs:
            assert pack["slotResults"], f"{set_id}: empty pack"
            for slot in pack["slotResults"]:
                card = slot["card"]
                assert {"id", "tier", "isNew", "finish"}.issubset(card)
                assert isinstance(card["isNew"], bool)
                if card["id"] not in collectible_ids:
                    # Physical energy supplements appear in the reveal but do
                    # not enter the tradable collection in the existing app.
                    assert card["id"].startswith("supplement-energy-")
                    assert card["tier"] == "E"
                    assert card["id"] not in after["cards"]
                    continue
                cards[card["id"]] += 1
                printings[f"{card['id']}#{card['finish']}"] += 1
        assert after["cards"] == dict(cards)
        assert after["printingCards"] == dict(printings)
        history = after["openingHistory"]
        assert len(history) == 20
        assert Counter(h["variant"] for h in history) == Counter(p["variant"] for p in packs)
        assert all(h["setID"] == set_id and h["mode"] == mode for h in history)
        assert all(h["printings"] and h["seed"] and h["rulesVersion"] for h in history)
        metadata = {key: history[0][key] for key in metadata_keys if key in history[0]}
        if expected_metadata is None:
            expected_metadata = metadata
        else:
            assert_tree_equal(metadata, expected_metadata, f"{set_id}/{mode}/history-metadata")
        assert version.startswith("ppb-server-v2/")
        if mode == "realistic":
            assert not after["packPity"]
        # Existing Swift decoding is the compatibility boundary for old clients.
        checked, _, _ = pair[0].apply(deepcopy(after), {"kind": "inspect"})
        assert checked["cards"] == after["cards"]
        assert checked["printingCards"] == after["printingCards"]
        assert len(checked["openingHistory"]) == 20


def test_opening_history_tracks_the_selected_price_snapshot(pair):
    snapshot = deepcopy(bundled(pair[0].executable))
    snapshot["cardPrices"]["prices"]["base1-4"] *= 1.75
    snapshot["cardPrices"]["printingPrices"]["base1-4#holo"] *= 1.75
    records = []
    for engine in pair[:2]:
        after, _, _ = engine.apply(
            state(packs={"base1": 1}),
            {"kind": "open_packs", "set_id": "base1", "count": 1},
            prices=snapshot,
        )
        record = after["openingHistory"][0]
        records.append(
            {
                key: record[key]
                for key in (
                    "priceSnapshotDigest",
                    "cardPriceDate",
                    "printingPriceDate",
                    "packQuote",
                )
                if key in record
            }
        )
    assert_tree_equal(records[1], records[0], "custom-price-history")


def test_seeded_openings_match_swift_oracle_for_every_set_and_mode(pair):
    from app.native_opening import draw_pack

    oracle = json.loads((pair[3] / "native-rules-oracle.json").read_text())
    openings = oracle["openings"]
    assert len({(row["set_id"], row["mode"]) for row in openings}) >= 254
    context = pair[1]._context(None)
    for row in openings:
        opened, pity = draw_pack(
            row["set_id"],
            context,
            set(),
            int(row["seed"]),
            pity=row["pity_before"],
            mode=row["mode"],
            hit_odds=row.get("hit_odds_bonus", 0.0),
        )
        assert_tree_equal(opened, row["opened"], f"{row['set_id']}/{row['mode']}")
        assert pity == row["pity_after"], f"{row['set_id']}/{row['mode']}: pity"


def test_seeded_oripa_boxes_match_swift_for_new_and_complete_collections(pair):
    from app.native_opening import SplitMix64, make_oripa_box

    oracle = json.loads((pair[3] / "native-rules-oracle.json").read_text())
    fixtures = oracle["oripa"]
    assert {(row["seed"], row["owned_all"]) for row in fixtures} == {
        ("42", False),
        ("42", True),
        ("2026", False),
        ("2026", True),
    }
    context = pair[1]._context(None)
    for row in fixtures:
        owned = dict.fromkeys(pair[1].catalogue, 1) if row["owned_all"] else {}
        before = state(cards=owned, oripa={"serial": row["serial"] - 1})
        actual = make_oripa_box(before, context, rng=SplitMix64(int(row["seed"])))
        assert_tree_equal(actual, row["box"], f"oripa/{row['seed']}/owned={row['owned_all']}")
