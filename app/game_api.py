from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth_service import Principal, identity
from app.database import get_db
from app.game_schemas import CommandRequest
from app.game_service import execute, read_state
from app.maintenance import expire
from app.models import Account, GameEvent

router = APIRouter(prefix="/v1", tags=["game"])
Database = Annotated[Session, Depends(get_db)]


Identity = Annotated[Principal, Depends(identity)]


def get_rules():
    from app.rules import rules

    return rules


@router.get("/state")
def state(
    db: Database,
    who: Identity,
    response: Response,
    if_none_match: Annotated[str | None, Header()] = None,
):
    expire(db)
    # Avoid loading/parsing the collection when only its revision is needed.
    revision = db.scalar(select(Account.revision).where(Account.id == who[0])) or 0
    tag = f'"{revision}"'
    if if_none_match == tag:
        return Response(status_code=304, headers={"ETag": tag, "Cache-Control": "no-store"})
    current = read_state(db, who[0])
    response.headers["ETag"] = f'"{current["revision"]}"'
    response.headers["Cache-Control"] = "no-store"
    return current


@router.get("/rules")
def rule_version(who: Identity, rules=Depends(get_rules)):
    from app.game_service import initial_state

    _, _, version = rules.apply(initial_state(), {"kind": "inspect"})
    return {"rules_version": version}


@router.post("/commands")
def commands(
    payload: CommandRequest,
    db: Database,
    who: Identity,
    x_ppb_state_patch: Annotated[str | None, Header()] = None,
    rules=Depends(get_rules),
):
    expire(db)
    return execute(
        db, who[0], who[1], payload, rules,
        session_hash=who.session_hash, patch=x_ppb_state_patch == "1",
    )


@router.get("/events")
def events(
    db: Database,
    who: Identity,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    rows = list(
        db.scalars(
            select(GameEvent)
            .where(GameEvent.account_id == who[0], GameEvent.revision > after)
            .order_by(GameEvent.revision)
            .limit(limit)
        )
    )
    return {
        "events": [
            {
                "revision": r.revision,
                "request_id": r.request_id,
                "device_id": r.device_id,
                "command": r.command,
                "payload": r.payload,
                "result": r.result,
                "balance_before": r.balance_before,
                "balance_after": r.balance_after,
                "rules_version": r.rules_version,
                "created_at": r.created_at,
            }
            for r in rows
        ],
        "next_after": rows[-1].revision if rows else after,
    }
