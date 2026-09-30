"""Native physical pack and Oripa rules.

The catalogue contains only immutable facts exported at build time. Draws execute
in-process against one price/rules snapshot, using a private RNG per request.
SplitMix64 and Swift's unbiased bounded-integer sampler preserve saved replay
seeds; Python's process-global random generator is deliberately never used.
"""

import bisect
import hashlib
import json
import math
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from fastapi import HTTPException

MASK64 = (1 << 64) - 1
SWIFT_EPOCH = 978307200
ENERGY_TYPES = (
    "grass",
    "fire",
    "water",
    "lightning",
    "psychic",
    "fighting",
    "darkness",
    "metal",
    "fairy",
)


@lru_cache(maxsize=65536)
def _price_key_order(value):
    """Foundation sortedKeys order for this ASCII price-schema key space.

    Keys are card/printing identifiers and schema field names. Foundation uses
    numeric, case-insensitive comparison with punctuation preceding numerals.
    Plain JSON sort_keys would place base1-10 before base1-2 and prices after
    priceSources, changing historical snapshot identities.
    """
    parts = []
    for part in re.findall("[0-9]+|[^0-9]", value.lower()):
        if part.isascii() and part.isdigit():
            parts.append((48, int(part)))
        else:
            parts.append((ord(part) if part.isalnum() else ord(part) - 128, 0))
    return tuple(parts), value


def _foundation_price_json(value):
    """Match JSONSerialization(.sortedKeys) for validated price snapshots.

    Foundation's NSNumber uses 17 significant digits, whereas Python's ordinary
    encoder chooses a shortest representation. This is identity metadata only;
    the live price book retains the original numeric values unchanged.
    """
    if isinstance(value, Mapping):
        return (
            "{"
            + ",".join(
                _foundation_price_json(key) + ":" + _foundation_price_json(value[key])
                for key in sorted(value, key=_price_key_order)
            )
            + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_foundation_price_json(item) for item in value) + "]"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("/", "\\/")
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite price metadata")
        return format(value, ".17g")
    if isinstance(value, int):
        return str(value)
    raise ValueError("Unsupported price metadata")


_card_digests = OrderedDict()
_card_digest_lock = threading.Lock()


def card_price_digest(ctx):
    """Preserve both legacy raw-bundle and imported-card snapshot identities."""
    if getattr(ctx, "prices_are_bundled", False):
        return ctx.bundled_card_price_digest
    with _card_digest_lock:
        if ctx.price_version in _card_digests:
            _card_digests.move_to_end(ctx.price_version)
            return _card_digests[ctx.price_version]
        try:
            digest = hashlib.sha256(
                _foundation_price_json(ctx.prices["cardPrices"]).encode()
            ).hexdigest()
        finally:
            # Repeated maps share identifiers during one serialization, but a
            # 48k-key natural-sort cache need not live as long as the server.
            # The compact digest itself is the long-lived versioned cache.
            _price_key_order.cache_clear()
        _card_digests[ctx.price_version] = digest
        while len(_card_digests) > 4:
            _card_digests.popitem(last=False)
        return digest


class SplitMix64:
    """The PackSeedGenerator and Swift 6 RandomNumberGenerator algorithms."""

    def __init__(self, seed: int):
        self.state = seed & MASK64

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        value = self.state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK64
        return value ^ (value >> 31)

    def bounded(self, upper: int) -> int:
        if not 0 < upper <= MASK64:
            raise ValueError("Invalid random upper bound")
        threshold = ((-upper) & MASK64) % upper
        product = self.next() * upper
        while (product & MASK64) < threshold:
            product = self.next() * upper
        return product >> 64

    def unit(self) -> float:
        # Swift Double.random(0..<1) uses the low 53 bits, not the high bits.
        return (self.next() & ((1 << 53) - 1)) * (2.0**-53)

    def shuffle(self, values: list) -> None:
        for offset in range(len(values) - 1):
            other = offset + self.bounded(len(values) - offset)
            values[offset], values[other] = values[other], values[offset]


def _require(condition: bool) -> None:
    if not condition:
        raise HTTPException(409, "game_precondition_failed")


def _rounded(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _weights(base, bonus: float, ranks) -> list[tuple[str, int]]:
    scaled = [(entry["tier"], entry["weight"] * 100) for entry in base]
    if bonus <= 0:
        return scaled
    above = sum(weight for tier, weight in scaled if ranks[tier] > ranks["R"])
    rare = next((weight for tier, weight in scaled if tier == "R"), None)
    if rare is None or above <= 0:
        return scaled
    moved = float(rare) * bonus
    return [
        (tier, max(1, _rounded(weight - moved)))
        if tier == "R"
        else (tier, _rounded(weight + moved * weight / above))
        if ranks[tier] > ranks["R"]
        else (tier, weight)
        for tier, weight in scaled
    ]


def _weighted_tier(weights, pool, rng: SplitMix64) -> str:
    candidates = [(tier, weight) for tier, weight in weights if pool.get(tier)]
    if not candidates:
        return "R"
    roll = rng.bounded(sum(weight for _, weight in candidates))
    for tier, weight in candidates:
        roll -= weight
        if roll < 0:
            return tier
    return candidates[-1][0]


def _request(tier: str, *, exact=None, candidates=None, hint="defaultForCard") -> dict:
    return {
        "tier": tier,
        "exact_card_id": exact,
        "candidate_card_ids": candidates,
        "finish_hint": hint,
    }


def _standard_requests(recipe, data, rng, pity, hit_odds, excluded=()):
    requests, hits = [], []
    ranks = data["tier_ranks"]
    for slot in recipe["slots"]:
        kind = slot["kind"]
        if kind in excluded:
            continue
        pool = slot["pool"]
        weights = (
            [(entry["tier"], entry["weight"]) for entry in slot["weights"]]
            if kind == "radiantCollectionHigh"
            else _weights(slot["weights"], hit_odds, ranks)
        )
        for _ in range(slot["count"]):
            parallel = slot.get("parallel")
            if parallel and rng.bounded(parallel["rolls"]) < parallel["hits"]:
                candidates = parallel["candidates"]
                if candidates:
                    picked = candidates[rng.bounded(len(candidates))]
                    requests.append(
                        _request(picked["tier"], exact=picked["id"], hint=parallel["finish_hint"])
                    )
                    continue
            if kind == "rare" and recipe["base_variant"] != "celebrations":
                selected = weights
                if pity >= data["pity_threshold"]:
                    above = [
                        (tier, weight) for tier, weight in weights if tier != "R" and pool.get(tier)
                    ]
                    if above:
                        selected = above
                tier = _weighted_tier(selected, pool, rng)
                hits.append(tier)
            else:
                tier = _weighted_tier(weights, pool, rng)
            # None means fallback through the full set. An empty physical sheet
            # is intentionally distinct: it cannot borrow from another sheet.
            candidates = pool.get(tier) if slot["uses_restricted_pool"] else None
            requests.append(_request(tier, candidates=candidates, hint=slot["finish_hints"][tier]))
    for tier in hits:
        pity = 0 if ranks[tier] > ranks["R"] else pity + 1
    return requests, pity


def _pick(ids, used, rng):
    if not ids:
        return None
    fresh = [card_id for card_id in ids if card_id not in used]
    source = fresh or ids
    return source[rng.bounded(len(source))]


def draw_pack(set_id, ctx, owned, seed, *, pity=0, mode="game", hit_odds=0.0, forced_variant=None):
    """Pure replay helper. Seeds/forced variants are never accepted over HTTP."""
    data, cards = ctx.data, ctx.cards_by_id
    recipe = data["pack_rules"][set_id]
    pool, rng = recipe["pool"], SplitMix64(seed)
    if mode == "realistic":
        hit_odds, pity = 0.0, 0
    variant = recipe["base_variant"]
    if forced_variant is not None:
        variant = forced_variant
    elif recipe["special_rules"]:
        roll = rng.bounded(recipe["special_rules"][0]["one_in"])
        if roll < len(recipe["special_rules"]):
            variant = recipe["special_rules"][roll]["variant"]
    if variant in ("scarletViolet151Demigod", "prismaticEvolutionsDemigod"):
        requests, pity = _standard_requests(
            recipe, data, rng, pity, hit_odds, ("reverseHolo", "reverseHoloHit", "rare")
        )
        if variant == "scarletViolet151Demigod":
            lines = data["special_requests"][variant]
            requests.extend(lines[rng.bounded(len(lines))])
        else:
            requests.extend(_request("SAR") for _ in range(3))
        pity = 0
    elif variant not in ("standard", "celebrations"):
        requests = data["special_requests"][variant]
        pity = 0
    else:
        requests, pity = _standard_requests(recipe, data, rng, pity, hit_odds)
    if variant not in ("standard", "celebrations"):
        for request in requests:
            exact = request.get("exact_card_id")
            _require(
                cards.get(exact, {}).get("set_id") == set_id
                if exact
                else bool(pool.get(request["tier"]))
            )
    picked, used = [], set()
    for request in requests:
        exact = request.get("exact_card_id")
        if exact and cards.get(exact, {}).get("set_id") == set_id:
            card_id = exact
        elif request.get("candidate_card_ids") is not None:
            card_id = _pick(request["candidate_card_ids"], used, rng)
        else:
            card_id = None
            for tier in data["fallback_chains"][request["tier"]]:
                if pool.get(tier):
                    card_id = _pick(pool[tier], used, rng)
                    break
        if card_id is None:
            continue
        used.add(card_id)
        entry, hint = cards[card_id], request["finish_hint"]
        picked.append(
            {
                "card": {
                    "id": card_id,
                    "tier": entry["tier"],
                    "isNew": card_id not in owned,
                    "finish": entry["finish_by_hint"][hint],
                },
                "finishHint": hint,
            }
        )
    energy = recipe.get("reverse_energy")
    if energy and rng.bounded(energy["one_in"]) == 0:
        reverse = next(
            (i for i, slot in enumerate(picked) if slot["card"]["finish"] == "reverseHolo"), None
        )
        if reverse is not None:
            style = recipe["energy_style"]
            types = ENERGY_TYPES if style == "sm" else ENERGY_TYPES[:-1]
            picked[reverse] = {
                "card": {
                    "id": f"supplement-energy-{style}-{types[rng.bounded(len(types))]}",
                    "tier": "E",
                    "isNew": False,
                    "finish": energy["finish"],
                },
                "finishHint": "defaultForCard",
            }
    _require(len(picked) == recipe["contents"]["game_card_count"])
    return {"slotResults": picked, "variant": variant}, 0 if mode == "realistic" else pity


def _stable_index(set_id, cards, upper):
    value = 14695981039346656037
    for byte in "|".join([set_id] + [card["id"] for card in cards]).encode():
        value = ((value ^ byte) * 1099511628211) & MASK64
    return value % upper


def _supplement(set_id, recipe, opened):
    cards = [slot["card"] for slot in opened["slotResults"]]
    marker = recipe["vstar_marker"] and cards and _stable_index(f"vstar-{set_id}", cards, 4) == 0
    return {
        "energyCount": 0 if marker else recipe["contents"]["energy_card_count"],
        "holoEnergy": set_id == "cel30" or opened["variant"] == "blackBoltWhiteFlareGod",
        "codeCount": recipe["contents"]["code_card_count"],
    }


def _collect(state, printings, ctx):
    from app.native_rewards import completions

    before = set(state["cards"])
    now = int(getattr(ctx, "now", time.time()))
    for printing in printings:
        card_id = printing["cardID"]
        key = f"{card_id}#{printing['finish']}"
        state["cards"][card_id] = state["cards"].get(card_id, 0) + 1
        state["printingCards"][key] = state["printingCards"].get(key, 0) + 1
        state["cardFirstAt"].setdefault(card_id, now)
    return completions(before, state, ctx) if printings else []


def open_packs(state, command, ctx):
    from app.native_economy import pack_quote
    from app.native_rewards import perks

    set_id, count = command.get("set_id"), command.get("count")
    _require(set_id in ctx.data["pack_rules"] and type(count) is int and 1 <= count <= 1000)
    _require(state["packs"].get(set_id, 0) >= count)
    mode, owned = state["openingMode"], set(state["cards"])
    hit_odds = 0.0 if mode == "realistic" else perks(state, ctx)["hitOdds"]
    pity = 0 if mode == "realistic" else state["packPity"].get(set_id, 0)
    quote = pack_quote(set_id, ctx)
    history_limit = ctx.data["history_limit"]
    recipe = ctx.data["pack_rules"][set_id]
    prices = ctx.prices["cardPrices"]
    snapshot_digest = card_price_digest(ctx)
    printing_date = (prices.get("printingPriceSnapshot") or {}).get("asOf")
    packs, printings, records = [], [], []
    seed_source = getattr(ctx, "seed_source", None) or (lambda: secrets.randbits(64))
    for offset in range(count):
        seed, before = seed_source(), pity
        opened, pity = draw_pack(set_id, ctx, owned, seed, pity=pity, mode=mode, hit_odds=hit_odds)
        pack_printings = [
            {"cardID": slot["card"]["id"], "finish": slot["card"]["finish"]}
            for slot in opened["slotResults"]
        ]
        if offset >= count - history_limit:
            record = {
                "id": str(uuid.uuid4()).upper(),
                "openedAt": getattr(ctx, "reference_time", time.time() - SWIFT_EPOCH),
                "setID": set_id,
                "seed": str(seed),
                "rulesVersion": ctx.data["opening_rules_version"],
                "catalogueDigest": ctx.data["catalogue_digest"],
                "mode": mode,
                "hitOddsBonus": hit_odds,
                "pityBefore": before,
                "pityAfter": pity,
                "variant": opened["variant"],
                "printings": pack_printings,
                "supplement": _supplement(set_id, recipe, opened),
                "priceSnapshotDigest": snapshot_digest,
                "packQuote": dict(quote),
            }
            if prices.get("asOf") is not None:
                record["cardPriceDate"] = prices["asOf"]
            if printing_date is not None:
                record["printingPriceDate"] = printing_date
            records.append(record)
        packs.append(opened)
        for printing in pack_printings:
            if not printing["cardID"].startswith("supplement-energy-"):
                printings.append(printing)
                owned.add(printing["cardID"])
    # Only mutate after the complete batch passed physical-card-count checks.
    remaining = state["packs"][set_id] - count
    if remaining:
        state["packs"][set_id] = remaining
    else:
        state["packs"].pop(set_id, None)
    state["packsOpened"] += count
    completed = _collect(state, printings, ctx)
    if mode == "game":
        if pity:
            state["packPity"][set_id] = pity
        else:
            state["packPity"].pop(set_id, None)
    state["openingHistory"] = (state["openingHistory"] + records)[-history_limit:]
    return {"packs": {"packs": packs, "completions": completed}}


@dataclass(frozen=True)
class OripaShelf:
    keyed: tuple
    descending_keys: tuple
    base_usd: float

    def window(self, low, high, need):
        lower = bisect.bisect_left(self.descending_keys, -high)
        upper = bisect.bisect_right(self.descending_keys, -low)
        while upper - lower < need and (lower > 0 or upper < len(self.keyed)):
            if lower > 0:
                lower -= 1
            if upper < len(self.keyed):
                upper += 1
        return self.keyed[lower:upper]


_shelves = OrderedDict()
_shelf_lock = threading.Lock()


def oripa_shelf(ctx):
    from app.native_economy import usd

    key = (ctx.data["catalogue_digest"], ctx.price_version)
    with _shelf_lock:
        if key in _shelves:
            _shelves.move_to_end(key)
            return _shelves[key]
        ranks = ctx.data["tier_ranks"]
        keyed = tuple(
            sorted(
                (
                    (entry["id"], usd(entry["id"], ctx))
                    for entry in ctx.data["cards"]
                    if ranks[entry["tier"]] >= ranks["RR"]
                ),
                key=lambda item: (-item[1], item[0]),
            )
        )
        shelf = OripaShelf(
            keyed, tuple(-value for _, value in keyed), keyed[len(keyed) // 2][1] if keyed else 0
        )
        _shelves[key] = shelf
        while len(_shelves) > 4:
            _shelves.popitem(last=False)
        return shelf


def make_oripa_box(state, ctx, *, rng=None):
    shelf = oripa_shelf(ctx)
    serial = (state.get("oripa") or {}).get("serial", 0) + 1
    if not shelf.keyed:
        return {"cards": [], "opened": [], "serial": serial}
    rng = rng or SplitMix64(secrets.randbits(64))
    low, high = 25.0 * shelf.base_usd, 500.0 * shelf.base_usd
    if low <= 0 or high <= low:
        headline = shelf.keyed[0]
    else:
        target = low * math.pow(high / low, rng.unit())
        candidates = shelf.window(target * 0.94, target * 1.06, 1)
        headline = candidates[rng.bounded(len(candidates))]
    cards, taken = [headline[0]], {headline[0]}
    for ratio, count, spread in ((0.180, 4, 0.06), (0.0367, 10, 0.12), (0.0240, 25, 0.12)):
        target = headline[1] * ratio
        free = [
            card_id
            for card_id, _ in shelf.window(target * (1 - spread), target * (1 + spread), count * 3)
            if card_id not in taken
        ]
        fresh = [card_id for card_id in free if state["cards"].get(card_id, 0) == 0]
        spare = [card_id for card_id in free if state["cards"].get(card_id, 0) > 0]
        for _ in range(count):
            source = fresh or spare
            if not source:
                break
            card_id = source.pop(rng.bounded(len(source)))
            taken.add(card_id)
            cards.append(card_id)
    rng.shuffle(cards)
    return {"cards": cards, "opened": [], "serial": serial}


def ensure_oripa(state, ctx):
    box = state.get("oripa")
    if box and len(box["cards"]) == 40 and len(box["opened"]) < len(box["cards"]):
        return box
    state["oripa"] = make_oripa_box(state, ctx, rng=getattr(ctx, "rng", None))
    return state["oripa"]


def refresh_oripa(state, ctx):
    state["oripa"] = make_oripa_box(state, ctx, rng=getattr(ctx, "rng", None))
    return {}


def oripa_price(state, ctx):
    from app.native_economy import quantized, tokens, usd
    from app.native_rewards import perks

    box = ensure_oripa(state, ctx)
    opened = set(box["opened"])
    left = [card_id for offset, card_id in enumerate(box["cards"]) if offset not in opened]
    if not left:
        return 0
    # Python 3.12 sum(float) compensates rounding; Swift's reduce is a plain
    # left fold. Keep the original rounding boundary before currency quantizing.
    total = 0.0
    for card_id in left:
        total += usd(card_id, ctx)
    mean = total / len(left)
    base = tokens(mean * 2.5, ctx) if mean > 0 else 30_000_000
    discount = perks(state, ctx)["packDiscount"]
    return quantized(_rounded(base * (1 - discount)), ctx) if discount > 0 else base


def pull_oripa(state, command, ctx, *, quoted_tokens=None):
    from app.native_economy import default_finish, spend

    box = ensure_oripa(state, ctx)
    envelope = command.get("envelope")
    _require(
        type(envelope) is int
        and 0 <= envelope < len(box["cards"])
        and envelope not in box["opened"]
    )
    price = oripa_price(state, ctx) if quoted_tokens is None else quoted_tokens
    _require(spend(state, price))
    card_id = box["cards"][envelope]
    card = {
        "id": card_id,
        "tier": ctx.cards_by_id[card_id]["tier"],
        "isNew": state["cards"].get(card_id, 0) == 0,
        "finish": default_finish(card_id, ctx),
    }
    box["opened"].append(envelope)
    completed = _collect(state, [{"cardID": card_id, "finish": card["finish"]}], ctx)
    return {"oripa": {"card": card, "completions": completed}}
