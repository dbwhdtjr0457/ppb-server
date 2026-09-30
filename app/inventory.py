from collections import Counter

from fastapi import HTTPException
from sqlalchemy import delete, select

from app.models import Inventory, Reservation


def counts(state):
    return {key: amount for key, amount in state.get("printingCards", {}).items() if amount > 0}


def normalized(state):
    totals = Counter()
    for key, amount in counts(state).items():
        if "#" not in key:
            return False
        totals[key.rsplit("#", 1)[0]] += amount
    return dict(totals) == {
        key: value for key, value in state.get("cards", {}).items() if value > 0
    }


def sync(db, account):
    if not normalized(account.state):
        raise ValueError("Inventory requires native normalization")
    current = {
        row.printing: row
        for row in db.scalars(select(Inventory).where(Inventory.account_id == account.id))
    }
    for key, quantity in counts(account.state).items():
        row = current.pop(key, None)
        if row:
            row.quantity = quantity
        else:
            db.add(Inventory(account_id=account.id, printing=key, quantity=quantity))
    for row in current.values():
        db.delete(row)
    # Production sessions intentionally disable autoflush. A second sync in the
    # same transaction must see the first projection, not insert duplicate keys.
    db.flush()


def reserved(db, account_id, excluding=None):
    result = Counter()
    for row in db.scalars(select(Reservation).where(Reservation.account_id == account_id)):
        if row.owner != excluding:
            result[row.printing] += row.quantity
    return dict(result)


def floors(db, account_id):
    return {key: value + 1 for key, value in reserved(db, account_id).items()}


def available(db, account, excluding=None):
    held = reserved(db, account.id, excluding)
    return {
        key: max(0, count - held.get(key, 0) - 1) for key, count in counts(account.state).items()
    }


def reserve(db, account, owner, lines):
    free = available(db, account)
    if not lines or any(
        type(n) is not int or n <= 0 or n > free.get(key, 0) for key, n in lines.items()
    ):
        raise HTTPException(409, "not_enough_duplicate_printings")
    for key, quantity in lines.items():
        db.add(Reservation(account_id=account.id, printing=key, owner=owner, quantity=quantity))
    db.flush()


def release(db, owner):
    db.execute(delete(Reservation).where(Reservation.owner == owner))
