import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app import catalogue, inventory, prices, social
from app.game_service import balance
from app.models import Account, CardTrade, MarketListing, Reservation, SocialProfile


def transfer(db, account, rules, remove=None, add=None, credit=0, debit=0):
    _, price_payload = prices.current(db, rules)
    state, _, _ = prices.apply(
        rules,
        account.state,
        {
            "kind": "transfer",
            "remove": remove or {},
            "add": add or {},
            "market_credit": credit,
            "market_debit": debit,
        },
        price_payload,
        inventory.floors(db, account.id),
    )
    balance(state)
    account.state = state


def trade(db, actor, command, rules):
    now = int(time.time())
    if command.action == "trade_create":
        other = social.public_target(db, command.target_id)
        if not social.are_friends(db, actor.id, other.account_id):
            raise HTTPException(403, "friends_required")
        offered = {line.printing: line.quantity for line in command.offered}
        requested = {line.printing: line.quantity for line in command.requested}
        if not offered or not requested:
            raise HTTPException(422, "both_sides_required")
        for key in offered | requested:
            catalogue.validate_printing(key, rules)
        row = CardTrade(
            id=str(uuid4()),
            sender=actor.id,
            recipient=other.account_id,
            offered=offered,
            requested=requested,
            status="pending",
            version=0,
            expires_at=now + 72 * 3600,
        )
        inventory.reserve(db, actor, row.id, offered)
        db.add(row)
        social.notify(db, other.account_id, "trade_request", row.id)
        return {"id": row.id}, {actor.id, other.account_id}
    row = db.get(CardTrade, str(command.target_id))
    if row is None or actor.id not in (row.sender, row.recipient):
        raise HTTPException(404, "trade_not_found")
    social.guard_version(row, command.target_version)
    if row.status != "pending" or row.expires_at <= now:
        raise HTTPException(409, "trade_unavailable")
    if command.action == "trade_accept":
        if actor.id != row.recipient or not social.are_friends(db, row.sender, row.recipient):
            raise HTTPException(403, "recipient_required")
        sender = db.get(Account, row.sender)
        if any(
            inventory.available(db, sender, row.id).get(k, 0) < n for k, n in row.offered.items()
        ):
            raise HTTPException(409, "offered_cards_unavailable")
        inventory.reserve(db, actor, row.id, row.requested)
        inventory.release(db, row.id)
        db.flush()
        transfer(db, sender, rules, remove=row.offered, add=row.requested)
        transfer(db, actor, rules, remove=row.requested, add=row.offered)
        row.status = "accepted"
    elif command.action == "trade_reject":
        if actor.id != row.recipient:
            raise HTTPException(403, "recipient_required")
        row.status = "rejected"
        inventory.release(db, row.id)
    elif command.action == "trade_cancel":
        if actor.id != row.sender:
            raise HTTPException(403, "sender_required")
        row.status = "cancelled"
        inventory.release(db, row.id)
    else:
        raise HTTPException(422, "invalid_trade_action")
    row.version += 1
    for account_id in (row.sender, row.recipient):
        social.notify(db, account_id, "trade_" + row.status, row.id)
    return {"id": row.id, "status": row.status}, {row.sender, row.recipient}


def market(db, actor, command, rules):
    now = int(time.time())
    if command.action == "listing_create":
        if not command.printing or not command.unit_tokens:
            raise HTTPException(422, "printing_and_price_required")
        catalogue.validate_printing(command.printing, rules)
        row = MarketListing(
            id=str(uuid4()),
            seller=actor.id,
            printing=command.printing,
            quantity=command.quantity,
            unit_tokens=command.unit_tokens,
            status="active",
            version=0,
            expires_at=now + 7 * 86400,
            created_at=now,
        )
        inventory.reserve(db, actor, row.id, {row.printing: row.quantity})
        db.add(row)
        card, finish = row.printing.rsplit("#", 1)
        for profile in db.scalars(
            select(SocialProfile).where(SocialProfile.account_id != actor.id)
        ):
            if social.blocked(db, actor.id, profile.account_id):
                continue
            account = db.get(Account, profile.account_id)
            for wish in profile.wishlist:
                count = (
                    inventory.counts(account.state).get(row.printing, 0)
                    if wish["finish"]
                    else account.state.get("cards", {}).get(card, 0)
                )
                if (
                    wish["card_id"] == card
                    and (not wish["finish"] or wish["finish"] == finish)
                    and count < wish["target"]
                ):
                    social.notify(db, profile.account_id, "wishlist_listing", row.id)
                    break
        return {"id": row.id}, {actor.id}
    row = db.get(MarketListing, str(command.target_id))
    if row is None:
        raise HTTPException(404, "listing_not_found")
    social.guard_version(row, command.target_version)
    if row.status != "active" or row.expires_at <= now:
        raise HTTPException(409, "listing_unavailable")
    if command.action == "listing_cancel":
        if row.seller != actor.id:
            raise HTTPException(403, "seller_required")
        row.status, row.version = "cancelled", row.version + 1
        inventory.release(db, row.id)
        return {"id": row.id, "status": row.status}, {actor.id}
    if (
        command.action != "listing_buy"
        or row.seller == actor.id
        or social.blocked(db, actor.id, row.seller)
    ):
        raise HTTPException(403, "purchase_not_allowed")
    if command.unit_tokens != row.unit_tokens or command.quantity > row.quantity:
        raise HTTPException(409, "price_or_stock_changed")
    total = row.unit_tokens * command.quantity
    if total > actor.balance:
        raise HTTPException(409, "insufficient_balance")
    seller = db.get(Account, row.seller)
    if inventory.available(db, seller, row.id).get(row.printing, 0) < command.quantity:
        raise HTTPException(409, "listing_stock_inconsistent")
    hold = db.get(Reservation, (seller.id, row.printing, row.id))
    if hold is None or hold.quantity != row.quantity:
        raise HTTPException(409, "listing_reservation_inconsistent")
    hold.quantity -= command.quantity
    row.quantity -= command.quantity
    if hold.quantity == 0:
        db.delete(hold)
        row.status = "sold"
    db.flush()
    transfer(db, seller, rules, remove={row.printing: command.quantity}, credit=total)
    transfer(db, actor, rules, add={row.printing: command.quantity}, debit=total)
    row.version += 1
    social.notify(db, seller.id, "listing_sold", row.id)
    social.notify(db, actor.id, "listing_bought", row.id)
    return {"id": row.id, "quantity": command.quantity, "total_tokens": total}, {
        seller.id,
        actor.id,
    }
