import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app import catalogue, inventory, offers, prices, rotation, social
from app.game_service import balance
from app.models import (
    Account,
    CardTrade,
    MarketListing,
    MarketPurchase,
    Reservation,
    SocialProfile,
)


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


def trade_lines(command, rules):
    """Cards on both sides, with money allowed on one side.

    Card plus money for a better card is common in real trading. Money may stand
    in for one side's cards, but at least one card must change hands: a money-only
    trade would just move balances between accounts.
    """
    offered = {line.printing: line.quantity for line in command.offered}
    requested = {line.printing: line.quantity for line in command.requested}
    give, get = command.offered_tokens, command.requested_tokens
    if give and get:
        raise HTTPException(422, "money_on_both_sides")
    if not offered and not requested:
        raise HTTPException(422, "cards_required")
    if not (offered or give) or not (requested or get):
        raise HTTPException(422, "both_sides_required")
    for key in offered | requested:
        catalogue.validate_printing(key, rules)
    return offered, requested, give, get


def check_requested(db, owner, requested):
    """A friend who shares spare cards can only be asked for those spares.

    Without the check a proposal could ask for cards the friend does not have and fail only
    when they try to accept, after the sender's cards were held for 72 hours. Owners who keep
    their spares private are not checked, so repeated proposals cannot reveal what they own.
    """
    if not social.profile(db, owner.id).trade_list_public:
        return
    free = inventory.available(db, owner)
    if any(free.get(key, 0) < quantity for key, quantity in requested.items()):
        raise HTTPException(409, "requested_cards_unavailable")


def open_trade(db, sender, recipient, terms, now, counter_of=None):
    offered, requested, give, get = terms
    # The sender's cards are held; money is not (that would need a hold in the
    # game rules). It is checked here and again when the trade is accepted.
    if give > sender.balance:
        raise HTTPException(409, "insufficient_balance")
    row = CardTrade(
        id=str(uuid4()),
        sender=sender.id,
        recipient=recipient.id,
        offered=offered,
        requested=requested,
        offered_tokens=give,
        requested_tokens=get,
        status="pending",
        version=0,
        expires_at=now + 72 * 3600,
        counter_of=counter_of,
    )
    if offered:
        inventory.reserve(db, sender, row.id, offered)
    db.add(row)
    return row


def trade(db, actor, command, rules):
    now = int(time.time())
    if command.action == "trade_create":
        other = social.public_target(db, command.target_id)
        if not social.are_friends(db, actor.id, other.account_id):
            raise HTTPException(403, "friends_required")
        terms = trade_lines(command, rules)
        recipient = db.get(Account, other.account_id)
        check_requested(db, recipient, terms[1])
        row = open_trade(db, actor, recipient, terms, now)
        social.notify(db, other.account_id, "trade_request", row.id)
        return {"id": row.id}, {actor.id, other.account_id}
    row = db.get(CardTrade, str(command.target_id))
    if row is None or actor.id not in (row.sender, row.recipient):
        raise HTTPException(404, "trade_not_found")
    social.guard_version(row, command.target_version)
    if row.status != "pending" or row.expires_at <= now:
        raise HTTPException(409, "trade_unavailable")
    if command.action == "trade_counter":
        # The recipient answers with changed cards instead of only accepting or rejecting.
        # The original closes as countered, releasing the sender's held cards, and a new
        # proposal goes back the other way.
        if actor.id != row.recipient or not social.are_friends(db, row.sender, row.recipient):
            raise HTTPException(403, "recipient_required")
        terms = trade_lines(command, rules)
        sender = db.get(Account, row.sender)
        inventory.release(db, row.id)
        db.flush()
        check_requested(db, sender, terms[1])
        counter = open_trade(db, actor, sender, terms, now, counter_of=row.id)
        row.status = "countered"
        row.version += 1
        social.notify(db, row.sender, "trade_countered", counter.id)
        return {"id": counter.id, "status": "pending"}, {row.sender, row.recipient}
    if command.action == "trade_accept":
        if actor.id != row.recipient or not social.are_friends(db, row.sender, row.recipient):
            raise HTTPException(403, "recipient_required")
        sender = db.get(Account, row.sender)
        if any(
            inventory.available(db, sender, row.id).get(k, 0) < n for k, n in row.offered.items()
        ):
            raise HTTPException(409, "offered_cards_unavailable")
        # The money was not held, so either side may have spent it since.
        if row.offered_tokens > sender.balance:
            raise HTTPException(409, "offered_tokens_unavailable")
        if row.requested_tokens > actor.balance:
            raise HTTPException(409, "insufficient_balance")
        if row.requested:
            inventory.reserve(db, actor, row.id, row.requested)
        inventory.release(db, row.id)
        db.flush()
        give, get = row.offered_tokens, row.requested_tokens
        transfer(db, sender, rules, remove=row.offered, add=row.requested, credit=get, debit=give)
        transfer(db, actor, rules, remove=row.requested, add=row.offered, credit=give, debit=get)
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
        # Both sides keep the line in their history; only the other side has news.
        social.notify(db, account_id, "trade_" + row.status, row.id, read=account_id == actor.id)
    return {"id": row.id, "status": row.status}, {row.sender, row.recipient}


def notify_wishlists(db, row):
    """Tell everyone still short of this card on their wishlist that it is for sale."""
    card, finish = row.printing.rsplit("#", 1)
    for profile in db.scalars(select(SocialProfile).where(SocialProfile.account_id != row.seller)):
        if row.seller is not None and social.blocked(db, row.seller, profile.account_id):
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


def buy_rotation(db, actor, row, command, rules, now):
    """One copy of a rotation card. The payment goes to nobody.

    Its price never changes during the day, so a buyer is not turned away because
    someone else bought a copy since they loaded the market (no version check).
    """
    if command.action != "listing_buy":
        raise HTTPException(403, "seller_required")
    if row.status != "active" or row.expires_at <= now:
        raise HTTPException(409, "listing_unavailable")
    if command.unit_tokens != row.unit_tokens:
        raise HTTPException(409, "price_or_stock_changed")
    if command.quantity != 1 or db.get(MarketPurchase, (row.id, actor.id)) is not None:
        raise HTTPException(409, "rotation_limit_reached")
    if row.unit_tokens > actor.balance:
        raise HTTPException(409, "insufficient_balance")
    row.quantity -= 1
    if row.quantity == 0:
        row.status = "sold"
    row.version += 1
    db.add(MarketPurchase(listing_id=row.id, account_id=actor.id, created_at=now))
    db.flush()
    transfer(db, actor, rules, add={row.printing: 1}, debit=row.unit_tokens)
    social.notify(db, actor.id, "listing_bought", row.id, read=True)
    return {"id": row.id, "quantity": 1, "total_tokens": row.unit_tokens}, {actor.id}


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
        notify_wishlists(db, row)
        return {"id": row.id}, {actor.id}
    row = db.get(MarketListing, str(command.target_id))
    if row is None:
        raise HTTPException(404, "listing_not_found")
    if row.source == rotation.SOURCE:
        return buy_rotation(db, actor, row, command, rules, now)
    social.guard_version(row, command.target_version)
    if row.status != "active" or row.expires_at <= now:
        raise HTTPException(409, "listing_unavailable")
    if command.action == "listing_cancel":
        if row.seller != actor.id:
            raise HTTPException(403, "seller_required")
        row.status, row.version = "cancelled", row.version + 1
        inventory.release(db, row.id)
        offers.close(db, row)
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
    settle(db, row, seller, actor, command.quantity, total, rules)
    social.notify(db, seller.id, "listing_sold", row.id)
    social.notify(db, actor.id, "listing_bought", row.id, read=True)
    return {"id": row.id, "quantity": command.quantity, "total_tokens": total}, {
        seller.id,
        actor.id,
    }


def settle(db, row, seller, buyer, quantity, total, rules):
    """Move `quantity` cards of a player's listing to the buyer for `total` tokens.

    Shared by buying at the asking price and by accepting a price offer. Pending
    offers the remaining stock can no longer fill are closed.
    """
    if inventory.available(db, seller, row.id).get(row.printing, 0) < quantity:
        raise HTTPException(409, "listing_stock_inconsistent")
    hold = db.get(Reservation, (seller.id, row.printing, row.id))
    if hold is None or hold.quantity != row.quantity:
        raise HTTPException(409, "listing_reservation_inconsistent")
    hold.quantity -= quantity
    row.quantity -= quantity
    if hold.quantity == 0:
        db.delete(hold)
        row.status = "sold"
    db.flush()
    transfer(db, seller, rules, remove={row.printing: quantity}, credit=total)
    transfer(db, buyer, rules, add={row.printing: quantity}, debit=total)
    row.version += 1
    offers.close(db, row)
