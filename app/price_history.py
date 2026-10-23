"""Price history beside the immutable snapshots.

A snapshot is a whole 8 MB document, so a chart cannot read 90 of them. Each activation
also stores only the values that changed, under the snapshot's day, plus a short list of
that day's biggest moves. Unchanged days are implied by the previous point.
"""

import json
import logging
import re
import time
from datetime import date, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.dialects.sqlite import insert

from app.models import PriceDay, PricePoint, PriceSnapshot

KINDS = ("printing", "card", "pack")
# A $0.10 card doubling is noise. Moves are listed only for cards worth a few dollars
# that moved by at least half a dollar.
MIN_USD = 3.0
MIN_CHANGE = 0.5
TOP = 20
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def snapshot_day(payload: dict) -> str:
    cards, packs = payload["cardPrices"], payload["packPrices"]
    day = (
        (cards.get("printingPriceSnapshot") or {}).get("asOf")
        or cards.get("asOf")
        or packs.get("asOf")
    )
    if not isinstance(day, str) or not _DAY.fullmatch(day):
        raise ValueError("Price snapshot has no day")
    return day


def series(payload: dict) -> dict[tuple[str, str], float]:
    cards, packs = payload["cardPrices"], payload["packPrices"]
    values = {("printing", key): float(v) for key, v in cards.get("printingPrices", {}).items()}
    values.update({("card", key): float(v) for key, v in cards.get("prices", {}).items()})
    values.update({("pack", key): float(e["usd"]) for key, e in packs.get("packs", {}).items()})
    return values


def recorded(db) -> bool:
    return db.scalar(select(PriceDay.day).limit(1)) is not None


def _write(db, day: str, values: dict[tuple[str, str], float]):
    rows = [{"kind": k, "item": i, "day": day, "usd": v} for (k, i), v in values.items()]
    for start in range(0, len(rows), 5000):
        statement = insert(PricePoint).values(rows[start : start + 5000])
        db.execute(
            statement.on_conflict_do_update(
                index_elements=["kind", "item", "day"], set_={"usd": statement.excluded.usd}
            )
        )


_PREVIOUS = """
    SELECT p.kind, p.item, p.usd,
        (SELECT q.usd FROM price_points q
         WHERE q.kind = p.kind AND q.item = p.item AND q.day < p.day
         ORDER BY q.day DESC LIMIT 1)
    FROM price_points p WHERE p.day = :day
"""


def _move(kind, item, before, after, count=None):
    move = {"kind": kind, "item": item, "before": before, "after": after}
    if count is not None:
        move["count"] = count
    return move


def printed_cards(payload: dict) -> set[str]:
    return {key.rsplit("#", 1)[0] for key in payload["cardPrices"].get("printingPrices", {})}


def moves(db, day: str, printed: set[str]) -> dict:
    """The day's risers and fallers, computed from the stored points, not the payload diff.

    A second refresh on the same day therefore still compares against the day before.
    A card's own price only stands in for cards that have no printing prices.
    """
    cards, packs = [], []
    for kind, item, after, before in db.execute(text(_PREVIOUS), {"day": day}):
        if before is None or before == after:
            continue
        if kind == "pack":
            packs.append(_move(kind, item, before, after))
        elif kind == "printing" or item not in printed:
            cards.append(_move(kind, item, before, after))

    def rate(move):
        return (move["after"] - move["before"]) / move["before"]

    def notable(move, value):
        return value >= MIN_USD and abs(move["after"] - move["before"]) >= MIN_CHANGE

    risers = sorted(
        (m for m in cards if m["after"] > m["before"] and notable(m, m["after"])),
        key=rate,
        reverse=True,
    )
    fallers = sorted(
        (m for m in cards if m["after"] < m["before"] and notable(m, m["before"])), key=rate
    )
    return {
        "changed": len(cards),
        "risers": risers[:TOP],
        "fallers": fallers[:TOP],
        "packs": sorted(packs, key=rate, reverse=True),
    }


def _close_day(db, day: str, since: str | None, snapshot: str, printed: set[str]):
    existing = db.get(PriceDay, day)
    # A second refresh on the same day keeps comparing against the earlier day.
    since = existing.since if existing else (since if since != day else None)
    data = json.dumps(moves(db, day, printed), separators=(",", ":"))
    if existing:
        existing.snapshot, existing.since, existing.moves = snapshot, since, data
    else:
        db.add(
            PriceDay(
                day=day, snapshot=snapshot, since=since, moves=data, created_at=int(time.time())
            )
        )


def record(db, previous: dict, candidate: dict, snapshot: str):
    """Store what changed with the activation, in the same transaction.

    Before the one-time backfill has run there is no baseline to diff against; the
    backfill reads this snapshot from the table later, so nothing is lost.
    """
    if not recorded(db):
        return
    try:
        since, day = snapshot_day(previous), snapshot_day(candidate)
        before, after = series(previous), series(candidate)
    except (KeyError, TypeError, ValueError):
        # History only describes prices; it must never block a valid activation.
        logging.getLogger("ppb.prices").error("price history skipped for %s", snapshot)
        return
    _write(db, day, {k: v for k, v in after.items() if before.get(k) != v})
    _close_day(db, day, since, snapshot, printed_cards(candidate))


def backfill(sessions, fallback: tuple[str, dict] | None):
    """Rebuild history from every stored snapshot, oldest first.

    A fresh server has no stored snapshot yet; the bundled one becomes the first day.
    Points are written one snapshot per transaction, and the days, which mark the history
    as built, only at the end. An interrupted run starts over; rewriting a point is harmless.
    """
    with sessions() as db:
        ids = list(db.scalars(select(PriceSnapshot.id).order_by(PriceSnapshot.created_at)))
    last: dict[tuple[str, str], float] = {}
    days: dict[str, tuple[str | None, str, set[str]]] = {}
    last_day = None
    sources = [(snapshot, None) for snapshot in ids] or ([fallback] if fallback else [])
    for snapshot, payload in sources:
        if payload is None:
            with sessions() as db:
                payload = json.loads(
                    db.scalar(select(PriceSnapshot.data).where(PriceSnapshot.id == snapshot))
                )
        day, values = snapshot_day(payload), series(payload)
        changed = {k: v for k, v in values.items() if last.get(k) != v}
        with sessions() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            _write(db, day, changed)
            db.commit()
        since = days[day][0] if day in days else (last_day if last_day != day else None)
        days[day] = (since, snapshot, printed_cards(payload))
        last.update(values)
        last_day = day
    with sessions() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        for day, (since, snapshot, printed) in days.items():
            _close_day(db, day, since, snapshot, printed)
        db.commit()


def history(db, kind: str, item: str, days: int) -> dict:
    latest = db.scalar(select(func.max(PriceDay.day)))
    query = select(PricePoint.day, PricePoint.usd).where(
        PricePoint.kind == kind, PricePoint.item == item
    )
    points = []
    if latest and days:
        cutoff = (date.fromisoformat(latest) - timedelta(days=days)).isoformat()
        # The value carried into the window opens the line at its left edge.
        opening = db.execute(
            query.where(PricePoint.day <= cutoff).order_by(PricePoint.day.desc()).limit(1)
        ).first()
        if opening:
            points.append({"day": cutoff, "usd": opening.usd})
        query = query.where(PricePoint.day > cutoff)
    points += [{"day": day, "usd": usd} for day, usd in db.execute(query.order_by(PricePoint.day))]
    return {"kind": kind, "item": item, "latest_day": latest, "points": points}


def owned_moves(db, day: str, keys: dict[tuple[str, str], int]) -> list[dict]:
    """How the account's own cards moved on the day, largest change in value first."""
    found = []
    by_kind: dict[str, list[str]] = {}
    for kind, item in keys:
        by_kind.setdefault(kind, []).append(item)
    for kind, items in by_kind.items():
        for start in range(0, len(items), 500):
            chunk = items[start : start + 500]
            marks = ",".join(f":i{n}" for n in range(len(chunk)))
            rows = db.execute(
                text(_PREVIOUS + f" AND p.kind = :kind AND p.item IN ({marks})"),
                {"day": day, "kind": kind, **{f"i{n}": item for n, item in enumerate(chunk)}},
            )
            for kind_, item, after, before in rows:
                if before is not None and before != after:
                    found.append(_move(kind_, item, before, after, keys[(kind_, item)]))
    found.sort(key=lambda m: (-abs(m["after"] - m["before"]) * m["count"], m["item"]))
    return found[:TOP]


def latest_moves(db) -> tuple[PriceDay | None, dict]:
    row = db.scalar(select(PriceDay).order_by(PriceDay.day.desc()).limit(1))
    return row, (json.loads(row.moves) if row else {})
