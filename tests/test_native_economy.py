"""Exact wallet contracts and cached catalogue pricing (no running server)."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import native_economy as economy
from app.native_data import load


@pytest.fixture
def ctx():
    cards = {
        "a": {
            "id": "a",
            "set_id": "p",
            "tier": "C",
            "default_finish": "normal",
            "finish_by_hint": {"defaultForCard": "normal"},
        },
        "b": {
            "id": "b",
            "set_id": "p",
            "tier": "C",
            "default_finish": "holo",
            "finish_by_hint": {"defaultForCard": "holo"},
        },
    }
    data = {
        "pack_rules": {
            "p": {
                "pool": {"C": ["a", "b"]},
                "contents": {"game_card_count": 1},
                "pack_odds": [{"tier": "C", "probability": 1.0}],
                "slots": [],
                "special_rules": [],
                "fallback_pack_tokens": 10_000_000,
            }
        },
        "special_requests": {},
    }
    prices = {
        "cardPrices": {
            "currency": "USD",
            "krwPerUsd": 1460,
            "prices": {"a": 1, "b": 3},
            "printingPrices": {"a#holo": 2, "b#holo": 4},
        },
        "packPrices": {"asOf": "2026-09-30", "packs": {"p": {"usd": 10}}},
    }
    value = SimpleNamespace(
        data=data,
        cards_by_id=cards,
        sets_by_id={"p": {}},
        prices=prices,
        price_version="fixture-v1",
        now=1234567890.75,
        perks=lambda state: {"tokenGain": 0.25, "packDiscount": 0.15, "dustBonus": 0.15},
    )
    value.economy = economy.PriceBook(value)
    return value


def wallet(ctx, **fields):
    state = {"usedSinceInstall": 100_000_000, "cards": {}, **fields}
    economy.normalize_state(state, ctx)
    return state


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.5, 1),
        (-0.5, -1),
        (1.5, 2),
        (2.5, 3),
        (2.49, 2),
        (-2.5, -3),
        (float(10**15), 10**15),
        (float(10**15) + 0.5, 10**15 + 1),
        (float(2**52 + 1), 2**52 + 1),
    ],
)
def test_rounding_matches_swift_not_python_bankers_round(value, expected):
    assert economy.round_swift(value) == expected


def test_legacy_normalization_preserves_both_ledgers_and_timestamps(ctx):
    state = wallet(
        ctx,
        cards={"a": 5, "b": 2},
        printingCards={"a": 1, "a#holo": 1, "b#holo": 4, "b#normal": 0},
        cardFirstAt={"a": 123},
    )
    assert state["cards"] == {"a": 5, "b": 4}
    assert state["printingCards"] == {"a#normal": 4, "a#holo": 1, "b#holo": 4}
    assert state["cardFirstAt"] == {"a": 123, "b": 1234567890}
    again = deepcopy(state)
    economy.normalize_state(state, ctx)
    assert state == again


def test_normalization_does_not_scan_printings_for_each_card(ctx):
    class CountedDict(dict):
        iterations = 0

        def items(self):
            self.iterations += 1
            return super().items()

    state = wallet(ctx)
    state["cards"] = {"a": 1000, "b": 1000}
    printings = CountedDict({"a#normal": 800, "b#holo": 500})
    state["printingCards"] = printings
    economy.normalize_printings(state, ctx)
    assert printings.iterations == 1
    assert sum(state["printingCards"].values()) == 2000


@pytest.mark.parametrize(
    "fields",
    [
        {"usedSinceInstall": True},
        {"usedSinceInstall": -1},
        {"cards": {"a": 1.5}},
        {"cards": {"unknown": 1}},
        {"printingCards": {"a#invented": 1}},
        {"schemaVersion": 3},
        {"packs": {"unknown": 1}},
    ],
)
def test_malformed_or_unknown_balances_are_rejected_not_zeroed(ctx, fields):
    with pytest.raises(HTTPException):
        wallet(ctx, **fields)


def test_pack_coupons_apply_strongest_not_multiplied_and_consume_in_order(ctx):
    state = wallet(
        ctx,
        coupons=[
            {"setID": "p", "left": 2, "value": 0.25},
            {"setID": "p", "left": 1, "value": 0.5},
        ],
    )
    command = {"kind": "buy_packs", "set_id": "p", "count": 4}
    quote = economy.apply(state, {**command, "kind": "quote", "quote_kind": "buy_packs"}, ctx)
    assert quote["tokens"] == 8_340_000
    assert economy.apply(state, command, ctx, quoted_tokens=quote["tokens"]) == {}
    assert state["spentTokens"] == quote["tokens"]
    assert state["packs"] == {"p": 4}
    assert state["coupons"] == []


def test_precomputed_quote_does_not_recompute_price(ctx, monkeypatch):
    state = wallet(ctx)
    quoted = economy.pack_total(state, "p", 2, ctx)

    def unexpected(*args):
        pytest.fail("Purchase recomputed an already verified quote")

    monkeypatch.setattr(economy, "pack_total", unexpected)
    economy.apply(
        state, {"kind": "buy_packs", "set_id": "p", "count": 2}, ctx, quoted_tokens=quoted
    )
    assert state["spentTokens"] == quoted


def test_sales_keep_last_card_reserved_floor_and_cheapest_finishes_first(ctx):
    state = wallet(ctx, cards={"a": 6}, printingCards={"a#normal": 4, "a#holo": 2})
    result = economy.apply(
        state, {"kind": "sell_bulk", "card_ids": ["a"]}, ctx, protected={"a#holo": 2}
    )
    assert result == {
        "tokens": 1_360_000,
        "sold": 4,
        "bulk": {"kinds": 1, "copies": 4, "tokens": 1_360_000},
    }
    assert state["cards"] == {"a": 2}
    assert state["printingCards"] == {"a#holo": 2}
    assert state["refundedTokens"] == 1_360_000
    assert state["cardsDisenchanted"] == 4


def test_equal_price_finishes_keep_more_special_copy(ctx):
    ctx.prices["cardPrices"]["printingPrices"]["a#holo"] = 1
    ctx.economy = economy.PriceBook(ctx)
    state = wallet(ctx, cards={"a": 3}, printingCards={"a#normal": 2, "a#holo": 1})
    economy.apply(state, {"kind": "sell_spares", "card_id": "a", "count": 2}, ctx)
    assert state["printingCards"] == {"a#holo": 1}


def test_partial_reserved_sale_fails_without_mutating_wallet(ctx):
    state = wallet(ctx, cards={"a": 5})
    previous = deepcopy(state)
    with pytest.raises(HTTPException):
        economy.apply(
            state,
            {"kind": "sell_spares", "card_id": "a", "count": 4},
            ctx,
            protected={"a#normal": 3},
        )
    assert state == previous


def test_transfer_keeps_last_of_each_printing_and_credits_separate_ledger(ctx):
    state = wallet(ctx, cards={"a": 3}, printingCards={"a#normal": 1, "a#holo": 2})
    with pytest.raises(HTTPException):
        economy.apply(state, {"kind": "transfer", "remove": {"a#normal": 1}}, ctx)
    economy.apply(
        state,
        {
            "kind": "transfer",
            "remove": {"a#holo": 1},
            "add": {"b#holo": 2},
            "market_credit": 123,
            "market_debit": 45,
        },
        ctx,
    )
    assert state["cards"] == {"a": 2, "b": 2}
    assert state["marketEarnedTokens"] == 123
    assert state["marketSpentTokens"] == 45
    assert state["usedSinceInstall"] == 100_000_000
    assert state["cardFirstAt"]["b"] == 1234567890


def test_token_perk_is_separate_and_uses_half_away_rounding(ctx):
    state = wallet(ctx, usedSinceInstall=0)
    economy.apply(state, {"kind": "apply_tokens", "collected_total": 2}, ctx)
    assert state["usedSinceInstall"] == 2
    assert state["perkTokens"] == 1
    assert state["installBaselineSet"] is True


def test_cached_quote_is_not_mutable_and_new_snapshot_uses_new_prices(ctx, monkeypatch):
    first = economy.pack_quote("p", ctx)
    first["baseTokens"] = 1

    def unexpected(*args):
        pytest.fail("A cached pack quote recalculated its catalogue expected value")

    monkeypatch.setattr(ctx.economy, "pack_value", unexpected)
    assert economy.pack_quote("p", ctx)["baseTokens"] == 2_920_000
    ctx.prices = deepcopy(ctx.prices)
    ctx.prices["packPrices"]["packs"]["p"]["usd"] = 20
    ctx.price_version = "fixture-v2"
    ctx.economy = economy.PriceBook(ctx)
    assert economy.pack_quote("p", ctx)["baseTokens"] == 5_840_000


def test_all_127_set_prices_match_swift_exported_oracle():
    root = Path(__file__).resolve().parents[1] / "data"
    manifest = load(root)
    ctx = SimpleNamespace(
        data=manifest,
        cards_by_id={card["id"]: card for card in manifest["cards"]},
        price_version="bundled",
        prices={
            "cardPrices": json.loads((root / "card-prices.json").read_text()),
            "packPrices": json.loads((root / "pack-prices.json").read_text()),
        },
    )
    book = economy.PriceBook(ctx)
    expected = json.loads((root / "native-rules-oracle.json").read_text())["prices"]
    assert len(expected) == 127
    for row in expected:
        actual = book.quote(row["set_id"])
        assert actual["baseTokens"] == row["base_tokens"], row["set_id"]
        assert actual["expectedCardValueUSD"] == pytest.approx(row["ev_usd"], abs=1e-10)


@pytest.mark.parametrize(
    "fields",
    [
        {"claimedDex": 123},
        {"claimedDex": [{}]},
        {"completedDex": [1]},
        {"grantedGifts": "not-a-list"},
        {"openingHistory": {"bad": "shape"}},
        {"oripa": {"bad": "shape"}},
        {"oripa": {"cards": ["a"], "opened": [True]}},
        {"oripa": {"cards": ["a"], "opened": [1]}},
        {"oripa": {"cards": ["a"], "opened": [0, 0]}},
        {"oripa": {"cards": ["a"], "serial": "first"}},
        {"openingMode": "invalid"},
        {"openingMode": []},
        {"language": "invalid"},
        {"packGrantSeeded": "false"},
        {"installBaselineSet": 0},
        {"lastDate": 0},
        {"packGrantedInstances": {"weekly": "not-a-list"}},
        {"packGrantedInstances": {"weekly": [None]}},
        {"title": True},
        {"favoriteCardID": []},
        {"coupons": {}},
    ],
)
def test_inspect_rejects_malformed_nonledger_fields_before_defaults(ctx, fields):
    state = {"usedSinceInstall": 0, "cards": {}, **fields}
    unchanged = deepcopy(state)
    with pytest.raises(HTTPException) as error:
        economy.normalize_state(state, ctx)
    assert error.value.status_code == 409
    assert state == unchanged


def test_optional_nulls_and_legacy_oripa_defaults_remain_supported(ctx):
    state = wallet(
        ctx, favoriteCardID=None, title=None, oripa=None, claimedTodayTokensByProvider=None
    )
    assert all(
        key not in state
        for key in ("favoriteCardID", "title", "oripa", "claimedTodayTokensByProvider")
    )
    state = wallet(ctx, oripa={"slots": ["a", "b"]})
    assert state["oripa"] == {"cards": ["a", "b"], "opened": [], "serial": 1}
    economy.apply(state, {"kind": "apply_tokens", "collected_total": 1}, ctx)
    assert state["usedSinceInstall"] == 100_000_001


@pytest.fixture(scope="module")
def recorded_wallet():
    from app.game_service import initial_state
    from app.native_rules import PythonRules

    rules = PythonRules(clock=lambda: 1_790_000_000.0, seed_source=lambda: 42)
    state = initial_state() | {"packs": {"sv8pt5": 1}}
    output, _, _ = rules.apply(state, {"kind": "open_packs", "set_id": "sv8pt5", "count": 1})
    return rules, output


def test_real_opening_record_is_preserved_when_inspected_and_credited(recorded_wallet):
    rules, state = recorded_wallet
    inspected, _, _ = rules.apply(state, {"kind": "inspect"})
    assert inspected == state
    credited, _, _ = rules.apply(inspected, {"kind": "apply_tokens", "collected_total": 1})
    assert credited["openingHistory"] == state["openingHistory"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "not-a-uuid"),
        ("openedAt", True),
        ("mode", "bad-mode"),
        ("variant", "unknown-variant"),
        ("seed", 123),
        ("pityBefore", "0"),
        ("printings", ["a#normal"]),
        ("printings", [{"cardID": "a", "finish": "invented"}]),
        ("supplement", {"energyCount": 1, "holoEnergy": "false", "codeCount": 1}),
        ("packQuote", {"baseTokens": 100}),
        ("priceSnapshotDigest", {}),
    ],
)
def test_nested_history_schema_is_checked_before_inspect(recorded_wallet, field, value):
    rules, valid = recorded_wallet
    state = deepcopy(valid)
    state["openingHistory"][0][field] = value
    with pytest.raises(HTTPException) as error:
        rules.apply(state, {"kind": "inspect"})
    assert error.value.status_code == 409


@pytest.mark.parametrize(
    "fields",
    [
        {"claimedDex": [{}]},
        {"openingHistory": {"bad": "shape"}},
        {"oripa": {"bad": "shape"}},
        {"openingMode": "invalid"},
        {"packGrantSeeded": "false"},
        {"grantedGifts": [123]},
    ],
)
def test_operator_import_rejects_malformed_protected_fields_without_writing(
    tmp_path,
    monkeypatch,
    fields,
):
    from app import admin
    from app.database import Base, make_engine
    from app.game_service import initial_state
    from app.models import Account, GameEvent, Inventory
    from app.native_rules import PythonRules

    engine = make_engine(f"sqlite:///{tmp_path}/isolated-import.db")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(admin, "SessionLocal", sessions)
    monkeypatch.setattr(admin, "rules", PythonRules())
    source = tmp_path / "game-state.json"
    source.write_text(json.dumps(initial_state() | fields))
    original = source.read_bytes()
    try:
        with pytest.raises(HTTPException) as error:
            admin.import_save(str(uuid4()), source, True, source_format=admin.LEGACY_SAVE_FORMAT)
        assert error.value.status_code == 409
        assert source.read_bytes() == original
        with sessions() as session:
            for model in (Account, GameEvent, Inventory):
                assert session.scalar(select(func.count()).select_from(model)) == 0
    finally:
        engine.dispose()


def test_pricebook_matches_validated_currency_alias(ctx):
    cards = ctx.prices["cardPrices"]
    cards["krwPerUSD"] = cards.pop("krwPerUsd")
    assert economy.PriceBook(ctx).step == 20_000
    cards["krwPerUsd"] = 1400
    with pytest.raises(ValueError, match="Conflicting currency"):
        economy.PriceBook(ctx)
