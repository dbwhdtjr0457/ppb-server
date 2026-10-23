from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text

from app import inventory, price_history, prices, statistics
from app.game_api import Database, Identity, get_rules
from app.game_schemas import CommandRequest
from app.game_service import read_state
from app.models import ServerJob

router = APIRouter(prefix="/v1", tags=["insights"])


@router.get("/prices")
def price_status(db: Database, who: Identity, rules=Depends(get_rules)):
    version = prices.current_version(db, rules)
    job = db.get(ServerJob, "prices")
    return {
        "version": version,
        "last_success": job.last_success if job else None,
        "error": job.error if job else None,
        "next_run": job.next_run if job else None,
    }


@router.get("/prices/snapshot")
def price_snapshot(db: Database, who: Identity, rules=Depends(get_rules)):
    version, data = prices.current_encoded(db, rules)
    if data is None:
        raise HTTPException(503, "prices_unavailable")
    return Response(
        data,
        media_type="application/json",
        headers={"ETag": version, "Cache-Control": "private, no-cache"},
    )


@router.get("/prices/history")
def price_history_series(
    db: Database,
    who: Identity,
    kind: Literal["printing", "card", "pack"],
    item: Annotated[str, Query(min_length=1, max_length=160)],
    days: Annotated[int, Query(ge=0, le=3650)] = 0,
):
    """One printing, card or pack over time. `days=0` is everything recorded."""
    return price_history.history(db, kind, item, days)


@router.get("/prices/movers")
def price_movers(db: Database, who: Identity, rules=Depends(get_rules)):
    """The latest day's biggest moves, and how the caller's own cards moved that day."""
    row, moves = price_history.latest_moves(db)
    if row is None:
        return {
            "day": None,
            "since": None,
            "changed": 0,
            "risers": [],
            "fallers": [],
            "packs": [],
            "owned": [],
        }
    _, payload = prices.current(db, rules)
    exact = payload["cardPrices"].get("printingPrices", {}) if payload else {}
    owned: dict[tuple[str, str], int] = {}
    # A printing without its own price is valued at its card's price, as in the app.
    for key, count in inventory.counts(read_state(db, who.account_id)["state"]).items():
        series = ("printing", key) if key in exact else ("card", key.rsplit("#", 1)[0])
        owned[series] = owned.get(series, 0) + count
    return {
        "day": row.day,
        "since": row.since,
        **moves,
        "owned": price_history.owned_moves(db, row.day, owned),
    }


@router.post("/quotes")
def quote(body: CommandRequest, db: Database, who: Identity, rules=Depends(get_rules)):
    state = read_state(db, who.account_id)
    version, payload = prices.current(db, rules)
    if state["revision"] != body.expected_revision:
        raise HTTPException(409, "revision_conflict")
    command = body.command.model_dump(mode="json")
    if command["kind"] not in prices.PRICED_COMMANDS:
        raise HTTPException(422, "command_has_no_quote")
    prices.check_version(command["kind"], body.price_version, version)
    _, result, _ = prices.apply(
        rules,
        state["state"],
        {**command, "quote_kind": command["kind"], "kind": "quote"},
        payload,
        inventory.floors(db, who.account_id),
    )
    return {"tokens": result["tokens"], "price_version": version, "revision": state["revision"]}


@router.get("/stats")
def stats(
    db: Database,
    who: Identity,
    days: Annotated[int, Query(ge=0, le=30)] = 0,
    set_id: Annotated[str | None, Query(max_length=160)] = None,
    mode: Literal["game", "realistic", "unknown"] | None = None,
    rules=Depends(get_rules),
):
    if days not in (0, 7, 30):
        raise HTTPException(422, "invalid_period")
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        statistics.rebuild(db, who.account_id)
        result = statistics.summary(db, who.account_id, days, set_id, mode)
        snapshot = read_state(db, who.account_id)
        db.commit()
        _, payload = prices.current(db, rules)
        db.rollback()
        # Valuation is pure and can be slow; do not hold SQLite's writer lock.
        _, valuation, _ = prices.apply(rules, snapshot["state"], {"kind": "valuation"}, payload)
        result["collection_usd"] = valuation.get("value_usd")
        return result
    except BaseException:
        db.rollback()
        raise
