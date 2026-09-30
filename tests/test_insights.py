import json
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app import prices, statistics
from app.database import Base, make_engine
from app.game_service import initial_state
from app.jobs import claim, next_daily
from app.models import Account, GameEvent, PriceSnapshot, ServerJob


def pair():
    return {
        "schemaVersion": 1,
        "cardPrices": {
            "currency": "USD",
            "krwPerUSD": 1300,
            "prices": {"a": 1},
            "printingPrices": {"a#holo": 2},
        },
        "packPrices": {"currency": "USD", "packs": {"set": {"usd": 5}}},
    }


def test_atomic_pair_validation():
    previous = pair()
    prices.validate(previous, previous)
    for mutate in [
        lambda p: p["cardPrices"]["prices"].clear(),
        lambda p: p["packPrices"]["packs"].clear(),
        lambda p: p["cardPrices"]["printingPrices"].update({"a#holo": float("nan")}),
    ]:
        candidate = deepcopy(previous)
        mutate(candidate)
        with pytest.raises(ValueError):
            prices.validate(candidate, previous)
    assert prices.encode(previous) == prices.encode(deepcopy(previous))
    with pytest.raises(HTTPException):
        prices.check_version("buy_packs", "old", "new")


def test_jobs_and_statistics_are_durable_and_idempotent(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/stats.db")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    account = str(uuid4())
    with sessions() as db:
        assert claim(db, "prices", 100, 60)
        assert claim(db, "prices", 110, 60) is None
        assert claim(db, "prices", 161, 60)
        assert next_daily(0) == 21 * 3600
        db.add(Account(id=account, revision=0, balance=0, state=initial_state()))
        db.flush()
        packs = [
            {
                "variant": "standard",
                "slotResults": [
                    {"card": {"id": "a", "tier": "C", "finish": "holo", "isNew": i == 0}}
                ],
            }
            for i in range(1200)
        ]
        event = GameEvent(
            account_id=account,
            device_id=str(uuid4()),
            request_id=str(uuid4()),
            revision=1,
            command="open_packs",
            fingerprint="a",
            payload={"set_id": "set"},
            result={"packs": {"packs": packs}},
            balance_before=0,
            balance_after=0,
            rules_version="test",
            created_at=datetime.now(),
        )
        db.add(event)
        db.flush()
        statistics.project(db, event, "realistic")
        db.flush()
        statistics.project(db, event, "realistic")
        db.commit()
        result = statistics.summary(db, account, mode="realistic")
        assert result["totals"]["opened"] == 1200
        assert result["totals"]["new"] == 1
        assert result["totals"]["duplicates"] == 1199
        assert statistics.summary(db, account, mode="game")["totals"].get("opened", 0) == 0


def test_price_job_failure_retry_and_atomic_publication(tmp_path, monkeypatch):
    from app import jobs

    engine = make_engine(f"sqlite:///{tmp_path}/jobs.db")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionLocal", sessions)

    class Rules:
        executable = str(tmp_path / "rules")

        def apply(self, *args, **kwargs):
            return {}, {}, "test"

    monkeypatch.setattr(jobs, "rules", Rules())
    monkeypatch.setattr(prices, "resources", lambda _: tmp_path)
    monkeypatch.setattr(prices, "bundled", lambda _: pair())
    clock = [100000]
    monkeypatch.setattr(jobs.time, "time", lambda: clock[0])
    version, data = prices.encode(pair())
    with sessions() as db:
        db.add(PriceSnapshot(id=version, data=data))
        db.add(ServerJob(name="prices", next_run=0, lease_until=0, active_snapshot=version))
        db.commit()

    def fail(*args, **kwargs):
        raise TimeoutError("Source unavailable")

    monkeypatch.setattr(jobs.subprocess, "run", fail)
    jobs.refresh_prices()
    with sessions() as db:
        job = db.get(ServerJob, "prices")
        assert job.active_snapshot == version and job.error
        assert job.next_run == clock[0] + 3600
    calls = []

    def collect(args, **kwargs):
        calls.append(True)
        candidate = pair()
        candidate["cardPrices"]["prices"]["a"] = 3
        candidate["packPrices"]["packs"]["set"]["usd"] = 7
        from pathlib import Path

        Path(args[args.index("--snapshot") + 1]).write_text(json.dumps(candidate))

    monkeypatch.setattr(jobs.subprocess, "run", collect)
    jobs.refresh_prices()
    assert not calls
    clock[0] += 3601
    jobs.refresh_prices()
    jobs.refresh_prices()
    assert len(calls) == 1
    with sessions() as db:
        job = db.get(ServerJob, "prices")
        assert job.error is None and job.active_snapshot != version
        _, active = prices.current(db, Rules())
        assert active["cardPrices"]["prices"]["a"] == 3
        assert active["packPrices"]["packs"]["set"]["usd"] == 7
        assert db.get(PriceSnapshot, version).data == data
