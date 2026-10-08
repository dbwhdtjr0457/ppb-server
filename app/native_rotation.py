"""Server-native rotation market, matching the app's RotationMarket.

Eight cards a day, one copy each. The lineup comes only from the UTC date and
the card catalogue (never from prices), so the app and the server pick the same
eight even when their price snapshots differ by a day. If anything here changes,
change Sources/PokePackBar/Core/RotationMarket.swift in the app too.
"""

from datetime import UTC, datetime

from fastapi import HTTPException

from app.native_opening import MASK64, SplitMix64

TOP = frozenset({"SAR", "UR", "HR", "SSR", "MUR", "BWR", "FUR", "SH", "MA"})
MIDDLE = frozenset({"AR", "SR", "CHR", "RRR", "K", "A", "S", "ACE", "PR"})
LOW = frozenset({"RR"})
PLAN = ((TOP, 1), (MIDDLE, 3), (LOW, 4))


def _require(condition, detail="game_precondition_failed"):
    if not condition:
        raise HTTPException(409, detail)


def date_key(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")


def seed(date: str) -> int:
    """FNV-1a 64-bit of "rotation:<date>". Python's hash() changes per process."""
    value = 0xCBF29CE484222325
    for byte in f"rotation:{date}".encode():
        value ^= byte
        value = (value * 0x100000001B3) & MASK64
    return value


def lineup(date: str, ctx) -> list[str]:
    rng = SplitMix64(seed(date))
    ordered = sorted(ctx.data["cards"], key=lambda card: card["id"])
    picked = []
    for tiers, count in PLAN:
        pool = [card["id"] for card in ordered if card["tier"] in tiers]
        for _ in range(count):
            if not pool:
                break
            picked.append(pool.pop(rng.next() % len(pool)))
    return picked


def price(card_id: str, ctx) -> int:
    """1.2x the market value on the 100-won grid, above any perk-boosted sale price."""
    from app.native_economy import default_finish, quantized, tokens, usd

    base = tokens(usd(card_id, ctx, default_finish(card_id, ctx)), ctx)
    return quantized((base * 6 + 2) // 5, ctx)


def purchase_key(date: str, card_id: str) -> str:
    return f"{date}|{card_id}"


def buy(state, command, ctx, *, quoted_tokens=None):
    from app.native_economy import balance, default_finish
    from app.native_opening import _collect

    card_id = command.get("card_id")
    date = command.get("date")
    _require(type(card_id) is str and type(date) is str)
    _require(date == date_key(ctx.now), "rotation_changed")
    _require(card_id in lineup(date, ctx), "rotation_changed")
    purchases = state.setdefault("rotationPurchases", [])
    key = purchase_key(date, card_id)
    _require(key not in purchases, "rotation_changed")
    cost = price(card_id, ctx) if quoted_tokens is None else quoted_tokens
    _require(cost > 0 and balance(state) >= cost, "insufficient_balance")
    state["marketSpentTokens"] = state.get("marketSpentTokens", 0) + cost
    # Only today's purchases matter; drop earlier lineups as the app does.
    state["rotationPurchases"] = [entry for entry in purchases if entry.startswith(f"{date}|")]
    state["rotationPurchases"].append(key)
    finish = default_finish(card_id, ctx)
    card = {
        "id": card_id,
        "tier": ctx.cards_by_id[card_id]["tier"],
        "isNew": state["cards"].get(card_id, 0) == 0,
        "finish": finish,
    }
    _collect(state, [{"cardID": card_id, "finish": finish}], ctx)
    return {"rotation": card}
