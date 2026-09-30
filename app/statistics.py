"""One projection per immutable event, never reconstructed from the 1000-pack UI tail."""

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import GameEvent, GameStatistic


def project(db, event, mode=None):
    if db.get(GameStatistic, event.id):
        return
    metrics = {
        "purchased": 0,
        "opened": 0,
        "purchase_spent": 0,
        "sale_income": 0,
        "new": 0,
        "duplicates": 0,
        "tiers": {},
        "finishes": {},
        "variants": {},
    }
    if event.command == "buy_packs":
        metrics.update(
            purchased=event.payload["count"],
            purchase_spent=event.balance_before - event.balance_after,
        )
    if event.command in {"sell_spares", "sell_bulk"}:
        metrics["sale_income"] = event.balance_after - event.balance_before
    tiers, finishes, variants = Counter(), Counter(), Counter()
    packs = event.result.get("packs", {}).get("packs", [])
    for pack in packs:
        metrics["opened"] += 1
        variants[pack["variant"]] += 1
        for slot in pack["slotResults"]:
            card = slot["card"]
            tiers[card["tier"]] += 1
            finishes[card.get("finish", "unknown")] += 1
            metrics["new" if card["isNew"] else "duplicates"] += 1
    metrics.update(tiers=dict(tiers), finishes=dict(finishes), variants=dict(variants))
    db.add(
        GameStatistic(
            event_id=event.id,
            account_id=event.account_id,
            created_at=event.created_at,
            set_id=event.payload.get("set_id"),
            mode=mode or event.result.get("opening_mode", "unknown"),
            metrics=metrics,
        )
    )


def rebuild(db, account_id):
    missing = (
        select(GameEvent)
        .outerjoin(GameStatistic, GameEvent.id == GameStatistic.event_id)
        .where(GameEvent.account_id == account_id, GameStatistic.event_id.is_(None))
    )
    for event in db.scalars(missing):
        project(db, event)
    db.flush()


def summary(db, account_id, days=0, set_id=None, mode=None):
    query = select(GameStatistic).where(GameStatistic.account_id == account_id)
    all_rows = list(db.scalars(query.order_by(GameStatistic.created_at)))
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days) if days else None
    total, tiers, finishes, variants = Counter(), Counter(), Counter(), Counter()
    for row in all_rows:
        if (
            (since and row.created_at.replace(tzinfo=None) < since)
            or (set_id and row.set_id != set_id)
            or (mode and row.mode != mode)
        ):
            continue
        for key, value in row.metrics.items():
            if isinstance(value, int):
                total[key] += value
        tiers.update(row.metrics["tiers"])
        finishes.update(row.metrics["finishes"])
        variants.update(row.metrics["variants"])
    opened = total["opened"]
    return {
        "totals": dict(total),
        "tiers": dict(tiers),
        "finishes": dict(finishes),
        "variants": dict(variants),
        "variant_rates": {key: value / opened for key, value in variants.items()} if opened else {},
        "coverage_since": all_rows[0].created_at.isoformat() if all_rows else None,
        "historical_mode_unknown": any(row.mode == "unknown" for row in all_rows),
        "observed_not_guaranteed": True,
        "new_rate": total["new"] / (total["new"] + total["duplicates"])
        if total["new"] + total["duplicates"]
        else None,
        "duplicate_rate": total["duplicates"] / (total["new"] + total["duplicates"])
        if total["new"] + total["duplicates"]
        else None,
    }
