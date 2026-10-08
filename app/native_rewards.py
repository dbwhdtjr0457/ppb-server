"""Server-native equivalents of WalletStore's rewards and preferences.

The caller owns the transaction and passes a private, normalized state copy.
Bonus instances arrive hashed from the client (or the explicit legacy importer);
they are opaque identities here and must never be hashed a second time.
"""

import secrets
from collections.abc import Mapping

from fastapi import HTTPException

PERK_CAPS = {"tokenGain": 0.25, "packDiscount": 0.15, "dustBonus": 0.15, "hitOdds": 0.20}
TIER_ORDER = (
    "E",
    "C",
    "U",
    "R",
    "P",
    "RR",
    "RRR",
    "PR",
    "A",
    "K",
    "CHR",
    "AR",
    "ACE",
    "SR",
    "S",
    "SSR",
    "SAR",
    "SH",
    "HR",
    "UR",
    "BWR",
    "MA",
    "MUR",
    "FUR",
)
GIFTS = {
    "v0.4.1-apology": (213_370_000, 1),
    "v0.6.0-patch": (213_370_000, 0),
    "v0.7.0-patch": (213_370_000, 0),
    "v0.8.0-patch": (213_370_000, 0),
}
BONUS_BUDGET = 20_000_000
BONUS_PACK_CAP = 10
GRANT_MEMORY = 24


def _require(condition):
    if not condition:
        raise HTTPException(409, "game_precondition_failed")


def _plain(value):
    """Detach returned JSON from the immutable catalogue."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _claim_key(dex, step):
    return f"{dex['id']}#{step}" if dex.get("kind", "theme") == "set" else dex["id"]


def _completed_count(claimed, dexes):
    return sum(
        _claim_key(dex, len(dex.get("milestones", ())) - 1 if dex.get("kind") == "set" else 0)
        in claimed
        for dex in dexes
    )


def perks(state, ctx):
    """Sum claimed theme perks and unlocked ladder steps, then apply the caps."""
    claimed = set(state.get("claimedDex", ()))
    total = dict.fromkeys(PERK_CAPS, 0.0)
    dexes = ctx.data["dexes"]
    for dex in dexes:
        if dex["id"] in claimed:
            for perk in dex.get("reward", {}).get("perks", ()):
                total[perk["kind"]] += perk["value"]
    done = _completed_count(claimed, dexes)
    for step in ctx.data.get("ladder", ()):
        if done >= step["completed"]:
            for perk in step["perks"]:
                total[perk["kind"]] += perk["value"]
    caps = ctx.data.get("perk_caps", PERK_CAPS)
    return {kind: min(value, caps[kind]) for kind, value in total.items()}


def completions(before_owned, state, ctx):
    """Report newly filled themes, without granting or claiming their rewards.

    Match DexProgress.newlyFilled, including its non-notification of set
    milestones: a set dex has no explicit card list and was already vacuously
    filled before the opening. Claiming set milestones is a separate operation.
    """
    claimed = set(state.get("claimedDex", ()))
    cards = state.get("cards", {})
    filled = [
        dex
        for dex in ctx.data["dexes"]
        if dex["id"] not in claimed
        and all(cards.get(card, 0) > 0 for card in dex.get("cards", ()))
        and not all(card in before_owned for card in dex.get("cards", ()))
    ]
    filled.sort(key=lambda dex: (-dex["tier"], dex["id"]))
    return [
        {"dexID": dex["id"], "name": _plain(dex["name"]), "tier": dex["tier"]} for dex in filled
    ]


def _reward_json(reward):
    result = {
        "packs": reward.get("packs", 0),
        "perks": _plain(reward.get("perks", ())),
        "tokens": reward.get("tokens", 0),
        "coupons": _plain(reward.get("coupons", ())),
    }
    if reward.get("card") is not None:
        result["card"] = _plain(reward["card"])
    return result


def _grant_card(state, want, ctx):
    from app.native_economy import default_finish, usd

    tier = want["tierFloor"]
    floor = TIER_ORDER.index(tier) if tier in TIER_ORDER else None
    pool = [
        card
        for card in ctx.data["cards"]
        if floor is None or TIER_ORDER.index(card["tier"]) >= floor
    ]
    if not pool:
        return None
    fresh = [card for card in pool if state["cards"].get(card["id"], 0) == 0]
    # Swift's min(by:) retains the first entry on an equal distance. Do not
    # reorder the exported CardIndex.cards array or use a set for this pool.
    picked = min(fresh or pool, key=lambda card: abs(usd(card["id"], ctx) - want["targetUSD"]))
    card_id = picked["id"]
    printing = f"{card_id}#{default_finish(card_id, ctx)}"
    state["cards"][card_id] = state["cards"].get(card_id, 0) + 1
    printings = state.setdefault("printingCards", {})
    printings[printing] = printings.get(printing, 0) + 1
    state.setdefault("cardFirstAt", {}).setdefault(card_id, int(ctx.now))
    return card_id


def claim_dex(state, command, ctx):
    dex = next((row for row in ctx.data["dexes"] if row["id"] == command.get("dex_id", "")), None)
    _require(dex is not None)
    step = command.get("step") or 0
    claimed = set(state.get("claimedDex", ()))
    if dex.get("kind", "theme") == "set":
        milestones = dex.get("milestones", ())
        _require(type(step) is int and 0 <= step < len(milestones))
        have = sum(
            state["cards"].get(card["id"], 0) > 0
            for card in ctx.data["cards"]
            if card["set_id"] == dex["homeSet"]
        )
        _require(have >= milestones[step]["need"])
        reward = milestones[step]["reward"]
    else:
        _require(step == 0)
        _require(all(state["cards"].get(card, 0) > 0 for card in dex["cards"]))
        reward = dex["reward"]
    claim_key = _claim_key(dex, step)
    _require(claim_key not in claimed)
    state.setdefault("claimedDex", []).append(claim_key)
    if reward.get("packs", 0) > 0:
        packs = state.setdefault("packs", {})
        packs[dex["homeSet"]] = packs.get(dex["homeSet"], 0) + reward["packs"]
    if reward.get("tokens", 0) > 0:
        state["perkTokens"] = state.get("perkTokens", 0) + reward["tokens"]
    coupons = state.setdefault("coupons", [])
    for grant in reward.get("coupons", ()):
        if grant["count"] <= 0:
            continue
        existing = next(
            (
                coupon
                for coupon in coupons
                if coupon["setID"] == dex["homeSet"] and coupon["value"] == grant["value"]
            ),
            None,
        )
        if existing is None:
            coupons.append(
                {"setID": dex["homeSet"], "value": grant["value"], "left": grant["count"]}
            )
        else:
            existing["left"] += grant["count"]
    result = {"dex": _plain(dex), "step": step, "reward": _reward_json(reward)}
    if reward.get("card") is not None:
        card = _grant_card(state, reward["card"], ctx)
        if card is not None:
            result["card"] = card
    return {"dex": result}


def claim_gift(state, command, ctx):
    gift_id = command.get("gift_id")
    _require(gift_id in GIFTS)
    granted = state.setdefault("grantedGifts", [])
    if gift_id in granted:
        return {}
    # Remember ineligible gifts too: buying the first pack later must not
    # retroactively turn a new account into an update-compensation recipient.
    granted.append(gift_id)
    if state.get("packsOpened", 0) <= 0 and state.get("spentTokens", 0) <= 0:
        return {}
    tokens, packs_per_set = GIFTS[gift_id]
    state["perkTokens"] = state.get("perkTokens", 0) + tokens
    if packs_per_set:
        packs = state.setdefault("packs", {})
        for entry in ctx.data["sets"]:
            set_id = entry["id"]
            packs[set_id] = packs.get(set_id, 0) + packs_per_set
    return {}


def initialize_gifts(state, ctx):
    for gift_id in GIFTS:
        claim_gift(state, {"gift_id": gift_id}, ctx)
    return {}


def set_preferences(state, command, ctx):
    from app.native_levels import title_available

    mode = command.get("opening_mode")
    favorite = command.get("favorite_card_id")
    title = command.get("title")
    level_title = command.get("level_title")
    _require(mode in ("game", "realistic"))
    _require(title is None or level_title is None)
    _require(level_title is None or title_available(state, level_title))
    _require(favorite is None or state.get("cards", {}).get(favorite, 0) > 0)
    done = _completed_count(set(state.get("claimedDex", ())), ctx.data["dexes"])
    _require(
        title is None
        or any(
            step["completed"] == title and done >= step["completed"]
            for step in ctx.data.get("ladder", ())
        )
    )
    state["openingMode"] = mode
    # Optional Swift Codable fields are omitted rather than encoded as null.
    if favorite is None:
        state.pop("favoriteCardID", None)
    else:
        state["favoriteCardID"] = favorite
    if title is None:
        state.pop("title", None)
    else:
        state["title"] = title
    if level_title is None:
        state.pop("levelTitle", None)
    else:
        state["levelTitle"] = level_title
    return {}


def bonus_payout(sets):
    """Uniform eligible-set draw, bounded by the same fixed token budget."""
    affordable = [(set_id, price) for set_id, price in sets if 0 < price <= BONUS_BUDGET]
    if not affordable:
        return min(sets, key=lambda entry: entry[1])[0], 1
    set_id, price = affordable[secrets.randbelow(len(affordable))]
    return set_id, min(BONUS_PACK_CAP, max(1, BONUS_BUDGET // price))


def report_bonus(state, command, ctx):
    from app.native_economy import base_pack_price

    sets = [(entry["id"], base_pack_price(entry["id"], ctx)) for entry in ctx.data["sets"]]
    if not sets:
        return {}
    windows = command.get("windows") or ()
    tiers = state.setdefault("packGrantTier", {})
    instances = state.setdefault("packGrantedInstances", {})
    if not state.get("packGrantSeeded", False):
        for window in windows:
            if window["utilization"] >= 100:
                if window["instance"]:
                    instances[window["key"]] = [window["instance"]]
                else:
                    tiers[window["key"]] = 1
        state["packGrantSeeded"] = True
        return {}

    # Adopt a legacy paid marker only when that window has never had an
    # instance ledger. An existing empty ledger is deliberately different.
    for window in windows:
        key, instance = window["key"], window["instance"]
        if (
            instance
            and key not in instances
            and tiers.get(key, 0) >= 1
            and window["utilization"] >= 100
        ):
            instances[key] = [instance]

    for window in windows:
        key, instance = window["key"], window["instance"]
        if window["utilization"] < 100:
            if not instance:
                tiers.pop(key, None)
            continue
        if not instance:
            if tiers.get(key, 0) >= 1:
                continue
        else:
            paid = instances.get(key, [])
            if instance in paid:
                continue
            instances[key] = [*paid, instance][-GRANT_MEMORY:]
        tiers[key] = 1
        set_id, count = bonus_payout(sets)
        packs = state.setdefault("packs", {})
        packs[set_id] = packs.get(set_id, 0) + count
    return {}
