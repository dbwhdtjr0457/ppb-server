import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select, text

from app import catalogue, inventory, maintenance, social
from app.game_api import Database, Identity, get_rules
from app.models import (
    Account,
    CardTrade,
    Friendship,
    MarketListing,
    Notification,
    SocialProfile,
    UserBlock,
)
from app.online_schemas import OnlineCommand
from app.online_service import execute, prepare

router = APIRouter(prefix="/v1", tags=["online"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def paged(items, offset, limit, revision):
    selected = items[offset : offset + limit]
    return {
        "items": selected,
        "revision": revision,
        "next_offset": offset + limit if len(items) > offset + limit else None,
    }


def own(db, who, rules):
    maintenance.expire(db)
    account = db.get(Account, who.account_id)
    if not account:
        raise HTTPException(404, "account_not_found")
    # Profile creation is internal provisioning, not a public identity change.
    if db.get(SocialProfile, account.id) is None or not inventory.normalized(account.state):
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        account = db.get(Account, who.account_id)
        social.profile(db, account.id)
        prepare(db, account, rules)
        db.commit()
    return account


@router.get("/profile")
@router.get("/wishlist")
@router.get("/binder")
def profile(db: Database, who: Identity, rules=Depends(get_rules)):
    account = own(db, who, rules)
    return {"revision": account.revision, "profile": social.view_profile(db, account.id, rules)}


@router.get("/friends")
def friends(
    db: Database, who: Identity, offset: Offset = 0, limit: Limit = 50, rules=Depends(get_rules)
):
    account = own(db, who, rules)
    rows = db.scalars(
        select(Friendship)
        .where(or_(Friendship.sender == account.id, Friendship.recipient == account.id))
        .order_by(Friendship.expires_at.desc())
    )
    import time

    items = []
    for row in rows:
        if row.status not in {"pending", "accepted"} or (
            row.status == "pending" and row.expires_at <= int(time.time())
        ):
            continue
        other = row.recipient if row.sender == account.id else row.sender
        if social.blocked(db, account.id, other):
            continue
        p = db.get(SocialProfile, other)
        if p:
            items.append(
                {
                    "id": row.id,
                    "version": row.version,
                    "status": row.status,
                    "incoming": row.recipient == account.id,
                    "public_id": p.public_id,
                    "nickname": p.nickname,
                    "expires_at": row.expires_at,
                }
            )
    return paged(items, offset, limit, account.revision)


@router.get("/friends/{public_id}")
def friend(public_id: str, db: Database, who: Identity, rules=Depends(get_rules)):
    return social.friend_view(db, who.account_id, public_id, rules)


@router.get("/friends/{public_id}/tradeable")
def tradeable(public_id: str, db: Database, who: Identity, rules=Depends(get_rules)):
    """A friend's trade binder: spare copies they could give, keeping one of each.

    Only printings and spare counts; the app has the catalogue and prices. Not paged,
    because the binder is sorted and searched as a whole on the device.
    """
    view = social.friend_view(db, who.account_id, public_id, rules)
    if not view["trade_list_public"]:
        raise HTTPException(403, "trade_list_private")
    friend = db.get(Account, social.public_target(db, public_id).account_id)
    spares = {key: count for key, count in inventory.available(db, friend).items() if count > 0}
    return {"items": spares}


@router.get("/blocks")
def blocks(db: Database, who: Identity, offset: Offset = 0, limit: Limit = 50):
    rows = db.scalars(
        select(SocialProfile)
        .join(UserBlock, SocialProfile.account_id == UserBlock.other)
        .where(UserBlock.actor == who.account_id)
    )
    return paged(
        [{"public_id": p.public_id, "nickname": p.nickname} for p in rows], offset, limit, 0
    )


@router.get("/inventory")
def collection(
    db: Database,
    who: Identity,
    target: str | None = None,
    q: str = "",
    offset: Offset = 0,
    limit: Limit = 50,
    rules=Depends(get_rules),
):
    account = own(db, who, rules)
    if target:
        view = social.friend_view(db, who.account_id, target, rules)
        if not view["collection_public"]:
            raise HTTPException(403, "collection_private")
        account = db.get(Account, social.public_target(db, target).account_id)
    free = inventory.available(db, account)
    held = inventory.reserved(db, account.id)
    items = []
    for key, count in sorted(inventory.counts(account.state).items()):
        item = catalogue.describe(key, rules)
        if q.casefold() not in f"{item['name']} {item['name_ko']} {key}".casefold():
            continue
        items.append(
            {**item, "quantity": count, "available": free.get(key, 0), "reserved": held.get(key, 0)}
        )
    return paged(items, offset, limit, account.revision if not target else 0)


@router.get("/notifications/summary")
def notification_summary(db: Database, who: Identity):
    """Counts the menu bar shows without opening the online window. Read-only and small:
    the app polls it alongside its regular sync."""
    now = int(time.time())
    unread = db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.account_id == who.account_id, Notification.read.is_(False))
    )
    trades = db.scalar(
        select(func.count())
        .select_from(CardTrade)
        .where(
            CardTrade.recipient == who.account_id,
            CardTrade.status == "pending",
            CardTrade.expires_at > now,
        )
    )
    friends = db.scalar(
        select(func.count())
        .select_from(Friendship)
        .where(
            Friendship.recipient == who.account_id,
            Friendship.status == "pending",
            Friendship.expires_at > now,
        )
    )
    return {"unread": unread or 0, "incoming_trades": trades or 0, "incoming_friends": friends or 0}


@router.get("/notifications")
def notifications(db: Database, who: Identity, after: Offset = 0, limit: Limit = 50):
    rows = list(
        db.scalars(
            select(Notification)
            .where(Notification.account_id == who.account_id, Notification.id > after)
            .order_by(Notification.id)
            .limit(limit + 1)
        )
    )
    visible = rows[:limit]
    return {
        "items": [
            {
                "id": r.id,
                "kind": r.kind,
                "target": r.target,
                "read": r.read,
                "created_at": r.created_at,
            }
            for r in visible
        ],
        "next_after": visible[-1].id if len(rows) > limit else None,
    }


@router.get("/matches")
def matches(
    db: Database, who: Identity, offset: Offset = 0, limit: Limit = 50, rules=Depends(get_rules)
):
    actor = own(db, who, rules)
    mine = social.profile(db, actor.id)
    if not mine.wishlist_public:
        return paged([], offset, limit, actor.revision)
    my_free = inventory.available(db, actor)
    items = []
    for row in db.scalars(
        select(Friendship).where(
            or_(Friendship.sender == actor.id, Friendship.recipient == actor.id),
            Friendship.status == "accepted",
        )
    ):
        other_id = row.recipient if row.sender == actor.id else row.sender
        other = social.profile(db, other_id)
        if not other.wishlist_public or social.blocked(db, actor.id, other_id):
            continue
        their_account = db.get(Account, other_id)
        their_free = inventory.available(db, their_account)

        def wanted(wishes, receiver, available):
            result = {}
            for wish in wishes:
                owned = (
                    inventory.counts(receiver.state).get(f"{wish['card_id']}#{wish['finish']}", 0)
                    if wish["finish"]
                    else receiver.state.get("cards", {}).get(wish["card_id"], 0)
                )
                missing = max(0, wish["target"] - owned)
                for key, count in available.items():
                    if (
                        missing
                        and count
                        and key.split("#")[0] == wish["card_id"]
                        and (not wish["finish"] or key.endswith("#" + wish["finish"]))
                    ):
                        amount = min(missing, count)
                        result[key] = max(result.get(key, 0), amount)
                        missing -= amount
            budget = 1000
            lines = []
            for key, amount in list(result.items())[:20]:
                amount = min(amount, budget)
                if amount:
                    lines.append({"printing": key, "quantity": amount})
                    budget -= amount
            return lines

        give, receive = (
            wanted(other.wishlist, their_account, my_free),
            wanted(mine.wishlist, actor, their_free),
        )
        if give and receive:
            items.append(
                {
                    "public_id": other.public_id,
                    "nickname": other.nickname,
                    "offered": give[:20],
                    "requested": receive[:20],
                }
            )
    return paged(items, offset, limit, actor.revision)


@router.get("/trades")
def trades(db: Database, who: Identity, offset: Offset = 0, limit: Limit = 50):
    maintenance.expire(db)
    rows = db.scalars(
        select(CardTrade)
        .where(or_(CardTrade.sender == who.account_id, CardTrade.recipient == who.account_id))
        .order_by(CardTrade.expires_at.desc())
    )
    items = []
    for row in rows:
        other_id = row.recipient if row.sender == who.account_id else row.sender
        other = db.get(SocialProfile, other_id)
        items.append(
            {
                "id": row.id,
                "version": row.version,
                "incoming": row.recipient == who.account_id,
                "nickname": other.nickname if other else "트레이너",
                "public_id": other.public_id if other else "",
                "offered": row.offered,
                "requested": row.requested,
                "status": row.status,
                "expires_at": row.expires_at,
                "counter_of": row.counter_of,
            }
        )
    return paged(items, offset, limit, db.get(Account, who.account_id).revision)


@router.get("/market/listings")
def listings(
    db: Database,
    who: Identity,
    q: str = "",
    set_id: str = "",
    tier: str = "",
    finish: str = "",
    sort: str = "newest",
    mine: bool = False,
    offset: Offset = 0,
    limit: Limit = 50,
    rules=Depends(get_rules),
):
    maintenance.expire(db)
    query = (
        select(MarketListing, SocialProfile)
        .join(SocialProfile, SocialProfile.account_id == MarketListing.seller)
        .where(
            MarketListing.seller.not_in(
                select(UserBlock.other).where(UserBlock.actor == who.account_id)
            ),
            MarketListing.seller.not_in(
                select(UserBlock.actor).where(UserBlock.other == who.account_id)
            ),
        )
    )
    query = (
        query.where(MarketListing.seller == who.account_id)
        if mine
        else query.where(MarketListing.status == "active")
    )
    query = (
        query.order_by(MarketListing.unit_tokens, MarketListing.id)
        if sort == "price"
        else query.order_by(MarketListing.created_at.desc(), MarketListing.id)
    )
    needle = "".join(q.casefold().split())
    items = []
    ranked = []
    matched = 0
    for row, seller in db.execute(query.execution_options(yield_per=100)):
        card = catalogue.describe(row.printing, rules)
        rank = search_rank(needle, card, row.printing)
        if (
            rank is None
            or (set_id and card.get("set_id") != set_id)
            or (tier and card.get("tier") != tier)
            or (finish and card.get("finish") != finish)
        ):
            continue
        item = {
            **card,
            "id": row.id,
            "version": row.version,
            "quantity": row.quantity,
            "unit_tokens": row.unit_tokens,
            "status": row.status,
            "expires_at": row.expires_at,
            "mine": row.seller == who.account_id,
            "nickname": seller.nickname if seller else "트레이너",
            "public_id": seller.public_id if seller else "",
        }
        if needle:
            # Ranking needs every match before paging, so a search cannot stop early.
            ranked.append((rank, len(ranked), item))
            continue
        matched += 1
        if matched <= offset:
            continue
        items.append(item)
        if len(items) > limit:
            break
    if needle:
        ranked.sort(key=lambda entry: entry[:2])
        items = [item for _, _, item in ranked[offset : offset + limit + 1]]
    return {
        "items": items[:limit],
        "revision": db.get(Account, who.account_id).revision,
        "next_offset": offset + limit if len(items) > limit else None,
    }


def search_rank(needle, card, printing):
    """0 for an exact name, 1 for a name prefix, 2 for any other match, None for no match.

    A plain substring test listed Mewtwo and Mew Duo before Mew when searching "mew"; the
    selected sort (newest or price) still orders listings within each rank.
    """
    if not needle:
        return 0
    names = ["".join(str(card.get(key) or "").casefold().split()) for key in ("name", "name_ko")]
    if needle in names:
        return 0
    if any(name.startswith(needle) for name in names):
        return 1
    if any(needle in name for name in names) or needle in printing.casefold():
        return 2
    return None


@router.post("/profile")
@router.post("/friends")
@router.post("/wishlist")
@router.post("/binder")
@router.post("/notifications")
@router.post("/trades")
@router.post("/market/listings")
def mutate(
    body: OnlineCommand, request: Request, db: Database, who: Identity, rules=Depends(get_rules)
):
    allowed = {
        "profile": {"profile", "rotate_code"},
        "friends": {
            "friend_request",
            "friend_accept",
            "friend_reject",
            "friend_remove",
            "block",
            "unblock",
        },
        "wishlist": {"wishlist"},
        "binder": {"binder"},
        "notifications": {"notification_read"},
    }
    allowed["trades"] = {
        "trade_create",
        "trade_accept",
        "trade_reject",
        "trade_cancel",
        "trade_counter",
    }
    allowed["listings"] = {"listing_create", "listing_cancel", "listing_buy"}
    if body.action not in allowed[request.url.path.rsplit("/", 1)[-1]]:
        raise HTTPException(422, "action_route_mismatch")
    maintenance.expire(db)
    return execute(db, who, body, rules)
