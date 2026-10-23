import json
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from test_auth import auth, headers, registered  # noqa: F401

from app import price_history, prices
from app.database import Base, make_engine
from app.models import Account, PriceDay, PricePoint, PriceSnapshot, ServerJob


def snapshot(day, printings=None, cards=None, packs=None):
    return {
        "schemaVersion": 1,
        "cardPrices": {
            "currency": "USD",
            "krwPerUsd": 1300,
            "asOf": "2026-09-01",
            "printingPriceSnapshot": {"asOf": day},
            "prices": {"a": 10.0, "b": 0.2, "solo": 8.0, **(cards or {})},
            "printingPrices": {
                "a#holo": 10.0,
                "a#reverseHolo": 12.0,
                "b#normal": 0.2,
                **(printings or {}),
            },
        },
        "packPrices": {"currency": "USD", "packs": {"set": {"usd": 5.0}, **(packs or {})}},
    }


@pytest.fixture
def sessions(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/history.db")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


def store(sessions, *payloads):
    start = datetime(2026, 9, 1)
    with sessions() as db:
        for n, payload in enumerate(payloads):
            version, data = prices.encode(payload)
            db.add(PriceSnapshot(id=version, data=data, created_at=start + timedelta(days=n)))
        db.commit()
    return [prices.encode(payload)[0] for payload in payloads]


def points(sessions, kind, item):
    with sessions() as db:
        rows = db.execute(
            select(PricePoint.day, PricePoint.usd)
            .where(PricePoint.kind == kind, PricePoint.item == item)
            .order_by(PricePoint.day)
        )
        return [tuple(row) for row in rows]


def day(sessions, value):
    with sessions() as db:
        row = db.get(PriceDay, value)
        return row.since, json.loads(row.moves)


def test_backfill_keeps_only_changes_and_lists_notable_moves(sessions):
    first = snapshot("2026-09-22")
    second = snapshot(
        "2026-09-23",
        printings={"a#holo": 14.0, "b#normal": 0.6},
        cards={"a": 14.0, "b": 0.6, "solo": 6.0},
        packs={"set": {"usd": 5.5}},
    )
    third = snapshot(
        "2026-09-24",
        printings={"a#holo": 14.0, "b#normal": 0.6},
        cards={"a": 14.0, "b": 0.6, "solo": 6.0},
        packs={"set": {"usd": 5.5}},
    )
    store(sessions, first, second, third)
    with sessions() as db:
        assert not price_history.recorded(db)
    price_history.backfill(sessions, None)

    # Unchanged days are not stored; the previous point carries the value.
    assert points(sessions, "printing", "a#holo") == [("2026-09-22", 10.0), ("2026-09-23", 14.0)]
    assert points(sessions, "printing", "a#reverseHolo") == [("2026-09-22", 12.0)]
    assert points(sessions, "pack", "set") == [("2026-09-22", 5.0), ("2026-09-23", 5.5)]

    since, moves = day(sessions, "2026-09-23")
    assert since == "2026-09-22"
    # A 20-cent card tripling is noise; a card's own price stands in only without printings.
    assert [(m["kind"], m["item"]) for m in moves["risers"]] == [("printing", "a#holo")]
    assert [(m["kind"], m["item"]) for m in moves["fallers"]] == [("card", "solo")]
    assert moves["changed"] == 3
    assert moves["packs"] == [{"kind": "pack", "item": "set", "before": 5.0, "after": 5.5}]
    assert day(sessions, "2026-09-24") == (
        "2026-09-23",
        {"changed": 0, "risers": [], "fallers": [], "packs": []},
    )
    assert day(sessions, "2026-09-22")[0] is None


def test_record_follows_activation_and_same_day_refresh_keeps_comparing_to_yesterday(sessions):
    first, second = snapshot("2026-09-22"), snapshot("2026-09-23", printings={"a#holo": 13.0})
    with sessions() as db:
        # Before the backfill there is no baseline; the backfill covers the snapshot later.
        price_history.record(db, first, second, "v2")
        db.commit()
        assert db.scalar(select(PricePoint.day).limit(1)) is None
    store(sessions, first)
    price_history.backfill(sessions, None)
    with sessions() as db:
        price_history.record(db, first, second, "v2")
        db.commit()
    again = snapshot("2026-09-23", printings={"a#holo": 15.0})
    with sessions() as db:
        price_history.record(db, second, again, "v3")
        db.commit()
    assert points(sessions, "printing", "a#holo") == [("2026-09-22", 10.0), ("2026-09-23", 15.0)]
    since, moves = day(sessions, "2026-09-23")
    assert since == "2026-09-22"
    assert moves["risers"][0] == {
        "kind": "printing",
        "item": "a#holo",
        "before": 10.0,
        "after": 15.0,
    }
    with sessions() as db:
        assert db.get(PriceDay, "2026-09-23").snapshot == "v3"


def test_history_window_opens_with_the_value_carried_in(sessions):
    payloads = [
        snapshot("2026-06-01"),
        snapshot("2026-08-01", printings={"a#holo": 11.0}),
        snapshot("2026-09-20", printings={"a#holo": 12.0}),
    ]
    store(sessions, *payloads)
    price_history.backfill(sessions, None)
    with sessions() as db:
        recent = price_history.history(db, "printing", "a#holo", 30)
        everything = price_history.history(db, "printing", "a#holo", 0)
        missing = price_history.history(db, "printing", "nope#holo", 30)
    assert recent["latest_day"] == "2026-09-20"
    assert recent["points"] == [
        {"day": "2026-08-21", "usd": 11.0},
        {"day": "2026-09-20", "usd": 12.0},
    ]
    assert [p["day"] for p in everything["points"]] == ["2026-06-01", "2026-08-01", "2026-09-20"]
    assert missing["points"] == []


def test_fresh_server_starts_history_from_the_bundled_snapshot(sessions):
    price_history.backfill(sessions, ("bundled", snapshot("2026-09-22")))
    with sessions() as db:
        assert price_history.recorded(db)
        assert db.get(PriceDay, "2026-09-22").snapshot == "bundled"
    assert points(sessions, "card", "solo") == [("2026-09-22", 8.0)]


def test_price_job_records_history_under_its_lease(tmp_path, sessions, monkeypatch):
    from app import jobs

    monkeypatch.setattr(jobs, "SessionLocal", sessions)

    class Rules:
        executable = str(tmp_path / "rules")

        def apply(self, *args, **kwargs):
            return {}, {}, "test"

    first = snapshot("2026-09-22")
    monkeypatch.setattr(jobs, "rules", Rules())
    monkeypatch.setattr(prices, "resources", lambda _: tmp_path)
    monkeypatch.setattr(prices, "bundled", lambda _: deepcopy(first))
    clock = [100000]
    monkeypatch.setattr(jobs.time, "time", lambda: clock[0])
    [version] = store(sessions, first)
    with sessions() as db:
        db.add(
            ServerJob(name="prices", next_run=0, lease_until=clock[0] + 60, active_snapshot=version)
        )
        db.commit()
    # A running refresh holds the lease; the backfill waits for the next turn.
    jobs.backfill_history()
    with sessions() as db:
        assert not price_history.recorded(db)
        db.get(ServerJob, "prices").lease_until = 0
        db.commit()
    jobs.backfill_history()
    with sessions() as db:
        assert price_history.recorded(db)
        job = db.get(ServerJob, "prices")
        assert job.owner is None and job.lease_until == 0 and job.next_run == 0

    def collect(args, **kwargs):
        from pathlib import Path

        candidate = snapshot("2026-09-23", printings={"a#holo": 16.0})
        Path(args[args.index("--snapshot") + 1]).write_text(json.dumps(candidate))

    monkeypatch.setattr(jobs.subprocess, "run", collect)
    jobs.refresh_prices()
    assert points(sessions, "printing", "a#holo") == [("2026-09-22", 10.0), ("2026-09-23", 16.0)]
    assert day(sessions, "2026-09-23")[1]["risers"][0]["after"] == 16.0


def test_movers_and_history_endpoints(auth):  # noqa: F811
    client, sessions = auth
    user = registered(client)
    first = snapshot("2026-09-22")
    second = snapshot(
        "2026-09-23", printings={"a#holo": 13.0, "a#reverseHolo": 11.0}, cards={"solo": 9.0}
    )
    empty = client.get("/v1/prices/movers", headers=headers(user)).json()
    assert empty["day"] is None and empty["owned"] == []
    store(sessions, first, second)
    price_history.backfill(sessions, None)
    with sessions() as db:
        db.add(
            ServerJob(
                name="prices", next_run=0, lease_until=0, active_snapshot=prices.encode(second)[0]
            )
        )
        account = db.get(Account, user["account_id"])
        state = deepcopy(account.state)
        state["cards"] = {"a": 3, "solo": 1}
        # "solo" has no printing price, so its own card price values it.
        state["printingCards"] = {"a#holo": 2, "a#reverseHolo": 1, "solo#holo": 1}
        account.state = state
        db.commit()
    reply = client.get("/v1/prices/movers", headers=headers(user)).json()
    assert (reply["day"], reply["since"]) == ("2026-09-23", "2026-09-22")
    assert [(m["item"], m["count"]) for m in reply["owned"]] == [
        ("a#holo", 2),
        ("a#reverseHolo", 1),
        ("solo", 1),
    ]
    assert [m["item"] for m in reply["risers"]] == ["a#holo", "solo"]
    assert [m["item"] for m in reply["fallers"]] == ["a#reverseHolo"]

    series = client.get(
        "/v1/prices/history",
        headers=headers(user),
        params={"kind": "printing", "item": "a#holo", "days": 90},
    ).json()
    assert series["points"] == [
        {"day": "2026-09-22", "usd": 10.0},
        {"day": "2026-09-23", "usd": 13.0},
    ]
    assert (
        client.get(
            "/v1/prices/history", headers=headers(user), params={"kind": "nope", "item": "a"}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/v1/prices/history",
            headers={**headers(user), "Authorization": ""},
            params={"kind": "pack", "item": "set"},
        ).status_code
        == 401
    )
