"""Resumable opening only. Each chunk and its progress commit in the SAME transaction.

No background draw: closing the app pauses between chunks. No pack reservation;
if another device uses packs or changes opening mode, continuing fails explicitly.
"""

import hashlib
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import Field
from sqlalchemy import select, text

from app.auth_service import require_session
from app.game_api import Database, Identity, get_rules
from app.game_schemas import CommandRequest, Identifier, StrictModel
from app.game_service import execute, snapshot
from app.models import Account, OnlineReceipt, OpeningJob

router = APIRouter(prefix="/v1/opening-jobs", tags=["opening jobs"])


class JobRequest(StrictModel):
    request_id: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0)]


class Create(JobRequest):
    set_id: Identifier
    count: Annotated[int, Field(strict=True, ge=1, le=10**15)]
    rules_version: Annotated[str, Field(max_length=300)]


class Step(JobRequest):
    target_version: Annotated[int, Field(strict=True, ge=0)]


def describe(row):
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "set_id",
            "total",
            "completed",
            "version",
            "status",
            "opening_mode",
            "created_at",
        )
    }


def owned(db, who, job_id):
    job = db.get(OpeningJob, str(job_id))
    if job is None or job.account_id != who.account_id:
        raise HTTPException(404, "opening_job_not_found")
    return job


def receipt(db, who, payload, action):
    fingerprint = hashlib.sha256(
        (action + who.device_id + payload.model_dump_json()).encode()
    ).hexdigest()
    prior = db.get(OnlineReceipt, (who.account_id, str(payload.request_id)))
    if prior:
        if prior.fingerprint != fingerprint:
            raise HTTPException(409, "idempotency_key_reused")
        reply = {"result": prior.result, "snapshot": snapshot(db.get(Account, who.account_id), db)}
        db.rollback()
        return fingerprint, reply
    return fingerprint, None


@router.get("")
def listing(
    db: Database, who: Identity, response: Response, offset: Annotated[int, Query(ge=0)] = 0
):
    response.headers["Cache-Control"] = "no-store"
    rows = db.scalars(
        select(OpeningJob)
        .where(OpeningJob.account_id == who.account_id)
        .order_by(OpeningJob.created_at.desc(), OpeningJob.id)
        .offset(offset)
        .limit(25)
    )
    return {"items": [describe(row) for row in rows]}


@router.post("")
def create(payload: Create, db: Database, who: Identity):
    db.execute(text("BEGIN IMMEDIATE"))
    require_session(db, who)
    fingerprint, replay = receipt(db, who, payload, "create_opening_job")
    if replay:
        return replay
    if db.get(OpeningJob, str(payload.request_id)) is not None:
        raise HTTPException(409, "idempotency_key_reused")
    account = db.get(Account, who.account_id)
    if account.revision != payload.expected_revision:
        raise HTTPException(409, "revision_conflict")
    if account.state.get("packs", {}).get(payload.set_id, 0) < payload.count:
        raise HTTPException(409, "not_enough_packs")
    job = OpeningJob(
        id=str(payload.request_id),
        account_id=who.account_id,
        set_id=payload.set_id,
        total=payload.count,
        completed=0,
        version=0,
        status="active",
        opening_mode=account.state.get("openingMode", "game"),
        rules_version=payload.rules_version,
        created_at=int(time.time()),
    )
    db.add(job)
    result = {"job": describe(job)}
    db.add(
        OnlineReceipt(
            account_id=who.account_id,
            request_id=str(payload.request_id),
            fingerprint=fingerprint,
            result=result,
        )
    )
    reply = {"result": result, "snapshot": snapshot(account, db)}
    db.commit()
    return reply


@router.post("/{job_id}/step")
def step(job_id: UUID, payload: Step, db: Database, who: Identity, rules=Depends(get_rules)):
    job = owned(db, who, job_id)
    # Chunks are always exactly 1,000 except the last; old versions can replay receipts.
    count = min(1000, job.total - payload.target_version * 1000)
    if count <= 0:
        raise HTTPException(409, "opening_job_changed")
    command = CommandRequest(
        request_id=payload.request_id,
        expected_revision=payload.expected_revision,
        command={"kind": "open_packs", "set_id": job.set_id, "count": count},
        rules_version=job.rules_version,
    )
    db.rollback()

    def advance(db, account, result):
        current = owned(db, who, job_id)
        if current.status != "active" or current.version != payload.target_version:
            raise HTTPException(409, "opening_job_changed")
        if result["opening_mode"] != current.opening_mode:
            raise HTTPException(409, "opening_mode_changed")
        current.completed += count
        current.version += 1
        if current.completed == current.total:
            current.status = "completed"
        result["job"] = describe(current)

    return execute(
        db,
        who.account_id,
        who.device_id,
        command,
        rules,
        session_hash=who.session_hash,
        after_apply=advance,
        scope=f"opening-job/{job_id}/{payload.target_version}",
    )


@router.post("/{job_id}/cancel")
def cancel(job_id: UUID, payload: Step, db: Database, who: Identity):
    db.execute(text("BEGIN IMMEDIATE"))
    require_session(db, who)
    fingerprint, replay = receipt(db, who, payload, f"cancel_opening_job/{job_id}")
    if replay:
        return replay
    job = owned(db, who, job_id)
    account = db.get(Account, who.account_id)
    if account.revision != payload.expected_revision or job.version != payload.target_version:
        raise HTTPException(409, "revision_conflict")
    if job.status != "active":
        raise HTTPException(409, "opening_job_changed")
    job.status, job.version = "cancelled", job.version + 1
    result = {"job": describe(job)}
    db.add(
        OnlineReceipt(
            account_id=who.account_id,
            request_id=str(payload.request_id),
            fingerprint=fingerprint,
            result=result,
        )
    )
    reply = {"result": result, "snapshot": snapshot(account, db)}
    db.commit()
    return reply
