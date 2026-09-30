import time
from uuid import uuid4

from sqlalchemy import select, text

from app import inventory, social
from app.models import Account, CardTrade, Friendship, GameEvent, MarketListing, utcnow


def expire(db, now=None):
    now = int(time.time()) if now is None else now
    # A normal 304 state poll must not acquire SQLite's global writer lock.
    # Re-query under the lock below when work exists; concurrent expiration is safe.
    due = any(
        db.scalar(select(model.id).where(model.status == active, model.expires_at <= now).limit(1))
        for model, active in [
            (Friendship, "pending"),
            (CardTrade, "pending"),
            (MarketListing, "active"),
        ]
    )
    db.rollback()
    if not due:
        return
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        for model, active, kind in [
            (Friendship, "pending", "friend_expired"),
            (CardTrade, "pending", "trade_expired"),
            (MarketListing, "active", "listing_expired"),
        ]:
            for row in db.scalars(
                select(model).where(model.status == active, model.expires_at <= now)
            ):
                row.status, row.version = "expired", row.version + 1
                inventory.release(db, row.id)
                affected = {row.seller} if model == MarketListing else {row.sender, row.recipient}
                transaction = str(uuid4())
                for account_id in affected:
                    account = db.get(Account, account_id)
                    account.revision += 1
                    account.updated_at = utcnow()
                    social.notify(db, account_id, kind, row.id)
                    db.add(
                        GameEvent(
                            account_id=account_id,
                            device_id="00000000-0000-0000-0000-000000000000",
                            request_id=str(uuid4()),
                            revision=account.revision,
                            command=kind,
                            fingerprint=transaction,
                            payload={"transaction_id": transaction, "target": row.id},
                            result={},
                            balance_before=account.balance,
                            balance_after=account.balance,
                            rules_version="online-v1",
                        )
                    )
        db.commit()
    except BaseException:
        db.rollback()
        raise
