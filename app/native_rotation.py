"""Which cards the daily rotation lists on the online market, and at what price.

The rotation used to be a shop in the app that picked the same eight cards on
every device from the UTC date, so anyone reading the app could list every
future lineup. The server now draws the cards itself and stores them as market
listings (app/rotation.py); nothing outside this server decides the lineup.
"""

from app.native_economy import default_finish, quantized, tokens, usd

TOP = frozenset({"SAR", "UR", "HR", "SSR", "MUR", "BWR", "FUR", "SH", "MA"})
MIDDLE = frozenset({"AR", "SR", "CHR", "RRR", "K", "A", "S", "ACE", "PR"})
LOW = frozenset({"RR"})
PLAN = ((TOP, 1), (MIDDLE, 3), (LOW, 4))


def lineup(ctx, rng) -> list[str]:
    """One top card, three middle and four low, never the same card twice."""
    ordered = sorted(ctx.cards_by_id)
    picked = []
    for tiers, count in PLAN:
        pool = [card_id for card_id in ordered if ctx.cards_by_id[card_id]["tier"] in tiers]
        for _ in range(count):
            if not pool:
                break
            picked.append(pool.pop(rng.randrange(len(pool))))
    return picked


def price(card_id: str, ctx) -> int:
    """1.2x the market value on the 100-won grid.

    Players listing the same card keep room to sell under it, and a copy bought
    here never sells back to the game at a profit (that pays half the value).
    """
    base = tokens(usd(card_id, ctx, default_finish(card_id, ctx)), ctx)
    return quantized((base * 6 + 2) // 5, ctx)


def offers(ctx, rng) -> list[tuple[str, int]]:
    """(printing, unit tokens) for each card in a fresh lineup."""
    return [
        (f"{card_id}#{default_finish(card_id, ctx)}", price(card_id, ctx))
        for card_id in lineup(ctx, rng)
    ]
