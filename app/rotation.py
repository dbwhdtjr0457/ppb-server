"""The daily rotation: cards the server itself lists on the online market.

Every day at 00:00 UTC (09:00 in Korea) yesterday's lineup comes down and twenty
new cards go up (native_rotation.lineup) with a few copies each. An account can
buy one copy of each card, so the first player online cannot take a whole lineup.
The server is the seller: no reservation, no seller wallet, and no sale or
expiry notice for anyone.
"""

import random
import time
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, text

from app import prices
from app.models import MarketListing

SOURCE = "rotation"
STOCK = 5
DAY = 86400


def round_start(now: float) -> int:
    day = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day.timestamp())


def listing_id(start: int, slot: int) -> str:
    """Listing ids are UUIDs on the wire. Deriving them from the day makes a second
    restock of the same day (another worker, a retry) collide instead of doubling up."""
    return str(uuid5(NAMESPACE_URL, f"ppb-rotation/{start}/{slot}"))


def restock(db, rules, now=None, rng=None) -> bool:
    """Put up today's lineup unless it is already there. True when it did.

    Call with no transaction open, like maintenance.expire: a check that finds
    the lineup in place reads one row and never takes SQLite's writer lock.
    """
    draw = getattr(rules, "rotation_offers", None)
    if draw is None:
        # The Swift oracle backend cannot draw from the catalogue; list nothing.
        return False
    now = time.time() if now is None else now
    start = round_start(now)
    first = listing_id(start, 0)
    present = db.get(MarketListing, first) is not None
    if not present:
        _, payload = prices.current(db, rules)
    db.rollback()
    if present:
        return False
    offers = draw(rng or random.SystemRandom(), payload)
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        if db.get(MarketListing, first) is not None:
            db.rollback()
            return False
        for row in db.scalars(
            select(MarketListing).where(
                MarketListing.source == SOURCE, MarketListing.status == "active"
            )
        ):
            row.status, row.version = "expired", row.version + 1
        from app.commerce import notify_wishlists

        for slot, (printing, unit_tokens) in enumerate(offers):
            row = MarketListing(
                id=listing_id(start, slot),
                seller=None,
                source=SOURCE,
                printing=printing,
                quantity=STOCK,
                unit_tokens=unit_tokens,
                status="active",
                version=0,
                expires_at=start + DAY,
                created_at=int(now),
            )
            db.add(row)
            notify_wishlists(db, row)
        db.commit()
        return True
    except BaseException:
        db.rollback()
        raise
