"""Focused physical-draw, state-isolation and finite Oripa stock regressions.

The separately exported oracle corpus additionally covers the real catalogue;
small fixtures here make branch boundaries and failure atomicity explicit.
"""

import hashlib
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import native_opening as opening


@pytest.fixture
def ctx(monkeypatch):
    from app import native_economy as economy

    tiers = ("E", "C", "U", "R", "P", "RR", "SAR")
    ranks = {tier: offset for offset, tier in enumerate(tiers)}
    cards = [
        {
            "id": f"demo-{tier}-{offset}",
            "tier": tier,
            "set_id": "demo",
            "default_finish": "normal",
            "finish_by_hint": {
                "normal": "normal",
                "reverseHolo": "reverseHolo",
                "defaultForCard": "holo",
                "masterBallParallel": "masterBall",
            },
        }
        for tier in ("C", "U", "R", "RR", "SAR")
        for offset in range(3)
    ]
    pool = {tier: [card["id"] for card in cards if card["tier"] == tier] for tier in tiers}
    slots = []
    for kind, count, weights in (
        ("common", 2, [("C", 10000)]),
        ("reverseHolo", 1, [("C", 5000), ("R", 5000)]),
        ("rare", 1, [("R", 9000), ("RR", 1000)]),
    ):
        hint = (
            "normal"
            if kind == "common"
            else "reverseHolo"
            if kind == "reverseHolo"
            else "defaultForCard"
        )
        slots.append(
            {
                "kind": kind,
                "count": count,
                "weights": [{"tier": tier, "weight": weight} for tier, weight in weights],
                "pool": pool,
                "finish_hints": dict.fromkeys(tiers, hint),
                "uses_restricted_pool": False,
                "parallel": None,
            }
        )
    recipe = {
        "pool": pool,
        "slots": slots,
        "special_rules": [],
        "base_variant": "standard",
        "contents": {"game_card_count": 4, "energy_card_count": 1, "code_card_count": 1},
        "reverse_energy": None,
        "vstar_marker": False,
        "energy_style": "sve",
    }
    data = {
        "cards": cards,
        "pack_rules": {"demo": recipe},
        "tier_ranks": ranks,
        "pity_threshold": 5,
        "fallback_chains": {
            tier: sorted(
                (candidate for candidate in tiers if candidate != "E"),
                key=lambda candidate: (abs(ranks[candidate] - ranks[tier]), ranks[candidate]),
            )
            for tier in tiers
        },
        "history_limit": 2,
        "catalogue_digest": "fixture-catalogue",
        "opening_rules_version": "fixture-rules",
        "dexes": [],
        "special_requests": {},
    }
    context = SimpleNamespace(
        data=data,
        cards_by_id={card["id"]: card for card in cards},
        prices={
            "cardPrices": {"asOf": "2026-09-30", "printingPriceSnapshot": {"asOf": "2026-09-29"}}
        },
        price_version="fixture-prices",
        now=1800000000,
        reference_time=821692800,
        prices_are_bundled=True,
        bundled_card_price_digest="fixture-card-bytes",
        seed_source=lambda: 42,
    )
    monkeypatch.setattr(
        economy, "pack_quote", lambda _set, _ctx: {"baseTokens": 100, "basis": "economy"}
    )
    monkeypatch.setattr(economy, "usd", lambda card_id, _ctx: 10.0)
    monkeypatch.setattr(economy, "tokens", lambda value, _ctx: int(value * 100))
    monkeypatch.setattr(economy, "quantized", lambda value, _ctx: value)
    monkeypatch.setattr(economy, "default_finish", lambda card_id, _ctx: "holo")
    opening._shelves.clear()
    return context


def state():
    return {
        "cards": {},
        "printingCards": {},
        "cardFirstAt": {},
        "packs": {"demo": 3},
        "packPity": {},
        "openingMode": "game",
        "openingHistory": [],
        "packsOpened": 0,
        "claimedDex": [],
        "usedSinceInstall": 100000,
        "spentTokens": 0,
        "refundedTokens": 0,
        "perkTokens": 0,
        "giftTokens": 0,
        "marketCredits": 0,
        "marketDebits": 0,
    }


def test_splitmix64_known_words_and_swift_bounded_sampling():
    rng = opening.SplitMix64(0)
    assert [rng.next() for _ in range(3)] == [
        0xE220A8397B1DCDAF,
        0x6E789E6AA1B965F4,
        0x06C45D188009454F,
    ]
    rng = opening.SplitMix64(0)
    assert rng.bounded(10) == 8
    assert rng.unit() == (0x6E789E6AA1B965F4 & ((1 << 53) - 1)) * 2.0**-53
    draws = iter([0, 0xFFFFFFFFFFFFFFFF])
    rng.next = lambda: next(draws)
    assert rng.bounded(10) == 9  # Reject the biased zero low word.


def test_pity_and_realistic_mode_preserve_physical_order(ctx):
    pack, pity = opening.draw_pack("demo", ctx, set(), 42, pity=5)
    assert [slot["finishHint"] for slot in pack["slotResults"]] == [
        "normal",
        "normal",
        "reverseHolo",
        "defaultForCard",
    ]
    assert pack["slotResults"][-1]["card"]["tier"] == "RR"
    assert pity == 0
    standard = opening.draw_pack("demo", ctx, set(), 42, pity=0, mode="realistic", hit_odds=0)
    boosted = opening.draw_pack("demo", ctx, set(), 42, pity=99, mode="realistic", hit_odds=0.2)
    assert boosted == standard
    assert boosted[1] == 0


def test_no_pack_duplicates_when_pool_has_alternatives(ctx):
    for seed in range(20):
        pack, _ = opening.draw_pack("demo", ctx, set(), seed)
        ids = [slot["card"]["id"] for slot in pack["slotResults"]]
        assert len(ids) == len(set(ids))


def test_batch_new_flags_atomic_counts_printings_and_history_limit(ctx):
    wallet = state()
    result = opening.open_packs(wallet, {"set_id": "demo", "count": 3}, ctx)
    assert not wallet["packs"]
    assert wallet["packsOpened"] == 3
    assert sum(wallet["cards"].values()) == sum(wallet["printingCards"].values()) == 12
    assert len(wallet["openingHistory"]) == 2
    assert all(record["seed"] == "42" for record in wallet["openingHistory"])
    assert all(
        record["priceSnapshotDigest"] == "fixture-card-bytes" for record in wallet["openingHistory"]
    )
    assert all(record["printingPriceDate"] == "2026-09-29" for record in wallet["openingHistory"])
    assert all(slot["card"]["isNew"] for slot in result["packs"]["packs"][0]["slotResults"])
    assert not any(slot["card"]["isNew"] for slot in result["packs"]["packs"][1]["slotResults"])
    assert set(wallet["cardFirstAt"].values()) == {1800000000}


def test_incomplete_batch_consumes_nothing(ctx):
    wallet = state()
    before = deepcopy(wallet)
    ctx.data["pack_rules"]["demo"]["contents"]["game_card_count"] = 9
    with pytest.raises(HTTPException):
        opening.open_packs(wallet, {"set_id": "demo", "count": 3}, ctx)
    assert wallet == before


def test_existing_opening_metadata_is_untouched_when_new_snapshot_is_recorded(ctx):
    wallet = state()
    previous = {
        "id": "existing-history",
        "priceSnapshotDigest": "original-snapshot",
        "printingPriceDate": "2025-01-01",
        "cardPriceDate": "2025-01-02",
    }
    wallet["openingHistory"] = [deepcopy(previous)]
    opening.open_packs(wallet, {"set_id": "demo", "count": 1}, ctx)
    assert wallet["openingHistory"][0] == previous
    assert wallet["openingHistory"][1]["printingPriceDate"] == "2026-09-29"
    assert wallet["openingHistory"][1]["cardPriceDate"] == "2026-09-30"
    assert wallet["openingHistory"][1]["priceSnapshotDigest"] == "fixture-card-bytes"


def test_imported_card_digest_matches_foundation_golden_and_is_cached(ctx, monkeypatch):
    payload = {
        "priceSources": {
            "ex10-?": "https://example.test/a",
            "ex10-2": "two",
            "ex10-10": "ten",
            "ex10-!": "mark",
        },
        "prices": {"base1-2": 68.24, "base1-10": 1.0, "base1-1": 0.05},
        "printingPriceSnapshot": {"asOf": "2026-09-23"},
    }
    # Captured directly from macOS Foundation JSONSerialization(.sortedKeys).
    golden = (
        '{"prices":{"base1-1":0.050000000000000003,"base1-2":68.239999999999995,"base1-10":1},'
        '"priceSources":{"ex10-!":"mark","ex10-?":"https:\\/\\/example.test\\/a","ex10-2":"two","ex10-10":"ten"},'
        '"printingPriceSnapshot":{"asOf":"2026-09-23"}}'
    )
    assert opening._foundation_price_json(payload) == golden
    ctx.prices_are_bundled = False
    ctx.price_version = "golden-pair-v1"
    ctx.prices = {"cardPrices": payload, "packPrices": {"notPartOfCardDigest": True}}
    expected = hashlib.sha256(golden.encode()).hexdigest()
    assert opening.card_price_digest(ctx) == expected

    def unexpected_encode(_):
        raise AssertionError("Warm price metadata must not reserialize the catalogue")

    monkeypatch.setattr(opening, "_foundation_price_json", unexpected_encode)
    assert opening.card_price_digest(ctx) == expected
    ctx.prices_are_bundled = True
    assert opening.card_price_digest(ctx) == "fixture-card-bytes"


def test_realistic_opening_does_not_erase_saved_game_pity(ctx):
    wallet = state()
    wallet.update(openingMode="realistic", packPity={"demo": 4})
    opening.open_packs(wallet, {"set_id": "demo", "count": 1}, ctx)
    assert wallet["packPity"] == {"demo": 4}
    assert (
        wallet["openingHistory"][0]["pityBefore"] == wallet["openingHistory"][0]["pityAfter"] == 0
    )


def test_parallel_exact_cards_and_finish_hint(ctx):
    reverse = ctx.data["pack_rules"]["demo"]["slots"][1]
    reverse["parallel"] = {
        "hits": 1,
        "rolls": 1,
        "candidates": [{"tier": "C", "id": "demo-C-0"}],
        "finish_hint": "masterBallParallel",
    }
    pack, _ = opening.draw_pack("demo", ctx, set(), 42)
    assert pack["slotResults"][2]["card"] == {
        "id": "demo-C-0",
        "tier": "C",
        "isNew": True,
        "finish": "masterBall",
    }


def test_reverse_slot_energy_is_not_collected(ctx):
    wallet = state()
    ctx.data["pack_rules"]["demo"]["reverse_energy"] = {"finish": "holo", "one_in": 1}
    result = opening.open_packs(wallet, {"set_id": "demo", "count": 1}, ctx)
    cards = [slot["card"] for slot in result["packs"]["packs"][0]["slotResults"]]
    energy = next(card for card in cards if card["id"].startswith("supplement-energy-"))
    assert energy["isNew"] is False
    assert sum(wallet["cards"].values()) == 3
    assert len(wallet["openingHistory"][0]["printings"]) == 4


def test_newly_complete_dexes_are_reported_not_claimed(ctx):
    ctx.data["dexes"] = [
        {"id": "demo-dex", "name": {"en": "Demo"}, "tier": 1, "cards": ["demo-C-0"]}
    ]
    wallet = state()
    pack, _ = opening.draw_pack("demo", ctx, set(), 42)
    ctx.data["dexes"][0]["cards"] = [pack["slotResults"][0]["card"]["id"]]
    result = opening.open_packs(wallet, {"set_id": "demo", "count": 1}, ctx)
    assert result["packs"]["completions"] == [
        {"dexID": "demo-dex", "name": {"en": "Demo"}, "tier": 1}
    ]
    assert wallet["claimedDex"] == []


def test_oripa_shelf_binary_windows_match_inclusive_boundaries():
    shelf = opening.OripaShelf((("a", 10), ("b", 8), ("c", 8), ("d", 5)), (-10, -8, -8, -5), 8)
    assert shelf.window(8, 8, 1) == (("b", 8), ("c", 8))
    assert shelf.window(7, 7, 2) == (("c", 8), ("d", 5))
    assert shelf.window(100, 101, 1) == (("a", 10),)


def test_oripa_shelf_is_cached_and_price_version_invalidates(ctx, monkeypatch):
    from app import native_economy as economy

    calls = []
    monkeypatch.setattr(economy, "usd", lambda card_id, _ctx: calls.append(card_id) or 10.0)
    first = opening.oripa_shelf(ctx)
    assert opening.oripa_shelf(ctx) is first
    assert len(calls) == 6
    ctx.price_version = "new-price-version"
    assert opening.oripa_shelf(ctx) is not first
    assert len(calls) == 12


def test_oripa_stock_indices_cannot_be_reopened(ctx, monkeypatch):
    from app import native_economy as economy

    wallet = state()
    wallet["oripa"] = {"cards": ["demo-RR-0"] * 40, "opened": [], "serial": 3}
    charged = []
    monkeypatch.setattr(economy, "spend", lambda _state, value: charged.append(value) or True)
    result = opening.pull_oripa(wallet, {"envelope": 7}, ctx, quoted_tokens=4321)
    assert result["oripa"]["card"]["id"] == "demo-RR-0"
    assert wallet["oripa"]["opened"] == [7]
    assert charged == [4321]
    assert wallet["cards"] == {"demo-RR-0": 1}
    before = deepcopy(wallet)
    with pytest.raises(HTTPException):
        opening.pull_oripa(wallet, {"envelope": 7}, ctx)
    assert wallet == before
    assert charged == [4321]


def test_oripa_generation_uses_unique_stock_and_fresh_cards_first(ctx, monkeypatch):
    keyed = tuple((f"card-{index}", 100.0 / (index + 1)) for index in range(100))
    shelf = opening.OripaShelf(keyed, tuple(-usd for _, usd in keyed), keyed[50][1])
    monkeypatch.setattr(opening, "oripa_shelf", lambda _: shelf)
    wallet = state()
    first = opening.make_oripa_box(wallet, ctx, rng=opening.SplitMix64(42))
    second = opening.make_oripa_box(wallet, ctx, rng=opening.SplitMix64(42))
    assert first == second
    assert len(first["cards"]) == len(set(first["cards"])) == 40
    assert first["serial"] == 1
    wallet["oripa"] = first
    opening.refresh_oripa(wallet, ctx)
    assert wallet["oripa"]["serial"] == 2

    # Every value band has enough unowned stock. Only the headline may be an
    # already owned card; all remaining 39 slots must prefer fresh inventory.
    wide_shelf = SimpleNamespace(keyed=keyed, base_usd=1.0, window=lambda low, high, need: keyed)
    monkeypatch.setattr(opening, "oripa_shelf", lambda _: wide_shelf)
    wallet["cards"] = dict.fromkeys((f"card-{index}" for index in range(50)), 1)
    box = opening.make_oripa_box(wallet, ctx, rng=opening.SplitMix64(42))
    assert sum(card_id in wallet["cards"] for card_id in box["cards"]) <= 1
    assert len(box["cards"]) == len(set(box["cards"])) == 40
