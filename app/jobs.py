"""Restart-safe private-server maintenance. Network work never holds SQLite's writer lock."""

import asyncio
import contextlib
import json
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from app import prices
from app.config import settings
from app.database import SessionLocal
from app.models import PriceSnapshot, ServerJob
from app.rules import rules


def record_failure(name, owner, message, retry=60):
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(ServerJob, name)
        if row and row.owner == owner:
            row.error = message
            row.next_run, row.lease_until, row.owner = int(time.time()) + retry, 0, None
        db.commit()


def backup_database():
    from app.backups import verified_backup

    with SessionLocal() as db:
        owner = claim(db, "backup", int(time.time()), 600)
        if owner is None:
            return
    try:
        verified_backup(
            settings.database_url, Path(settings.backup_directory), settings.backup_keep
        )
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = db.get(ServerJob, "backup")
            if row.owner == owner:
                row.last_success = int(time.time())
                row.next_run, row.lease_until, row.owner, row.error = (
                    row.last_success + 86400,
                    0,
                    None,
                    None,
                )
            db.commit()
    except Exception:
        record_failure(
            "backup",
            owner,
            "자동 백업 또는 복원 검사가 실패했습니다. 이전 백업은 보존됩니다.",
            3600,
        )


def claim(db, name, now, duration=3600):
    db.execute(text("BEGIN IMMEDIATE"))
    row = db.get(ServerJob, name)
    if row is None:
        row = ServerJob(name=name, next_run=0, lease_until=0)
        db.add(row)
    if row.next_run > now or row.lease_until > now:
        db.rollback()
        return None
    row.owner = str(uuid4())
    row.lease_until = now + duration
    owner = row.owner
    db.commit()
    return owner


def next_daily(now):
    target = datetime.fromtimestamp(now, UTC).replace(hour=21, minute=0, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return int(target.timestamp())


def refresh_prices():
    now = int(time.time())
    with SessionLocal() as db:
        owner = claim(db, "prices", now)
        if owner is None:
            return
    try:
        with SessionLocal() as db:
            _, previous = prices.current(db, rules)
        binary = Path(rules.executable).resolve()
        collector = binary.parent / "price-tools/update_printing_prices.py"
        with tempfile.TemporaryDirectory(prefix="ppb-price-refresh-") as temporary:
            root = Path(temporary)
            cards, packs, pair = root / "cards.json", root / "packs.json", root / "snapshot.json"
            cards.write_text(json.dumps(previous["cardPrices"]))
            packs.write_text(json.dumps(previous["packPrices"]))
            subprocess.run(
                [
                    sys.executable,
                    str(collector),
                    "--binary",
                    str(binary),
                    "--resources",
                    str(prices.resources(str(binary))),
                    "--output",
                    str(cards),
                    "--packs-output",
                    str(packs),
                    "--snapshot",
                    str(pair),
                ],
                check=True,
                capture_output=True,
                timeout=1800,
            )
            candidate = json.loads(pair.read_text())
            prices.validate(candidate, previous)
            # The same native validator/calculator that will execute commands
            # must accept the complete pair before activation.
            from app.game_service import initial_state

            rules.apply(initial_state(), {"kind": "inspect"}, prices=candidate)
            version, data = prices.encode(candidate)
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = db.get(ServerJob, "prices")
            if row.owner != owner:
                db.rollback()
                return
            if db.get(PriceSnapshot, version) is None:
                db.add(PriceSnapshot(id=version, data=data))
            row.active_snapshot, row.error = version, None
            row.last_success = int(time.time())
            row.next_run, row.lease_until, row.owner = next_daily(row.last_success), 0, None
            db.commit()
    except Exception:
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = db.get(ServerJob, "prices")
            if row.owner == owner:
                row.error = "시세 갱신 실패: 마지막 정상 가격을 유지하고 1시간 후 재시도합니다."
                row.next_run, row.lease_until, row.owner = int(time.time()) + 3600, 0, None
            db.commit()


async def loop():
    while True:
        try:
            await asyncio.to_thread(refresh_prices)
        except Exception:
            import logging

            logging.getLogger("ppb.jobs").error("price scheduler unavailable")
        await asyncio.sleep(60)


def expire_transactions():
    from app.maintenance import expire

    now = int(time.time())
    with SessionLocal() as db:
        owner = claim(db, "expiry", now, 120)
        if owner is None:
            return
        try:
            expire(db, now)
            db.execute(text("BEGIN IMMEDIATE"))
            job = db.get(ServerJob, "expiry")
            if job.owner == owner:
                job.next_run, job.lease_until, job.owner, job.last_success = now + 60, 0, None, now
                job.error = None
            db.commit()
        except Exception:
            db.rollback()
            record_failure("expiry", owner, "거래 만료 처리가 실패했습니다. 1분 후 재시도합니다.")


async def maintenance_loop():
    while True:
        try:
            await asyncio.to_thread(expire_transactions)
        except Exception:
            import logging

            logging.getLogger("ppb.jobs").error("expiry scheduler unavailable")
        await asyncio.sleep(60)


async def backup_loop():
    while True:
        try:
            await asyncio.to_thread(backup_database)
        except Exception:
            import logging

            logging.getLogger("ppb.jobs").error("backup scheduler unavailable")
        await asyncio.sleep(60)


@contextlib.asynccontextmanager
async def lifespan(app):
    import os

    enabled = os.getenv("PPB_JOBS_ENABLED", "1") == "1" and bool(rules.executable)
    tasks = (
        [
            asyncio.create_task(loop()),
            asyncio.create_task(maintenance_loop()),
            asyncio.create_task(backup_loop()),
        ]
        if enabled
        else []
    )
    yield
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
