import secrets
import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from app import catalogue, inventory
from app.models import (
    Account,
    CardTrade,
    Friendship,
    MarketListing,
    MarketOffer,
    Notification,
    SocialProfile,
    UserBlock,
)
from app.native_levels import level_for


def profile(db, account_id):
    row = db.get(SocialProfile, account_id)
    if row is None:
        row = SocialProfile(
            account_id=account_id,
            public_id=str(uuid4()),
            friend_code=secrets.token_hex(8).upper(),
            nickname="트레이너",
            collection_public=False,
            wishlist_public=False,
            binder_public=False,
            trade_list_public=True,
            wishlist=[],
            binder=[],
        )
        db.add(row)
        db.flush()
    return row


def public_target(db, public_id):
    row = db.scalar(select(SocialProfile).where(SocialProfile.public_id == str(public_id)))
    if row is None:
        raise HTTPException(404, "profile_not_found")
    return row


def blocked(db, left, right):
    return (
        db.get(UserBlock, (left, right)) is not None or db.get(UserBlock, (right, left)) is not None
    )


def pair(left, right):
    return or_(
        and_(Friendship.sender == left, Friendship.recipient == right),
        and_(Friendship.sender == right, Friendship.recipient == left),
    )


def are_friends(db, left, right):
    return (
        not blocked(db, left, right)
        and db.scalar(
            select(Friendship.id).where(pair(left, right), Friendship.status == "accepted")
        )
        is not None
    )


def notify(db, account_id, kind, target, *, read=False):
    """Record what happened for one account.

    `read=True` is for the person who just did the thing: accepting a trade or
    buying a listing still leaves a line in their history, but it must not raise
    their own unread count or reach them as a push notification.
    """
    db.add(
        Notification(
            account_id=account_id, kind=kind, target=target, read=read, created_at=int(time.time())
        )
    )


def notification_details(db, account_id, rows):
    """Who and what each notification is about, for the list and for push text.

    `target` is only an id. Loading the trades, listings and friendships a page
    refers to in three queries keeps this off the per-row path.
    """
    by_kind = {"trade": set(), "listing": set(), "friend": set(), "offer": set()}
    for row in rows:
        prefix = "listing" if row.kind == "wishlist_listing" else row.kind.split("_", 1)[0]
        if prefix in by_kind:
            by_kind[prefix].add(row.target)

    def load(model, ids):
        return {r.id: r for r in db.scalars(select(model).where(model.id.in_(ids)))} if ids else {}

    trades = load(CardTrade, by_kind["trade"])
    friendships = load(Friendship, by_kind["friend"])
    offers = load(MarketOffer, by_kind["offer"])
    listings = load(MarketListing, by_kind["listing"] | {o.listing_id for o in offers.values()})
    # A price offer's other party is the buyer for the seller and the seller for the buyer.
    offer_other = {
        o.id: o.buyer if o.buyer != account_id else listings[o.listing_id].seller
        for o in offers.values()
        if o.listing_id in listings
    }
    others = {
        r.sender if r.recipient == account_id else r.recipient
        for r in (*trades.values(), *friendships.values())
    } | {other for other in offer_other.values() if other}
    names = (
        {
            p.account_id: p.nickname
            for p in db.scalars(select(SocialProfile).where(SocialProfile.account_id.in_(others)))
        }
        if others
        else {}
    )
    details = {}
    for row in rows:
        pair_row = trades.get(row.target) or friendships.get(row.target)
        listing = listings.get(row.target)
        offer = offers.get(row.target) if row.kind.startswith("offer_") else None
        if offer is not None and offer.listing_id in listings:
            detail = {
                "printing": listings[offer.listing_id].printing,
                "unit_tokens": offer.unit_tokens,
            }
            if offer_other.get(offer.id) in names:
                detail["nickname"] = names[offer_other[offer.id]]
            details[row.id] = detail
        elif row.kind.startswith(("trade_", "friend_")) and pair_row is not None:
            other = pair_row.sender if pair_row.recipient == account_id else pair_row.recipient
            if other in names:
                details[row.id] = {"nickname": names[other]}
        elif listing is not None and (
            row.kind.startswith("listing_") or row.kind == "wishlist_listing"
        ):
            details[row.id] = {"printing": listing.printing, "unit_tokens": listing.unit_tokens}
    return details


def guard_version(row, expected):
    if row.version != expected:
        raise HTTPException(409, "target_version_changed")


def cancel_between(db, left, right):
    for trade in db.scalars(
        select(CardTrade).where(
            CardTrade.status == "pending",
            or_(
                and_(CardTrade.sender == left, CardTrade.recipient == right),
                and_(CardTrade.sender == right, CardTrade.recipient == left),
            ),
        )
    ):
        trade.status, trade.version = "cancelled", trade.version + 1
        inventory.release(db, trade.id)
        # `left` is the one who unfriended or blocked; they caused the cancellation.
        notify(db, trade.sender, "trade_cancelled", trade.id, read=trade.sender == left)
        notify(db, trade.recipient, "trade_cancelled", trade.id, read=trade.recipient == left)


def mutate(db, actor, command, rules):
    own = profile(db, actor.id)
    action = command.action
    affected = {actor.id}
    result = {}
    if action == "profile":
        if command.nickname is None or not command.nickname.strip():
            raise HTTPException(422, "nickname_required")
        own.nickname = command.nickname.strip()
        own.collection_public, own.wishlist_public, own.binder_public = (
            command.collection_public,
            command.wishlist_public,
            command.binder_public,
        )
        # Older apps do not send this flag; keep the stored choice instead of resetting it.
        if command.trade_list_public is not None:
            own.trade_list_public = command.trade_list_public
    elif action == "rotate_code":
        own.friend_code = secrets.token_hex(8).upper()
    elif action == "wishlist":
        wishes = [item.model_dump() for item in command.wishes]
        if len({(w["card_id"], w["finish"]) for w in wishes}) != len(wishes):
            raise HTTPException(422, "duplicate_wish")
        for wish in wishes:
            if wish["card_id"] not in catalogue.cards(rules) or (
                wish["finish"] and wish["finish"] not in catalogue.FINISHES
            ):
                raise HTTPException(422, "unknown_card_or_finish")
        own.wishlist = wishes
    elif action == "binder":
        for key in command.binder:
            catalogue.validate_printing(key, rules)
            if inventory.counts(actor.state).get(key, 0) < 1:
                raise HTTPException(409, "binder_requires_owned_printing")
        own.binder = list(command.binder)
    elif action == "friend_request":
        other = db.scalar(
            select(SocialProfile).where(
                SocialProfile.friend_code == (command.friend_code or "").upper()
            )
        )
        if not other or other.account_id == actor.id or blocked(db, actor.id, other.account_id):
            raise HTTPException(404, "friend_code_unavailable")
        existing = db.scalar(
            select(Friendship).where(
                pair(actor.id, other.account_id), Friendship.status.in_(["pending", "accepted"])
            )
        )
        if existing:
            raise HTTPException(409, "friend_request_already_exists")
        row = Friendship(
            id=str(uuid4()),
            sender=actor.id,
            recipient=other.account_id,
            status="pending",
            expires_at=int(time.time()) + 7 * 86400,
            version=0,
        )
        db.add(row)
        result["id"] = row.id
        affected.add(other.account_id)
        notify(db, other.account_id, "friend_request", row.id)
    elif action in {"friend_accept", "friend_reject", "friend_remove"}:
        row = db.get(Friendship, str(command.target_id))
        if not row or actor.id not in (row.sender, row.recipient):
            raise HTTPException(404, "friendship_not_found")
        guard_version(row, command.target_version)
        other = row.recipient if row.sender == actor.id else row.sender
        if action != "friend_remove" and (
            row.recipient != actor.id
            or row.status != "pending"
            or row.expires_at <= int(time.time())
            or blocked(db, actor.id, other)
        ):
            raise HTTPException(409, "friend_request_unavailable")
        row.status = {
            "friend_accept": "accepted",
            "friend_reject": "rejected",
            "friend_remove": "removed",
        }[action]
        row.version += 1
        affected.add(other)
        if action == "friend_remove":
            cancel_between(db, actor.id, other)
        notify(db, other, action, row.id)
    elif action in {"block", "unblock"}:
        other = public_target(db, command.target_id).account_id
        if other == actor.id:
            raise HTTPException(422, "cannot_block_self")
        row = db.get(UserBlock, (actor.id, other))
        if action == "block":
            if row is None:
                db.add(UserBlock(actor=actor.id, other=other))
            for friendship in db.scalars(select(Friendship).where(pair(actor.id, other))):
                friendship.status, friendship.version = "removed", friendship.version + 1
            cancel_between(db, actor.id, other)
            affected.add(other)
        elif row:
            db.delete(row)
    elif action == "notification_read":
        row = db.get(Notification, command.notification_id)
        if not row or row.account_id != actor.id:
            raise HTTPException(404, "notification_not_found")
        row.read = True
    elif action == "notification_read_all":
        # Everything unread at this moment. `notification_id`, when sent, is the newest
        # one the app has shown: anything that arrived after it stays unread.
        unread = select(Notification).where(
            Notification.account_id == actor.id, Notification.read.is_(False)
        )
        if command.notification_id is not None:
            unread = unread.where(Notification.id <= command.notification_id)
        marked = 0
        for row in db.scalars(unread):
            row.read = True
            marked += 1
        result["marked"] = marked
    else:
        raise HTTPException(422, "invalid_social_action")
    return result, affected


def view_profile(db, account_id, rules):
    row = profile(db, account_id)
    account = db.get(Account, account_id)
    owned = inventory.counts(account.state)
    wishes = []
    for wish in row.wishlist:
        count = (
            owned.get(f"{wish['card_id']}#{wish['finish']}", 0)
            if wish["finish"]
            else account.state.get("cards", {}).get(wish["card_id"], 0)
        )
        wishes.append({**wish, "owned": count, "missing": max(0, wish["target"] - count)})
    return {
        "public_id": row.public_id,
        "friend_code": row.friend_code,
        "nickname": row.nickname,
        "collection_public": row.collection_public,
        "wishlist_public": row.wishlist_public,
        "binder_public": row.binder_public,
        "trade_list_public": row.trade_list_public,
        "wishlist": wishes,
        "binder": [key for key in row.binder if owned.get(key, 0) > 0],
    }


def friend_view(db, actor_id, target_id, rules):
    target = public_target(db, target_id)
    if not are_friends(db, actor_id, target.account_id):
        raise HTTPException(404, "friend_not_found")
    own = view_profile(db, target.account_id, rules)
    result = {
        key: own[key]
        for key in (
            "public_id",
            "nickname",
            "collection_public",
            "wishlist_public",
            "binder_public",
            "trade_list_public",
        )
    }
    result["wishlist"] = own["wishlist"] if target.wishlist_public else []
    result["binder"] = own["binder"] if target.binder_public else []
    result["level"] = account_level(db, target.account_id)
    return result


def account_level(db, account_id) -> int:
    """Trainer level shown next to a friend's name. Derived, never stored."""
    account = db.get(Account, account_id)
    state = account.state if account and isinstance(account.state, dict) else {}
    opened = state.get("packsOpened", 0)
    return level_for(opened if type(opened) is int else 0)
