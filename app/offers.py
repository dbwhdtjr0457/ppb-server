"""Price offers on players' market listings.

A buyer offers a price per card below the asking price, but at least half of it:
under that the seller is better off selling to the game, which pays half the
market value. The seller accepts, which runs the sale at the offered price, or
rejects. A buyer keeps one open offer per listing; sending another replaces it.

An offer closes after 48 hours, or once its listing ends or no longer has the
stock to fill it. The buyer's money is not held (that would need a hold in the
game rules), so acceptance checks that the buyer can still pay.

Rotation listings sell at a fixed price for the day and take no offers.
"""

import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app import social
from app.models import Account, MarketListing, MarketOffer

HOURS = 48


def close(db, listing):
    """Close the pending offers this listing can no longer fill and tell their buyers."""
    for row in db.scalars(
        select(MarketOffer).where(
            MarketOffer.listing_id == listing.id, MarketOffer.status == "pending"
        )
    ):
        if listing.status == "active" and row.quantity <= listing.quantity:
            continue
        row.status, row.version = "expired", row.version + 1
        social.notify(db, row.buyer, "offer_expired", row.id)


def expire(db, now):
    """Close offers past their 48 hours. Runs inside maintenance.expire's lock."""
    for row in db.scalars(
        select(MarketOffer).where(MarketOffer.status == "pending", MarketOffer.expires_at <= now)
    ):
        row.status, row.version = "expired", row.version + 1
        social.notify(db, row.buyer, "offer_expired", row.id)


def create(db, actor, command, now):
    listing = db.get(MarketListing, str(command.target_id))
    if listing is None:
        raise HTTPException(404, "listing_not_found")
    if listing.status != "active" or listing.expires_at <= now:
        raise HTTPException(409, "listing_unavailable")
    if listing.seller is None:
        raise HTTPException(409, "offer_not_accepted")
    if listing.seller == actor.id or social.blocked(db, actor.id, listing.seller):
        raise HTTPException(403, "offer_not_allowed")
    unit = command.unit_tokens
    if not unit:
        raise HTTPException(422, "price_required")
    if unit >= listing.unit_tokens:
        raise HTTPException(409, "offer_not_below_price")
    if unit * 2 < listing.unit_tokens:
        raise HTTPException(409, "offer_too_low")
    if command.quantity > listing.quantity:
        raise HTTPException(409, "price_or_stock_changed")
    if unit * command.quantity > actor.balance:
        raise HTTPException(409, "insufficient_balance")
    for old in db.scalars(
        select(MarketOffer).where(
            MarketOffer.listing_id == listing.id,
            MarketOffer.buyer == actor.id,
            MarketOffer.status == "pending",
        )
    ):
        old.status, old.version = "replaced", old.version + 1
    row = MarketOffer(
        id=str(uuid4()),
        listing_id=listing.id,
        buyer=actor.id,
        quantity=command.quantity,
        unit_tokens=unit,
        status="pending",
        version=0,
        expires_at=min(now + HOURS * 3600, listing.expires_at),
        created_at=now,
    )
    db.add(row)
    social.notify(db, listing.seller, "offer_received", row.id)
    return {"id": row.id}, {actor.id}


def pending(row, now):
    if row.status != "pending" or row.expires_at <= now:
        raise HTTPException(409, "offer_unavailable")


def handle(db, actor, command, rules):
    now = int(time.time())
    if command.action == "offer_create":
        return create(db, actor, command, now)
    row = db.get(MarketOffer, str(command.target_id))
    if row is None:
        raise HTTPException(404, "offer_not_found")
    listing = db.get(MarketListing, row.listing_id)
    if command.action == "offer_cancel":
        if row.buyer != actor.id:
            raise HTTPException(403, "buyer_required")
        pending(row, now)
        row.status, row.version = "cancelled", row.version + 1
        return {"id": row.id, "status": row.status}, {actor.id}
    if listing.seller != actor.id:
        raise HTTPException(403, "seller_required")
    social.guard_version(row, command.target_version)
    pending(row, now)
    if command.action == "offer_reject":
        row.status, row.version = "rejected", row.version + 1
        social.notify(db, row.buyer, "offer_rejected", row.id)
        return {"id": row.id, "status": row.status}, {actor.id}
    if command.action != "offer_accept":
        raise HTTPException(422, "invalid_offer_action")
    if listing.status != "active" or listing.expires_at <= now:
        raise HTTPException(409, "listing_unavailable")
    if row.quantity > listing.quantity:
        raise HTTPException(409, "offer_quantity_unavailable")
    buyer = db.get(Account, row.buyer)
    if social.blocked(db, actor.id, buyer.id):
        raise HTTPException(403, "purchase_not_allowed")
    total = row.unit_tokens * row.quantity
    if total > buyer.balance:
        raise HTTPException(409, "buyer_cannot_pay")
    from app.commerce import settle

    row.status, row.version = "accepted", row.version + 1
    settle(db, listing, actor, buyer, row.quantity, total, rules)
    social.notify(db, buyer.id, "offer_accepted", row.id)
    social.notify(db, actor.id, "listing_sold", listing.id, read=True)
    result = {"id": row.id, "status": row.status, "quantity": row.quantity, "total_tokens": total}
    return result, {actor.id, buyer.id}
