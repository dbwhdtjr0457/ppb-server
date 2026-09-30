"""Authenticated, sanitized operational health. No paths, emails, or other users' data."""

import os
import time

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select

from app.auth_api import Database, Identity
from app.auth_service import gateway
from app.config import settings
from app.models import ServerJob
from app.performance import summary

router = APIRouter(prefix="/v1/server", tags=["operations"], dependencies=[Depends(gateway)])


@router.get("/status")
def status(db: Database, who: Identity, response: Response):
    response.headers["Cache-Control"] = "no-store"
    now = int(time.time())
    enabled = os.getenv("PPB_JOBS_ENABLED", "1") == "1" and bool(settings.rules_executable)
    rows = {row.name: row for row in db.scalars(select(ServerJob))}
    items = []
    for name, grace in [("backup", 90000), ("expiry", 300), ("prices", 93600)]:
        row = rows.get(name)
        stale = bool(row and row.last_success and row.last_success + grace < now)
        running = bool(row and row.owner and row.lease_until > now)
        state = "disabled" if not enabled else "waiting"
        if enabled and row:
            if row.error:
                state = "failed"
            elif stale:
                state = "stale"
            elif running:
                state = "running"
            elif row.last_success:
                state = "ok"
        items.append(
            {
                "name": name,
                "state": state,
                "last_success": row.last_success if row else None,
                "next_run": row.next_run if row else None,
                "error": row.error if row else None,
            }
        )
    return {
        "jobs": items,
        "backup_retention": settings.backup_keep,
        "token_policy": "client_report_trusted",
        "latency_worker_recent": summary(),
        "server_time": now,
    }
