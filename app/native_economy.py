"""In-process wallet arithmetic, ported from the shipped Swift rules.

PriceBook owns only an immutable catalogue/price version and derived quote caches.
It never retains an account, request, clock, or mutable wallet. The dispatcher owns
the defensive state copy and calls normalize_state once, not once per card.
"""

import math
from collections import defaultdict
from collections.abc import Mapping
from threading import RLock
from types import MappingProxyType
from uuid import UUID

from fastapi import HTTPException

TOKENS_PER_USD = 292_000.0
PACK_MARGIN = 3.4
UNKNOWN_USD = 0.05
MAX_COUNTER = 10**15
MAX_SAVE_NUMBER = ((1 << 63) - 1) // 64
FINISHES = (
    "normal",
    "holo",
    "reverseHolo",
    "patternedReverse",
    "fullArt",
    "etched",
    "radiant",
    "amazingRare",
    "rainbow",
    "gold",
    "shiny",
    "shinyFullArt",
    "aceSpec",
    "pokeBall",
    "masterBall",
    "celebrationsClassic",
    "radiantCollection",
    "prism",
    "breakFoil",
    "blackWhite",
    "megaAttack",
)
FINISH_RANK = {finish: rank for rank, finish in enumerate(FINISHES)}
COUNTERS = (
    "usedSinceInstall",
    "spentTokens",
    "refundedTokens",
    "perkTokens",
    "cardsDisenchanted",
    "packsOpened",
    "marketEarnedTokens",
    "marketSpentTokens",
)
COUNT_MAPS = (
    "cards",
    "printingCards",
    "packs",
    "cardFirstAt",
    "packPity",
    "packGrantTier",
    "claimedTodayTokensByProvider",
)
OPENING_MODES = {"game", "realistic"}
OPENING_VARIANTS = {
    "standard",
    "celebrations",
    "scarletViolet151Demigod",
    "prismaticEvolutionsGod",
    "prismaticEvolutionsDemigod",
    "blackBoltWhiteFlareGod",
    "ascendedHeroesGod",
}
LANGUAGES = {"ko", "en", "ja", "es", "fr", "pt"}


def require(condition):
    if not condition:
        raise HTTPException(409, "game_precondition_failed")


def _swift_sum(values):
    # Python 3.12 sum uses compensated floating summation; Swift reduce does not.
    result = 0.0
    for value in values:
        result += value
    return result


def round_swift(value: float) -> int:
    """Swift Double.rounded(): nearest integer, ties away from zero.

    Do not use floor(value + .5): adding .5 changes representable large integral
    doubles. modf preserves Swift's treatment on the current 10**15 token range.
    """
    if not math.isfinite(value):
        raise ValueError("Non-finite economy amount")
    fraction, integral = math.modf(value)
    return int(integral) + (1 if fraction >= 0.5 else -1 if fraction <= -0.5 else 0)


def split_printing(key: str) -> tuple[str, str]:
    card_id, separator, finish = key.rpartition("#")
    return (card_id, finish) if separator and finish in FINISH_RANK else (key, "normal")


def _cards(ctx):
    return ctx.cards_by_id


def default_finish(card_id: str, ctx) -> str:
    return _cards(ctx).get(card_id, {}).get("default_finish", "normal")


def normalize_state(state: dict, ctx) -> None:
    """Fill schema defaults and preserve both aggregate/printing inventories.

    The old WalletStore normalized each card by scanning all printing keys. This
    combines canonicalization, reconciliation and materialization in O(C + P).
    Reject malformed ledger fields rather than replacing user balances by zero.
    """
    if not isinstance(state, dict):
        raise ValueError("Invalid game state")
    # Inspect is also the operator-import trust boundary. Never let defaults
    # disguise malformed protected fields as a successfully preserved save.
    validate_state(state, ctx)
    for key in COUNTERS:
        state.setdefault(key, 0)
    for key in COUNT_MAPS:
        if key != "claimedTodayTokensByProvider":
            state.setdefault(key, {})
    for key in (
        "openingHistory",
        "claimedDex",
        "coupons",
        "grantedGifts",
        "claimedLevels",
        "rotationPurchases",
    ):
        state.setdefault(key, [])
    if not state["claimedDex"] and isinstance(state.get("completedDex"), list):
        state["claimedDex"] = list(state["completedDex"])
    state.pop("completedDex", None)
    state.setdefault("schemaVersion", 2)
    state.setdefault("openingMode", "game")
    state.setdefault("installBaselineSet", False)
    state.setdefault("lastDate", "")
    state.setdefault("packGrantedInstances", {})
    state.setdefault("packGrantSeeded", False)
    state.setdefault("language", ctx.data.get("state_defaults", {}).get("language", "en"))
    for optional in (
        "favoriteCardID",
        "title",
        "levelTitle",
        "oripa",
        "claimedTodayTokensByProvider",
    ):
        if state.get(optional) is None:
            state.pop(optional, None)
    if "oripa" in state:
        box = state["oripa"]
        legacy = "cards" not in box
        state["oripa"] = {
            "cards": list(box["slots"] if legacy else box["cards"]),
            "opened": [] if legacy else list(box.get("opened", [])),
            "serial": box.get("serial", 1),
        }
    normalize_printings(state, ctx)
    first = state["cardFirstAt"]
    for card_id, count in state["cards"].items():
        if count > 0:
            first.setdefault(card_id, int(ctx.now))


def normalize_printings(state: dict, ctx) -> None:
    canonical = defaultdict(int)
    totals = defaultdict(int)
    for key, count in state["printingCards"].items():
        if count > 0:
            card_id, finish = split_printing(key)
            canonical[f"{card_id}#{finish}"] += count
            totals[card_id] += count
    cards = state["cards"]
    for card_id, count in totals.items():
        if count > cards.get(card_id, 0):
            cards[card_id] = count
    for card_id, count in cards.items():
        missing = count - totals.get(card_id, 0)
        if missing > 0:
            canonical[f"{card_id}#{default_finish(card_id, ctx)}"] += missing
    state["printingCards"] = dict(canonical)


def validate_state(state: dict, ctx) -> None:
    if not isinstance(state, dict) or type(state.get("schemaVersion", 2)) is not int:
        raise HTTPException(409, "game_precondition_failed")
    if state.get("schemaVersion", 2) > 2:
        raise HTTPException(409, "game_precondition_failed")
    for key in COUNTERS:
        value = state.get(key, 0)
        if type(value) is not int or not 0 <= value <= MAX_SAVE_NUMBER:
            raise HTTPException(409, "game_precondition_failed")
    for key in COUNT_MAPS:
        values = state.get(key, {})
        if values is None and key == "claimedTodayTokensByProvider":
            continue
        if not isinstance(values, dict) or any(
            type(k) is not str or type(v) is not int or not 0 <= v <= MAX_SAVE_NUMBER
            for k, v in values.items()
        ):
            raise HTTPException(409, "game_precondition_failed")
    if any(card_id not in _cards(ctx) for card_id in state.get("cards", {})):
        raise HTTPException(409, "game_precondition_failed")
    if any(set_id not in ctx.sets_by_id for set_id in state.get("packs", {})):
        raise HTTPException(409, "game_precondition_failed")
    if any(split_printing(key)[0] not in _cards(ctx) for key in state.get("printingCards", {})):
        raise HTTPException(409, "game_precondition_failed")
    for key in ("installBaselineSet", "packGrantSeeded"):
        require(key not in state or type(state[key]) is bool)
    for key in ("lastDate",):
        require(key not in state or type(state[key]) is str)
    for key, allowed in (("openingMode", OPENING_MODES), ("language", LANGUAGES)):
        require(key not in state or type(state[key]) is str and state[key] in allowed)
    for key in ("claimedDex", "completedDex", "grantedGifts", "rotationPurchases"):
        require(key not in state or _strings(state[key]))
    require(
        "claimedLevels" not in state
        or isinstance(state["claimedLevels"], list)
        and all(_integer(level) for level in state["claimedLevels"])
    )
    for key, predicate in (
        ("favoriteCardID", lambda value: type(value) is str),
        ("title", _integer),
        ("levelTitle", _integer),
    ):
        require(key not in state or state[key] is None or predicate(state[key]))
    instances = state.get("packGrantedInstances", {})
    require(
        isinstance(instances, dict)
        and all(type(key) is str and _strings(values) for key, values in instances.items())
    )
    require(isinstance(state.get("coupons", []), list))
    for coupon in state.get("coupons", []):
        if (
            not isinstance(coupon, dict)
            or not _integer(coupon.get("left"))
            or not _number(coupon.get("value"))
            or not 0 <= coupon["value"] <= 1
            or type(coupon.get("setID")) is not str
        ):
            raise HTTPException(409, "game_precondition_failed")
    if state.get("oripa") is not None:
        _validate_oripa(state["oripa"], ctx)
    history = state.get("openingHistory", [])
    require(isinstance(history, list))
    for record in history:
        _validate_opening_record(record)


def _strings(value):
    return isinstance(value, list) and all(type(item) is str for item in value)


def _integer(value):
    return type(value) is int and -(1 << 63) <= value <= (1 << 63) - 1


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validate_oripa(box, ctx):
    require(isinstance(box, dict))
    # `slots` was the legitimate pre-envelope save format. Missing *both*
    # fields is corruption, not an empty box to silently materialize on import.
    cards = box.get("cards", box.get("slots"))
    require(_strings(cards) and all(card in _cards(ctx) for card in cards))
    require("serial" not in box or _integer(box["serial"]) and box["serial"] >= 0)
    if "slots" in box:
        require(_strings(box["slots"]))
    opened = box.get("opened", [])
    require(
        isinstance(opened, list)
        and all(type(index) is int and 0 <= index < len(cards) for index in opened)
    )
    require(len(opened) == len(set(opened)))


def _validate_opening_record(record):
    require(isinstance(record, dict))
    for key in ("id", "setID", "seed", "rulesVersion", "catalogueDigest"):
        require(type(record.get(key)) is str)
    try:
        UUID(record["id"])
    except ValueError:
        require(False)
    for key in ("openedAt", "hitOddsBonus"):
        require(_number(record.get(key)))
    for key in ("pityBefore", "pityAfter"):
        require(_integer(record.get(key)))
    require(type(record.get("mode")) is str and record["mode"] in OPENING_MODES)
    require(type(record.get("variant")) is str and record["variant"] in OPENING_VARIANTS)
    printings = record.get("printings")
    require(isinstance(printings, list))
    for printing in printings:
        require(
            isinstance(printing, dict)
            and type(printing.get("cardID")) is str
            and type(printing.get("finish")) is str
            and printing["finish"] in FINISH_RANK
        )
    supplement = record.get("supplement")
    require(
        isinstance(supplement, dict)
        and _integer(supplement.get("energyCount"))
        and _integer(supplement.get("codeCount"))
        and type(supplement.get("holoEnergy")) is bool
    )
    for key in ("cardPriceDate", "printingPriceDate", "priceSnapshotDigest"):
        require(key not in record or record[key] is None or type(record[key]) is str)
    quote = record.get("packQuote")
    require(isinstance(quote, dict))
    for key in ("expectedCardValueUSD", "economyFloorUSD"):
        require(_number(quote.get(key)))
    require(_integer(quote.get("baseTokens")))
    require(
        type(quote.get("basis")) is str
        and quote["basis"] in {"market", "economyFloor", "expectedValue", "fallback"}
    )
    require(quote.get("marketUSD") is None or _number(quote["marketUSD"]))
    for key in ("marketDate", "productURL"):
        require(key not in quote or quote[key] is None or type(quote[key]) is str)


def balance(state: dict) -> int:
    return max(
        0,
        state.get("usedSinceInstall", 0)
        - state.get("spentTokens", 0)
        + state.get("refundedTokens", 0)
        + state.get("perkTokens", 0)
        + state.get("marketEarnedTokens", 0)
        - state.get("marketSpentTokens", 0),
    )


def spend(state: dict, amount: int) -> bool:
    if amount <= 0 or balance(state) < amount:
        return False
    state["spentTokens"] += amount
    return True


class PriceBook:
    """Read/validate a content-addressed snapshot once; cache derived set quotes."""

    def __init__(self, ctx):
        self.version = ctx.price_version
        self.data = ctx.data
        self.cards = ctx.cards_by_id
        self._lock = RLock()
        self._quotes = {}
        card_data = ctx.prices["cardPrices"]
        pack_data = ctx.prices["packPrices"]
        representatives, printings = {}, {}

        def merge(card_id, values):
            for finish, value in values.items():
                if finish in {"default", "aggregate"}:
                    representatives[card_id] = float(value)
                else:
                    representatives[card_id] = max(representatives.get(card_id, 0), float(value))
                    if finish in FINISH_RANK:
                        printings[f"{card_id}#{finish}"] = float(value)

        for key, value in card_data.get("prices", {}).items():
            if isinstance(value, Mapping):
                merge(key, value)
            elif "#" in key:
                card_id, finish = split_printing(key)
                printings[f"{card_id}#{finish}"] = float(value)
            else:
                representatives[key] = float(value)
        for key, value in card_data.get("printingPrices", {}).items():
            if isinstance(value, Mapping):
                merge(key, value)
            else:
                card_id, finish = split_printing(key)
                printings[f"{card_id}#{finish}"] = float(value)
        for alias in ("pricesByFinish", "finishPrices"):
            for card_id, values in card_data.get(alias, {}).items():
                merge(card_id, values)
        original = set(representatives)
        for key, value in printings.items():
            card_id, _ = split_printing(key)
            if card_id not in original:
                representatives[card_id] = max(representatives.get(card_id, 0), value)
        from app.prices import currency_conversion

        self.krw_per_usd = float(currency_conversion(card_data))
        invalid_value = any(
            not math.isfinite(x) or not 0 <= x < 10_000_000
            for x in (*representatives.values(), *printings.values())
        )
        if (
            card_data.get("currency") != "USD"
            or not 1 <= self.krw_per_usd <= 100_000
            or not representatives
            or invalid_value
        ):
            raise ValueError("Invalid card prices")
        self.representatives = MappingProxyType(representatives)
        self.printings = MappingProxyType(printings)
        self.step = max(1, round_swift(100.0 * TOKENS_PER_USD / self.krw_per_usd))
        self.packs = pack_data.get("packs", {})
        self.pack_date = pack_data.get("asOf")

    def usd(self, card_id, finish=None):
        if finish is not None:
            value = self.printings.get(f"{card_id}#{finish}")
            if value is not None:
                return value
        return self.representatives.get(card_id, UNKNOWN_USD)

    def quantized(self, value):
        if self.step <= 1:
            return max(1, value)
        # Swift integer division truncates toward zero, including malformed
        # negative discounts; valid game prices are always nonnegative.
        numerator = value + self.step // 2
        quotient = numerator // self.step if numerator >= 0 else -(-numerator // self.step)
        return max(self.step, quotient * self.step)

    def tokens(self, value):
        return self.quantized(max(1, round_swift(value * TOKENS_PER_USD)))

    def pack_value(self, set_id):
        recipe = self.data["pack_rules"].get(set_id)
        if not recipe or not recipe["pack_odds"]:
            return 0.0
        pool = recipe["pool"]
        means = {}

        def mean(tier):
            if tier not in means:
                ids = pool.get(tier, ())
                means[tier] = (
                    _swift_sum(self.usd(card_id) for card_id in ids) / len(ids) if ids else 0.0
                )
            return means[tier]

        count = float(recipe["contents"]["game_card_count"])
        value = _swift_sum(
            entry["probability"] * count * mean(entry["tier"]) for entry in recipe["pack_odds"]
        )
        adjustment = 0.0
        for slot in recipe["slots"]:
            available = [entry for entry in slot["weights"] if slot["pool"].get(entry["tier"])]
            weight = sum(entry["weight"] for entry in available)
            if weight <= 0:
                continue
            parallel = slot.get("parallel")
            parallel_chance = parallel["hits"] / parallel["rolls"] if parallel else 0.0
            for entry in available:
                tier = entry["tier"]
                ids = slot["pool"][tier]
                hint = slot["finish_hints"][tier]
                actual = _swift_sum(
                    self.usd(card_id, self.cards[card_id]["finish_by_hint"][hint])
                    for card_id in ids
                ) / len(ids)
                adjustment += (
                    (actual - mean(tier))
                    * (entry["weight"] / weight)
                    * float(slot["count"])
                    * slot["standard_share"]
                    * (1 - parallel_chance)
                )
        for slot in recipe["slots"]:
            parallel = slot.get("parallel")
            if not parallel or not parallel["candidates"]:
                continue
            candidates = parallel["candidates"]
            finish = "masterBall" if slot["kind"] == "reverseHoloHit" else "pokeBall"
            actual = _swift_sum(self.usd(card["id"], finish) for card in candidates) / len(
                candidates
            )
            counted = _swift_sum(mean(card["tier"]) for card in candidates) / len(candidates)
            adjustment += (
                (actual - counted)
                * (parallel["hits"] / parallel["rolls"])
                * slot["standard_share"]
                * float(slot["count"])
            )

        def request_adjustment(requests):
            result = 0.0
            for request in requests:
                exact = request.get("exact_card_id")
                ids = (exact,) if exact else pool.get(request["tier"], ())
                if not ids:
                    continue
                delta = 0.0
                for card_id in ids:
                    card = self.cards.get(card_id)
                    if card and card["set_id"] == set_id:
                        finish = card["finish_by_hint"][request["finish_hint"]]
                        delta += self.usd(card_id, finish) - mean(card["tier"])
                result += delta / len(ids)
            return result

        specials = recipe["special_rules"]
        if specials:
            variant = specials[0]["variant"]
            chance = 1.0 / specials[0]["one_in"]
            requests = self.data["special_requests"].get(variant, ())
            if variant == "scarletViolet151Demigod" and requests:
                adjustment += (
                    _swift_sum(request_adjustment(line) for line in requests)
                    / len(requests)
                    * chance
                )
            elif variant == "prismaticEvolutionsGod":
                adjustment += request_adjustment(requests) * chance
                adjustment += (
                    request_adjustment([{"tier": "SAR", "finish_hint": "defaultForCard"}] * 3)
                    * chance
                )
            elif variant in {"blackBoltWhiteFlareGod", "ascendedHeroesGod"}:
                adjustment += request_adjustment(requests) * chance
        return max(0.0, value + adjustment)

    def quote(self, set_id):
        with self._lock:
            if set_id in self._quotes:
                return dict(self._quotes[set_id])
            value = self.pack_value(set_id)
            floor = value * PACK_MARGIN
            market = self.packs.get(set_id)
            if market and market.get("usd", 0) <= 0:
                market = None
            price = max(floor, market["usd"] if market else 0)
            basis = (
                "fallback"
                if price <= 0
                else "market"
                if market and market["usd"] >= floor
                else "economyFloor"
                if market
                else "expectedValue"
            )
            recipe = self.data["pack_rules"].get(set_id, {})
            result = {
                "expectedCardValueUSD": value,
                "economyFloorUSD": floor,
                "baseTokens": self.tokens(price)
                if price > 0
                else recipe.get("fallback_pack_tokens", 20_000_000),
                "basis": basis,
            }
            if market:
                result.update(
                    marketUSD=market["usd"], marketDate=market.get("asOf") or self.pack_date
                )
                if market.get("url") is not None:
                    result["productURL"] = market["url"]
            self._quotes[set_id] = MappingProxyType(result)
            return dict(result)


def usd(card_id, ctx, finish=None):
    return ctx.economy.usd(card_id, finish)


def quantized(value, ctx):
    return ctx.economy.quantized(value)


def tokens(value, ctx):
    return ctx.economy.tokens(value)


def pack_quote(set_id, ctx):
    return ctx.economy.quote(set_id)


def base_pack_price(set_id, ctx):
    return pack_quote(set_id, ctx)["baseTokens"]


def pack_total(state, set_id, count, ctx):
    base = base_pack_price(set_id, ctx)
    discount = ctx.perks(state)["packDiscount"]
    permanent = quantized(round_swift(float(base) * (1 - discount)), ctx) if discount > 0 else base
    remaining, total = max(0, count), 0
    coupons = sorted(
        (coupon for coupon in state["coupons"] if coupon["setID"] == set_id and coupon["left"] > 0),
        key=lambda coupon: -coupon["value"],
    )
    for coupon in coupons:
        if remaining <= 0:
            break
        used = min(remaining, coupon["left"])
        price = quantized(round_swift(float(base) * (1 - max(discount, coupon["value"]))), ctx)
        total += price * used
        remaining -= used
    return total + permanent * remaining


def _printing_index(state):
    result = defaultdict(list)
    for key, count in state["printingCards"].items():
        if count > 0:
            card_id, finish = split_printing(key)
            result[card_id].append((key, finish, count))
    return result


def _sale_plan(state, card_id, count, ctx, protected, indexed, dust_bonus):
    spares = max(0, state["cards"].get(card_id, 0) - 1)
    owned = indexed.get(card_id, ())
    if protected:
        spares = min(
            spares, sum(max(0, amount - protected.get(key, 0)) for key, _, amount in owned)
        )
    remaining = min(max(0, count), spares)
    if remaining <= 0:
        return []
    ordered = sorted(owned, key=lambda row: (usd(card_id, ctx, row[1]), FINISH_RANK[row[1]]))
    lines = []
    for key, finish, amount in ordered:
        quantity = min(max(0, amount - protected.get(key, 0)), remaining)
        if quantity > 0:
            unit = tokens(usd(card_id, ctx, finish), ctx)
            if dust_bonus > 0:
                unit = quantized(round_swift(float(unit) * (1 + dust_bonus)), ctx)
            lines.append((key, quantity, unit))
            remaining -= quantity
        if remaining <= 0:
            break
    return lines


def _apply_sale(state, card_id, lines):
    for key, count, _ in lines:
        left = state["printingCards"][key] - count
        if left > 0:
            state["printingCards"][key] = left
        else:
            del state["printingCards"][key]
    state["cards"][card_id] = max(1, state["cards"][card_id] - sum(x[1] for x in lines))


def apply(state, command, ctx, protected=None, quoted_tokens=None):
    """Mutate only the dispatcher-owned copy; return the wire result object."""
    protected = protected or {}
    kind = command["kind"]
    if kind == "inspect":
        return {}
    if kind == "valuation":
        return {
            "value_usd": max(
                0.0,
                _swift_sum(
                    usd(card_id, ctx, finish) * count
                    for key, count in state["printingCards"].items()
                    for card_id, finish in (split_printing(key),)
                    if card_id in state["cards"] and count > 0
                ),
            )
        }
    if kind == "apply_tokens":
        delta = command.get("collected_total")
        require(type(delta) is int and 0 <= delta <= MAX_COUNTER)
        state["installBaselineSet"] = True
        state["usedSinceInstall"] += delta
        gain = ctx.perks(state)["tokenGain"]
        if gain > 0:
            state["perkTokens"] += max(0, round_swift(float(delta) * gain))
        return {}
    if kind == "transfer":
        remove, add = command.get("remove") or {}, command.get("add") or {}
        changes = remove | add
        require(len(changes) <= 40)
        for key, quantity in (*remove.items(), *add.items()):
            card_id, finish = split_printing(key)
            require(
                type(quantity) is int
                and 1 <= quantity <= 1000
                and key == f"{card_id}#{finish}"
                and card_id in _cards(ctx)
            )
        credit, debit = command.get("market_credit") or 0, command.get("market_debit") or 0
        require(
            type(credit) is int
            and type(debit) is int
            and 0 <= credit <= MAX_COUNTER
            and 0 <= debit <= balance(state)
            and state["marketEarnedTokens"] <= MAX_COUNTER - credit
            and state["marketSpentTokens"] <= MAX_COUNTER - debit
        )
        for key, quantity in remove.items():
            require(state["printingCards"].get(key, 0) - quantity >= max(1, protected.get(key, 0)))
        for key, quantity in remove.items():
            card_id, _ = split_printing(key)
            state["printingCards"][key] -= quantity
            state["cards"][card_id] -= quantity
        for key, quantity in add.items():
            card_id, _ = split_printing(key)
            state["printingCards"][key] = state["printingCards"].get(key, 0) + quantity
            state["cards"][card_id] = state["cards"].get(card_id, 0) + quantity
            state["cardFirstAt"].setdefault(card_id, int(ctx.now))
        state["marketEarnedTokens"] += credit
        state["marketSpentTokens"] += debit
        return {}
    quote_kind = command.get("quote_kind") if kind == "quote" else kind
    if quote_kind == "buy_packs":
        set_id, count = command.get("set_id"), command.get("count")
        require(type(set_id) is str and type(count) is int and count > 0)
        if kind != "quote":
            require(set_id in ctx.sets_by_id and count <= 1000)
        total = (
            quoted_tokens
            if kind != "quote" and quoted_tokens is not None
            else pack_total(state, set_id, count, ctx)
        )
        if kind == "quote":
            return {"tokens": total}
        require(spend(state, total))
        state["packs"][set_id] = state["packs"].get(set_id, 0) + count
        remaining = count
        coupons = sorted(
            (
                coupon
                for coupon in state["coupons"]
                if coupon["setID"] == set_id and coupon["left"] > 0
            ),
            key=lambda coupon: -coupon["value"],
        )
        for coupon in coupons:
            take = min(remaining, coupon["left"])
            coupon["left"] -= take
            remaining -= take
            if remaining <= 0:
                break
        state["coupons"] = [coupon for coupon in state["coupons"] if coupon["left"] > 0]
        return {}
    if quote_kind in {"sell_spares", "sell_bulk"}:
        indexed = _printing_index(state)
        bonus = ctx.perks(state)["dustBonus"]
        if quote_kind == "sell_spares":
            card_id, count = command.get("card_id") or "", command.get("count") or 0
            if kind != "quote":
                require(
                    card_id in _cards(ctx)
                    and type(count) is int
                    and 1 <= count <= 1000
                    and state["cards"].get(card_id, 0) > count
                )
            plans = [(card_id, _sale_plan(state, card_id, count, ctx, protected, indexed, bonus))]
            if kind != "quote":
                require(sum(line[1] for line in plans[0][1]) == count)
        else:
            ids = command.get("card_ids") or []
            if kind != "quote":
                require(
                    len(ids) <= 1000
                    and len(set(ids)) == len(ids)
                    and all(card_id in _cards(ctx) for card_id in ids)
                )
            plans = [
                (
                    card_id,
                    _sale_plan(state, card_id, MAX_SAVE_NUMBER, ctx, protected, indexed, bonus),
                )
                for card_id in ids
            ]
        total = sum(count * unit for _, lines in plans for _, count, unit in lines)
        if kind == "quote":
            return {"tokens": total}
        copies = sum(count for _, lines in plans for _, count, _ in lines)
        kinds = sum(bool(lines) for _, lines in plans)
        for card_id, lines in plans:
            if lines:
                _apply_sale(state, card_id, lines)
        state["refundedTokens"] += total
        state["cardsDisenchanted"] += copies
        result = {"tokens": total, "sold": copies}
        if quote_kind == "sell_bulk":
            result["bulk"] = {"kinds": kinds, "copies": copies, "tokens": total}
        return result
    raise NotImplementedError(kind)
