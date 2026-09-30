from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi import Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.auth_service import Principal, identity
from app.database import Base, get_db, make_engine
from app.game_api import get_rules
from app.game_service import initial_state, snapshot
from app.main import app
from app.models import Account, GameEvent


def game_test_identity(x_ppb_account_id: str = Header(), x_ppb_device_id: str = Header()):
    # This module isolates game transactions. test_auth.py tests real credentials.
    return Principal(x_ppb_account_id, x_ppb_device_id, None)


class TestRules:
    def apply(self, state, command):
        state = deepcopy(state)
        if command["kind"] == "initialize":
            return state, {}, "test-rule-v1"
        if command["kind"] == "apply_tokens":
            state["usedSinceInstall"] += command["collected_total"]
            return state, {}, "test-rule-v1"
        if command["kind"] != "buy_packs" or state["usedSinceInstall"] - state["spentTokens"] < 10:
            raise HTTPException(409, "game_precondition_failed")
        state["spentTokens"] += 10
        state["packs"]["base1"] = state["packs"].get("base1", 0) + command["count"]
        return state, {}, "test-rule-v1"


@pytest.fixture
def game(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/game.db")
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)

    def database():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_rules] = TestRules
    app.dependency_overrides[identity] = game_test_identity
    with TestClient(app) as client:
        yield client, sessions
    app.dependency_overrides.clear()
    engine.dispose()


def headers(account=None, device=None):
    return {"X-PPB-Account-ID": account or str(uuid4()), "X-PPB-Device-ID": device or str(uuid4())}


def request(command, revision=0, request_id=None):
    return {
        "request_id": request_id or str(uuid4()),
        "expected_revision": revision,
        "command": command,
    }


def test_credit_dedup_and_device_high_water(game):
    client, sessions = game
    who = headers()
    body = request({"kind": "report_tokens", "collected_total": 100})
    first = client.post("/v1/commands", headers=who, json=body)
    assert first.status_code == 200
    assert first.json()["snapshot"]["balance"] == 100
    replay = client.post("/v1/commands", headers=who, json=body)
    assert replay.json()["replayed"] is True
    assert replay.json()["snapshot"]["revision"] == 1
    for total, revision in [(100, 1), (50, 2), (120, 3)]:
        reply = client.post(
            "/v1/commands",
            headers=who,
            json=request({"kind": "report_tokens", "collected_total": total}, revision),
        )
        assert reply.status_code == 200
    assert reply.json()["snapshot"]["balance"] == 120
    # Replaying an older operation returns CURRENT state, not a rollback snapshot.
    assert client.post("/v1/commands", headers=who, json=body).json()["snapshot"]["revision"] == 4
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(GameEvent)) == 4


def test_rule_version_conflict_rolls_back_credit_and_cursor(game):
    client, _ = game
    who = headers()
    body = request({"kind": "report_tokens", "collected_total": 100})
    body["rules_version"] = "outdated"
    assert client.post("/v1/commands", headers=who, json=body).status_code == 409
    assert client.get("/v1/state", headers=who).json()["revision"] == 0
    body["rules_version"] = "test-rule-v1"
    reply = client.post("/v1/commands", headers=who, json=body)
    assert reply.status_code == 200
    assert reply.json()["snapshot"]["balance"] == 100


def test_account_headers_required_and_internal_commands_not_public(game):
    client, _ = game
    assert client.get("/v1/state").status_code == 422
    assert (
        client.post(
            "/v1/commands",
            headers=headers(),
            json=request({"kind": "apply_tokens", "collected_total": 100}),
        ).status_code
        == 422
    )


def test_polling_returns_no_wallet_body_when_unchanged(game):
    client, _ = game
    who = headers()
    first = client.get("/v1/state", headers=who)
    cached = {**who, "If-None-Match": first.headers["etag"]}
    unchanged = client.get("/v1/state", headers=cached)
    assert unchanged.status_code == 304 and unchanged.content == b""
    client.post(
        "/v1/commands", headers=who, json=request({"kind": "report_tokens", "collected_total": 10})
    )
    assert client.get("/v1/state", headers=cached).json()["balance"] == 10


def test_account_isolation_and_second_device(game):
    client, _ = game
    first = headers()
    second = headers(first["X-PPB-Account-ID"])
    client.post(
        "/v1/commands",
        headers=first,
        json=request({"kind": "report_tokens", "collected_total": 100}),
    )
    response = client.post(
        "/v1/commands",
        headers=second,
        json=request({"kind": "report_tokens", "collected_total": 20}, 1),
    )
    assert response.json()["snapshot"]["balance"] == 120
    stranger = headers()
    assert client.get("/v1/state", headers=stranger).json()["balance"] == 0
    assert client.get("/v1/events", headers=stranger).json()["events"] == []
    assert len(client.get("/v1/events?limit=1", headers=second).json()["events"]) == 1


def test_conflict_and_reused_id(game):
    client, _ = game
    who = headers()
    body = request({"kind": "report_tokens", "collected_total": 100})
    client.post("/v1/commands", headers=who, json=body)
    body["command"]["collected_total"] = 200
    assert client.post("/v1/commands", headers=who, json=body).status_code == 409
    assert (
        client.post(
            "/v1/commands",
            headers=who,
            json=request({"kind": "buy_packs", "set_id": "base1", "count": 1}),
        ).status_code
        == 409
    )
    assert client.get("/v1/state", headers=who).json()["balance"] == 100


def test_failed_rule_rolls_back_everything(game):
    client, sessions = game
    who = headers()
    response = client.post(
        "/v1/commands",
        headers=who,
        json=request({"kind": "buy_packs", "set_id": "base1", "count": 1}),
    )
    assert response.status_code == 409
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Account)) == 0
        assert db.scalar(select(func.count()).select_from(GameEvent)) == 0


@pytest.mark.parametrize(
    "command",
    [
        {"kind": "report_tokens", "collected_total": -1},
        {"kind": "report_tokens", "collected_total": True},
        {"kind": "report_tokens", "collected_total": 10**16},
        {"kind": "buy_packs", "set_id": "base1", "count": 0},
        {"kind": "buy_packs", "set_id": "base1", "count": 1, "price": 0},
        {"kind": "open_packs", "set_id": "base1", "count": 1, "seed": 1},
        {"kind": "replace_state", "state": {}},
    ],
)
def test_untrusted_fields_rejected(game, command):
    client, _ = game
    assert client.post("/v1/commands", headers=headers(), json=request(command)).status_code == 422


def test_concurrent_purchase_only_one_revision_wins(game):
    client, _ = game
    who = headers()
    client.post(
        "/v1/commands", headers=who, json=request({"kind": "report_tokens", "collected_total": 10})
    )
    bodies = [request({"kind": "buy_packs", "set_id": "base1", "count": 1}, 1) for _ in range(2)]
    with ThreadPoolExecutor(2) as pool:
        replies = list(
            pool.map(lambda body: client.post("/v1/commands", headers=who, json=body), bodies)
        )
    assert sorted(reply.status_code for reply in replies) == [200, 409]
    state = client.get("/v1/state", headers=who).json()
    assert state["balance"] == 0
    assert state["state"]["packs"] == {"base1": 1}


def test_concurrent_identical_request_is_applied_once(game):
    client, _ = game
    who = headers()
    body = request({"kind": "report_tokens", "collected_total": 100})
    with ThreadPoolExecutor(2) as pool:
        replies = list(
            pool.map(lambda _: client.post("/v1/commands", headers=who, json=body), range(2))
        )
    assert [r.status_code for r in replies] == [200, 200]
    assert sorted(r.json()["replayed"] for r in replies) == [False, True]


def test_unopened_oripa_mapping_not_exposed():
    state = initial_state()
    state["oripa"] = {"cards": ["c", "b", "a"], "opened": [1], "serial": 1}
    account = Account(id=str(uuid4()), state=state, revision=1, balance=0)
    public = snapshot(account)["state"]["oripa"]
    assert public["cards"] == ["a", "b", "c"]
    assert account.state["oripa"]["cards"] == ["c", "b", "a"]
