from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text

from app import inventory, prices, statistics
from app.game_api import Database, Identity, get_rules
from app.game_schemas import CommandRequest
from app.game_service import read_state
from app.models import ServerJob

router = APIRouter(prefix="/v1", tags=["insights"])


@router.get("/prices")
def price_status(db: Database, who: Identity, rules=Depends(get_rules)):
    version, _ = prices.current(db, rules)
    job = db.get(ServerJob, "prices")
    return {
        "version": version,
        "last_success": job.last_success if job else None,
        "error": job.error if job else None,
        "next_run": job.next_run if job else None,
    }


@router.get("/prices/snapshot")
def price_snapshot(db: Database, who: Identity, rules=Depends(get_rules)):
    version, payload = prices.current(db, rules)
    if not payload:
        raise HTTPException(503, "prices_unavailable")
    return Response(
        prices.encode(payload)[1],
        media_type="application/json",
        headers={"ETag": version, "Cache-Control": "private, no-cache"},
    )


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
        _, payload = prices.current(db, rules)
        snapshot = read_state(db, who.account_id)
        _, valuation, _ = prices.apply(rules, snapshot["state"], {"kind": "valuation"}, payload)
        result["collection_usd"] = valuation.get("value_usd")
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
