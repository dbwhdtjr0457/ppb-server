"""Production rules must stay in-process and cache only price-independent state."""

import subprocess
from copy import deepcopy

import pytest
from fastapi import HTTPException

from app.game_service import initial_state
from app.native_rules import PythonRules


def test_native_runtime_never_spawns_swift_and_does_not_mutate_inputs(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Native game handling spawned a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    rules = PythonRules()
    rules.warm()
    before = {**initial_state(), "perkTokens": 10**12}
    original = deepcopy(before)
    prices = rules.bundled_prices
    original_prices = deepcopy(prices)
    after, _, version, quote = rules.apply_with_quote(
        before, {"kind": "buy_packs", "set_id": "cel30", "count": 20}, prices=prices
    )
    assert after["packs"]["cel30"] == 20 and quote > 0 and version == rules.version
    opened, result, _ = rules.apply(
        after, {"kind": "open_packs", "set_id": "cel30", "count": 20}, prices=prices
    )
    assert opened["packsOpened"] == 20 and len(result["packs"]["packs"]) == 20
    assert before == original and prices == original_prices


def test_compiled_prices_are_reused_and_bounded_per_version(monkeypatch):
    import app.native_economy as economy

    original = economy.PriceBook
    compiled = []

    def record(ctx):
        compiled.append(ctx.price_version)
        return original(ctx)

    monkeypatch.setattr(economy, "PriceBook", record)
    rules = PythonRules()
    price_pair = rules.bundled_prices
    for _ in range(4):
        rules.apply(initial_state(), {"kind": "inspect"}, prices=price_pair)
    assert len(compiled) == 1
    for offset in range(5):
        changed = deepcopy(price_pair)
        changed["cardPrices"]["prices"]["base1-4"] += offset + 1
        rules.apply(initial_state(), {"kind": "inspect"}, prices=changed)
    assert len(compiled) == 6 and len(rules._price_books) == 4
    assert len(set(compiled)) == 6


@pytest.mark.parametrize("missing", ["prices", "printingPrices"])
def test_partial_price_snapshot_is_rejected_before_cache_publication(missing):
    rules = PythonRules()
    changed = deepcopy(rules.bundled_prices)
    changed["cardPrices"][missing].pop(next(iter(changed["cardPrices"][missing])))
    with pytest.raises(HTTPException, match="prices_unavailable"):
        rules.apply(initial_state(), {"kind": "inspect"}, prices=changed)
    assert not rules._price_books
