"""Which cards the daily rotation lists on the online market, and at what price.

The rotation used to be a shop in the app that picked the same eight cards on
every device from the UTC date, so anyone reading the app could list every
future lineup. The server now draws the cards itself and stores them as market
listings (app/rotation.py); nothing outside this server decides the lineup.

Twenty cards a day: 4 top, 8 middle and 8 low tier. Two rules keep a lineup
varied. No two cards share a set, so one big set cannot fill the day. And each
tier group is split into price bands, one card from each, so a lineup always
has cheap cards next to the chase cards instead of whatever a uniform draw hits.
"""

from app.native_economy import default_finish, quantized, tokens, usd

TOP = frozenset({"SAR", "UR", "HR", "SSR", "MUR", "BWR", "FUR", "SH", "MA"})
MIDDLE = frozenset({"AR", "SR", "CHR", "RRR", "K", "A", "S", "ACE", "PR"})
LOW = frozenset({"RR"})
PLAN = ((TOP, 4), (MIDDLE, 8), (LOW, 8))


def market(card_id: str, ctx) -> int:
    return tokens(usd(card_id, ctx, default_finish(card_id, ctx)), ctx)


def lineup(ctx, rng) -> list[str]:
    """Per tier group, one card from each price band, never two from one set."""
    cards = ctx.cards_by_id
    used_sets = set()
    picked = []
    for tiers, count in PLAN:
        pool = sorted(
            (card_id for card_id, card in cards.items() if card["tier"] in tiers),
            key=lambda card_id: (market(card_id, ctx), card_id),
        )
        for band in range(count):
            lower, upper = band * len(pool) // count, (band + 1) * len(pool) // count
            choices = [c for c in pool[lower:upper] if cards[c]["set_id"] not in used_sets]
            # A band whose sets are all taken falls back to any free card in the group.
            choices = choices or [c for c in pool if cards[c]["set_id"] not in used_sets]
            if not choices:
                break
            choice = choices[rng.randrange(len(choices))]
            used_sets.add(cards[choice]["set_id"])
            picked.append(choice)
    return picked


def price(card_id: str, ctx) -> int:
    """1.2x the market value on the 100-won grid.

    Players listing the same card keep room to sell under it, and a copy bought
    here never sells back to the game at a profit (that pays half the value).
    """
    return quantized((market(card_id, ctx) * 6 + 2) // 5, ctx)


def offers(ctx, rng) -> list[tuple[str, int]]:
    """(printing, unit tokens) for each card in a fresh lineup."""
    return [
        (f"{card_id}#{default_finish(card_id, ctx)}", price(card_id, ctx))
        for card_id in lineup(ctx, rng)
    ]
