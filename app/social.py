import secrets
import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from app import catalogue, inventory
from app.models import Account, CardTrade, Friendship, Notification, SocialProfile, UserBlock


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


def notify(db, account_id, kind, target):
    db.add(
        Notification(
            account_id=account_id, kind=kind, target=target, read=False, created_at=int(time.time())
        )
    )


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
        notify(db, trade.sender, "trade_cancelled", trade.id)
        notify(db, trade.recipient, "trade_cancelled", trade.id)


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
    return result
