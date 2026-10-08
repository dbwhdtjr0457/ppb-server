"""In-process, request-isolated rules; no runtime Swift, user files, or network calls.

Resources and compiled price books are bounded, versioned caches. Wallets, clocks
and random streams belong to one evaluation and are never stored in those caches.
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from functools import cached_property
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from app.performance import timed


class RuleContext(SimpleNamespace):
    def perks(self, state):
        from app.native_rewards import perks

        return perks(state, self)


class PythonRules:
    supports_context = True
    backend = "python"
    executable = ""  # Legacy oracle compatibility, never executed by this backend.

    def __init__(self, resources_directory=None, *, clock=None, seed_source=None):
        self.resources_dir = Path(
            resources_directory or Path(__file__).resolve().parents[1] / "data"
        ).resolve()
        self.configured = bool(resources_directory)
        self.clock = clock or time.time
        self.seed_source = seed_source
        self._price_books = OrderedDict()
        self._price_lock = threading.RLock()

    @cached_property
    def data(self):
        from app.native_data import load

        try:
            return load(self.resources_dir)
        except (OSError, ValueError, KeyError) as error:
            raise HTTPException(503, "rules_data_unavailable") from error

    @cached_property
    def catalogue(self):
        cards = self.data["cards"]
        return cards if isinstance(cards, Mapping) else {card["id"]: card for card in cards}

    @cached_property
    def sets(self):
        sets = self.data["sets"]
        return sets if isinstance(sets, Mapping) else {entry["id"]: entry for entry in sets}

    @property
    def version(self):
        return self.data["rules_version"]

    @cached_property
    def bundled_prices(self):
        try:
            return {
                "schemaVersion": 1,
                "cardPrices": json.loads((self.resources_dir / "card-prices.json").read_text()),
                "packPrices": json.loads((self.resources_dir / "pack-prices.json").read_text()),
            }
        except (OSError, ValueError) as error:
            raise HTTPException(503, "prices_unavailable") from error

    @cached_property
    def bundled_card_price_digest(self):
        return hashlib.sha256((self.resources_dir / "card-prices.json").read_bytes()).hexdigest()

    def _context(self, payload):
        from app.native_economy import PriceBook
        from app.prices import validate as validate_prices

        prices_are_bundled = payload is None
        payload = self.bundled_prices if prices_are_bundled else payload
        # prices.current retains immutable payload objects per published version.
        # Strong references prevent object-ID reuse; bounded retention caps memory.
        key = id(payload)
        with self._price_lock:
            entry = self._price_books.get(key)
            if entry is None or entry[0] is not payload:
                # The Swift boundary rejected partial snapshots; preserve that
                # check once per version rather than on every rule evaluation.
                try:
                    validate_prices(payload, self.bundled_prices)
                except (KeyError, TypeError, ValueError) as error:
                    raise HTTPException(503, "prices_unavailable") from error
                encoded = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
                if len(encoded) > 25_000_000:
                    raise HTTPException(503, "prices_unavailable")
                version = hashlib.sha256(encoded).hexdigest()
                base = RuleContext(
                    data=self.data,
                    cards_by_id=self.catalogue,
                    sets_by_id=self.sets,
                    prices=payload,
                    price_version=version,
                )
                book = PriceBook(base)
                entry = (payload, version, book)
                self._price_books[key] = entry
                while len(self._price_books) > 4:
                    self._price_books.popitem(last=False)
            self._price_books.move_to_end(key)
        now = self.clock()
        return RuleContext(
            data=self.data,
            cards_by_id=self.catalogue,
            sets_by_id=self.sets,
            prices=entry[0],
            price_version=entry[1],
            economy=entry[2],
            prices_are_bundled=prices_are_bundled,
            bundled_card_price_digest=self.bundled_card_price_digest,
            now=now,
            reference_time=now - 978307200,
            seed_source=self.seed_source,
        )

    def _prepare(self, state, payload):
        from app import native_economy as economy

        if not isinstance(state, dict):
            raise HTTPException(409, "game_precondition_failed")
        ctx = self._context(payload)
        copied = deepcopy(state)
        # normalize_state validates the raw input before filling any defaults.
        economy.normalize_state(copied, ctx)
        return copied, ctx

    def warm(self, prices=None):
        """Compile public data before readiness, without constructing an account."""
        from app.native_economy import base_pack_price
        from app.native_opening import card_price_digest, oripa_shelf

        ctx = self._context(prices)
        for set_id in self.sets:
            base_pack_price(set_id, ctx)
        card_price_digest(ctx)
        oripa_shelf(ctx)

    @staticmethod
    def _quote(state, command, ctx, protected):
        from app import native_economy as economy
        from app import native_opening as opening

        kind = command.get("quote_kind", command["kind"])
        if kind == "pull_oripa":
            return opening.oripa_price(state, ctx)
        if kind == "rotation_buy":
            from app import native_rotation as rotation

            return rotation.price(command.get("card_id", ""), ctx)
        if kind == "refresh_oripa":
            return 0
        result = economy.apply(
            state, {**command, "kind": "quote", "quote_kind": kind}, ctx, protected
        )
        return result["tokens"]

    @staticmethod
    def _dispatch(state, command, ctx, protected, quoted_tokens=None):
        from app import native_economy as economy
        from app import native_opening as opening
        from app import native_rewards as rewards

        kind = command.get("kind")
        if kind == "inspect":
            return {}
        if kind == "quote":
            return {"tokens": PythonRules._quote(state, command, ctx, protected)}
        if kind == "initialize":
            rewards.initialize_gifts(state, ctx)
            opening.ensure_oripa(state, ctx)
            return {}
        if kind == "open_packs":
            return opening.open_packs(state, command, ctx)
        if kind == "refresh_oripa":
            opening.refresh_oripa(state, ctx)
            return {}
        if kind == "pull_oripa":
            return opening.pull_oripa(state, command, ctx, quoted_tokens=quoted_tokens)
        if kind in {"claim_gift", "claim_dex", "report_bonus", "set_preferences"}:
            return getattr(rewards, kind)(state, command, ctx)
        if kind == "claim_levels":
            from app import native_levels as levels

            return levels.claim_levels(state, command, ctx)
        if kind == "rotation_buy":
            from app import native_rotation as rotation

            return rotation.buy(state, command, ctx, quoted_tokens=quoted_tokens)
        if kind in {
            "valuation",
            "transfer",
            "apply_tokens",
            "buy_packs",
            "sell_spares",
            "sell_bulk",
        }:
            return economy.apply(state, command, ctx, protected, quoted_tokens=quoted_tokens)
        raise HTTPException(409, "game_precondition_failed")

    @timed("rules")
    def apply(self, state, command, *, prices=None, protected=None):
        from app.native_economy import validate_state

        prepared, ctx = self._prepare(state, prices)
        result = self._dispatch(prepared, command, ctx, protected or {})
        validate_state(prepared, ctx)
        return prepared, result, self.version

    @timed("rules")
    def apply_with_quote(self, state, command, *, prices=None, protected=None):
        from app.native_economy import validate_state

        prepared, ctx = self._prepare(state, prices)
        floor = protected or {}
        quote = self._quote(prepared, command, ctx, floor)
        result = self._dispatch(prepared, command, ctx, floor, quoted_tokens=quote)
        validate_state(prepared, ctx)
        return prepared, result, self.version, quote
