"""Deterministic gates prove concurrency; no timing benchmark or production data."""

import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.orm import sessionmaker

from app import inventory, prices
from app.auth_service import Principal
from app.database import Base, make_engine
from app.game_schemas import CommandRequest
from app.game_service import execute, initial_state
from app.models import (
    Account,
    Device,
    GameEvent,
    Inventory,
    LoginSession,
    MarketListing,
    PriceSnapshot,
    Reservation,
    ServerJob,
    TokenPolicy,
)
from app.online_schemas import OnlineCommand


class Rules:
    supports_context = True

    def __init__(self, gate=None):
        self.gate = gate

    def apply(self, state, command, **context):
        state = deepcopy(state)
        if command["kind"] == "apply_tokens":
            if self.gate and command["collected_total"] == 100:
                self.gate()
            state["usedSinceInstall"] += command["collected_total"]
        return state, {}, "test-rules"


@pytest.fixture
def database(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/performance.db")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    accounts = [str(uuid4()), str(uuid4())]
    with sessions() as db:
        for account_id in accounts:
            state = initial_state()
            state.update(usedSinceInstall=1000, cards={"a": 3}, printingCards={"a#normal": 3})
            db.add(Account(id=account_id, revision=0, balance=1000, state=state))
        db.commit()
    yield sessions, accounts, str(uuid4())
    engine.dispose()


def credit(amount=100):
    return CommandRequest(
        request_id=uuid4(),
        expected_revision=0,
        command={"kind": "report_tokens", "collected_total": amount},
    )


def run(sessions, account, device, body, rules, **kwargs):
    with sessions() as db:
        return execute(db, account, device, body, rules, **kwargs)


def test_slow_rule_does_not_block_another_account_commit(database):
    sessions, (first, second), device = database
    entered, release = Event(), Event()

    def pause():
        entered.set()
        assert release.wait(5), "test did not release pure computation"

    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(run, sessions, first, device, credit(), Rules(pause))
        try:
            assert entered.wait(2)
            other = pool.submit(run, sessions, second, device, credit(7), Rules())
            assert other.result(timeout=2)["snapshot"]["balance"] == 1007
            assert not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=2)["snapshot"]["balance"] == 1100


@pytest.mark.parametrize("identical", [False, True])
def test_simultaneous_compute_has_one_durable_effect(database, identical):
    sessions, (account, _), device = database
    barrier = Barrier(2)
    rules = Rules(lambda: barrier.wait(timeout=5))
    body = credit()
    bodies = [body, body if identical else credit()]
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run, sessions, account, device, item, rules) for item in bodies]
        replies, failures = [], []
        for future in futures:
            try:
                replies.append(future.result(timeout=5))
            except HTTPException as error:
                failures.append(error)
    if identical:
        assert not failures
        assert sorted(reply["replayed"] for reply in replies) == [False, True]
    else:
        assert len(replies) == len(failures) == 1
        assert failures[0].status_code == 409
        assert failures[0].detail == {"code": "revision_conflict", "revision": 1}
    with sessions() as db:
        assert db.get(Account, account).balance == 1100
        assert db.get(Device, (account, device)).collected_total == 100
        assert db.scalar(select(func.count()).select_from(GameEvent)) == 1


def publish(db, value):
    version, data = prices.encode({"schemaVersion": 1, "value": value})
    db.add(PriceSnapshot(id=version, data=data))
    job = db.get(ServerJob, "prices")
    if job is None:
        job = ServerJob(name="prices", active_snapshot=version)
        db.add(job)
    else:
        job.active_snapshot = version
    return version


@pytest.mark.parametrize(
    ("changed", "status", "detail"),
    [
        ("policy", 409, "token_policy_changed"),
        ("reservation", 409, "reserved_printings_changed"),
        ("prices", 409, "price_version_changed"),
        ("session", 401, "login_required"),
        ("state", 409, {"code": "revision_conflict", "revision": 0}),
        ("balance", 409, {"code": "revision_conflict", "revision": 0}),
    ],
)
def test_mutable_inputs_are_rechecked_after_compute(database, changed, status, detail):
    sessions, (account, _), device = database
    token_hash = "a" * 64
    with sessions() as db:
        db.add(
            LoginSession(
                token_hash=token_hash,
                account_id=account,
                device_id=device,
                expires_at=int(time.time()) + 60,
                revoked=False,
            )
        )
        db.commit()
    entered, release = Event(), Event()

    def pause():
        entered.set()
        assert release.wait(5)

    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(
            run, sessions, account, device, credit(), Rules(pause), session_hash=token_hash
        )
        try:
            assert entered.wait(2)

            def change():
                with sessions() as db:
                    if changed == "policy":
                        db.add(TokenPolicy(account_id=account, version=1))
                    elif changed == "reservation":
                        db.add(
                            Reservation(
                                account_id=account,
                                printing="a#normal",
                                owner=str(uuid4()),
                                quantity=1,
                            )
                        )
                    elif changed == "prices":
                        publish(db, 2)
                    elif changed == "state":
                        stored = db.get(Account, account)
                        stored.state = {**stored.state, "favoriteCardID": "a"}
                    elif changed == "balance":
                        db.get(Account, account).balance = 999
                    else:
                        db.get(LoginSession, token_hash).revoked = True
                    db.commit()

            pool.submit(change).result(timeout=2)
        finally:
            release.set()
        with pytest.raises(HTTPException) as rejected:
            pending.result(timeout=2)
        assert rejected.value.status_code == status
        assert rejected.value.detail == detail
    with sessions() as db:
        assert db.get(Account, account).balance == (999 if changed == "balance" else 1000)
        if changed == "state":
            assert db.get(Account, account).state["favoriteCardID"] == "a"
        assert db.get(Account, account).revision == 0
        assert db.get(Device, (account, device)) is None
        assert db.scalar(select(func.count()).select_from(GameEvent)) == 0


def test_callback_failure_rolls_back_game_device_and_receipt(database):
    sessions, (account, _), device = database

    def fail(db, account, result):
        result["job"] = {"completed": 1000}
        raise HTTPException(409, "opening_job_changed")

    with pytest.raises(HTTPException):
        run(sessions, account, device, credit(), Rules(), after_apply=fail)
    with sessions() as db:
        assert db.get(Account, account).revision == 0
        assert db.get(Device, (account, device)) is None
        assert db.scalar(select(func.count()).select_from(GameEvent)) == 0


@pytest.mark.parametrize("projection", ["complete", "missing", "partial", "stale", "extra"])
def test_unchanged_printings_skip_only_a_verified_complete_projection(
    database, monkeypatch, projection
):
    sessions, (account, _), device = database
    expected = {"a#normal": 3, "b#normal": 2}
    with sessions() as db:
        stored = db.get(Account, account)
        stored.state = {**stored.state, "cards": {"a": 3, "b": 2}, "printingCards": expected}
        rows = {} if projection == "missing" else dict(expected)
        if projection == "partial":
            rows.pop("b#normal")
        if projection == "stale":
            rows["a#normal"] = 2
        if projection == "extra":
            rows["c#normal"] = 1
        for printing, quantity in rows.items():
            db.add(Inventory(account_id=account, printing=printing, quantity=quantity))
        db.commit()
    original, calls = inventory.sync, []

    def track(db, account):
        calls.append(account.id)
        return original(db, account)

    monkeypatch.setattr(inventory, "sync", track)
    run(sessions, account, device, credit(), Rules())
    assert len(calls) == (0 if projection == "complete" else 1)
    with sessions() as db:
        projected = dict(
            db.execute(
                select(Inventory.printing, Inventory.quantity).where(
                    Inventory.account_id == account
                )
            ).all()
        )
        assert projected == expected


@pytest.mark.parametrize("legacy", [False, True])
def test_changed_or_legacy_printings_keep_projection_materialization(database, monkeypatch, legacy):
    sessions, (account, _), device = database
    with sessions() as db:
        stored = db.get(Account, account)
        if legacy:
            stored.state = {**stored.state, "printingCards": {"a#normal": 1}}
        db.add(Inventory(account_id=account, printing="a#normal", quantity=3))
        db.commit()

    class PrintingRules(Rules):
        def apply(self, state, command, **context):
            state, result, version = super().apply(state, command, **context)
            state["cards"]["a"] = 3 if legacy else 4
            state["printingCards"]["a#normal"] = state["cards"]["a"]
            return state, result, version

    original, calls = inventory.sync, []

    def track(db, account):
        calls.append(account.id)
        return original(db, account)

    monkeypatch.setattr(inventory, "sync", track)
    run(sessions, account, device, credit(), PrintingRules())
    assert calls == [account]
    with sessions() as db:
        assert db.get(Inventory, (account, "a#normal")).quantity == (3 if legacy else 4)


@pytest.mark.parametrize("quoted", [10, 11])
def test_combined_native_quote_computes_once_and_validates_price(database, quoted):
    sessions, (account, _), device = database
    with sessions() as db:
        version = publish(db, 1)
        db.commit()

    class CombinedRules(Rules):
        calls = 0

        def apply(self, *args, **kwargs):
            pytest.fail("combined pricing must not make a separate rules call")

        def apply_with_quote(self, state, command, **context):
            self.calls += 1
            state = deepcopy(state)
            state["spentTokens"] += 10
            state["packs"]["base1"] = 1
            return state, {}, "test-rules", 10

    rules = CombinedRules()
    body = CommandRequest(
        request_id=uuid4(),
        expected_revision=0,
        command={"kind": "buy_packs", "set_id": "base1", "count": 1},
        price_version=version,
        quoted_tokens=quoted,
    )
    if quoted == 10:
        assert run(sessions, account, device, body, rules)["snapshot"]["balance"] == 990
    else:
        with pytest.raises(HTTPException) as rejected:
            run(sessions, account, device, body, rules)
        assert rejected.value.detail == "quote_changed"
        with sessions() as db:
            assert db.get(Account, account).balance == 1000
    assert rules.calls == 1


def test_price_version_cache_reuses_decoded_and_encoded_payload(database):
    sessions, _, _ = database
    engine = sessions.kw["bind"]
    with sessions() as db:
        first = publish(db, 1)
        db.commit()
    loads = []

    def capture(connection, cursor, statement, parameters, context, many):
        if statement.startswith("SELECT") and "price_snapshots.data" in statement:
            loads.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        payloads, encoded = [], []
        for _ in range(3):
            with sessions() as db:
                assert prices.current_version(db, Rules()) == first
                version, payload = prices.current(db, Rules())
                assert version == first
                payloads.append(payload)
                encoded.append(prices.current_encoded(db, Rules())[1])
        assert len(loads) == 1
        assert all(payload is payloads[0] for payload in payloads)
        assert all(data is encoded[0] for data in encoded)
        with sessions() as db:
            second = publish(db, 2)
            db.commit()
        with sessions() as db:
            assert prices.current_version(db, Rules()) == second
            assert prices.current(db, Rules())[1] == {"schemaVersion": 1, "value": 2}
        assert len(loads) == 2
        assert payloads[0]["value"] == 1
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("field", ["krwPerUsd", "krwPerUSD"])
def test_price_validation_preserves_currency_conversion_with_either_json_alias(field):
    from test_insights import pair

    previous = pair()
    previous["cardPrices"].pop("krwPerUSD")
    previous["cardPrices"][field] = 1368.52
    candidate = deepcopy(previous)
    prices.validate(candidate, previous)
    candidate["cardPrices"][field] = 1
    with pytest.raises(ValueError, match="preserve"):
        prices.validate(candidate, previous)
    candidate["cardPrices"].pop(field)
    with pytest.raises(ValueError, match="Invalid currency"):
        prices.validate(candidate, previous)
    candidate["cardPrices"].update(krwPerUsd=1368.52, krwPerUSD=1)
    with pytest.raises(ValueError, match="Conflicting"):
        prices.validate(candidate, previous)


def test_online_journal_loads_balances_only_for_touched_accounts(database):
    from app.online_service import execute as online_execute

    sessions, (account, _), device = database
    engine = sessions.kw["bind"]
    account_reads = []

    def capture(connection, cursor, statement, parameters, context, many):
        if statement.startswith("SELECT") and "FROM accounts" in statement:
            account_reads.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with sessions() as db:
            online_execute(
                db,
                Principal(account, device, None),
                OnlineCommand(
                    request_id=uuid4(), expected_revision=0, action="profile", nickname="Trainer"
                ),
                Rules(),
            )
        assert account_reads and all("WHERE accounts.id" in sql for sql in account_reads)
        with sessions() as db:
            recorded = db.scalar(select(GameEvent))
            assert recorded.balance_before == recorded.balance_after == 1000
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("buyer_first", [False, True])
def test_market_journals_preserve_each_previous_balance_in_either_account_order(
    database, buyer_first
):
    from app.online_service import execute as online_execute

    sessions, accounts, device = database
    seller, buyer = sorted(accounts, reverse=buyer_first)
    identifier = str(uuid4())
    with sessions() as db:
        account = db.get(Account, buyer)
        account.state = {**account.state, "usedSinceInstall": 7000}
        account.balance = 7000
        db.add(
            MarketListing(
                id=identifier,
                seller=seller,
                printing="a#normal",
                quantity=1,
                unit_tokens=10,
                status="active",
                version=0,
                expires_at=int(time.time()) + 60,
                created_at=int(time.time()),
            )
        )
        db.add(Reservation(account_id=seller, printing="a#normal", owner=identifier, quantity=1))
        db.commit()

    class TransferRules(Rules):
        def apply(self, state, command, **context):
            state = deepcopy(state)
            for field, sign in [("remove", -1), ("add", 1)]:
                for printing, count in command[field].items():
                    state["printingCards"][printing] += sign * count
                    state["cards"][printing.split("#")[0]] += sign * count
            state["marketEarnedTokens"] = command["market_credit"]
            state["marketSpentTokens"] = command["market_debit"]
            return state, {}, "test-rules"

    with sessions() as db:
        online_execute(
            db,
            Principal(buyer, device, None),
            OnlineCommand(
                request_id=uuid4(),
                expected_revision=0,
                action="listing_buy",
                target_id=identifier,
                target_version=0,
                quantity=1,
                unit_tokens=10,
            ),
            TransferRules(),
        )
    with sessions() as db:
        journals = {
            row.account_id: (row.balance_before, row.balance_after)
            for row in db.scalars(select(GameEvent))
        }
        assert journals == {seller: (1000, 1010), buyer: (7000, 6990)}
