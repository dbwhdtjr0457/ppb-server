import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi import HTTPException
from test_auth import Rules, credentials, headers, registered
from test_auth import auth as auth_fixture

from app.models import Account, OpeningJob

auth = auth_fixture


@pytest.fixture
def opening(auth, monkeypatch):
    client, sessions = auth
    original = Rules.apply

    def apply(self, state, command):
        if command["kind"] != "open_packs":
            return original(self, state, command)
        state = deepcopy(state)
        count = command["count"]
        if state["packs"].get(command["set_id"], 0) < count:
            raise HTTPException(409, "not_enough_packs")
        state["packs"][command["set_id"]] -= count
        state["cards"]["a-1"] = state["cards"].get("a-1", 0) + count
        return state, {"packs": {"packs": [], "completions": []}}, "test-rules"

    monkeypatch.setattr(Rules, "apply", apply)
    user = registered(client)
    with sessions() as db:
        account = db.get(Account, user["account_id"])
        account.state = {**account.state, "packs": {"a": 2500}}
        db.commit()
    return client, sessions, user


def create(client, user):
    payload = {
        "request_id": str(uuid4()),
        "expected_revision": 0,
        "set_id": "a",
        "count": 2500,
        "rules_version": "test-rules",
    }
    response = client.post("/v1/opening-jobs", headers=headers(user), json=payload)
    assert response.status_code == 200, response.text
    assert client.post("/v1/opening-jobs", headers=headers(user), json=payload).status_code == 200
    return response.json()["result"]["job"]


def test_resume_replay_two_devices_and_atomic_progress(opening):
    client, sessions, user = opening
    job = create(client, user)
    path = f"/v1/opening-jobs/{job['id']}/step"
    body = {"request_id": str(uuid4()), "expected_revision": 0, "target_version": 0}
    first = client.post(path, headers=headers(user), json=body)
    assert first.status_code == 200, first.text
    assert first.json()["result"]["job"]["completed"] == 1000
    replay = client.post(path, headers=headers(user), json=body)
    assert replay.json()["replayed"] is True
    assert replay.json()["snapshot"]["state"]["packs"]["a"] == 1500
    other_device = client.post("/auth/login", json=credentials()).json()
    rows = client.get("/v1/opening-jobs", headers=headers(other_device)).json()["items"]
    assert rows[0]["completed"] == 1000
    for version in [1, 2]:
        reply = client.post(
            path,
            headers=headers(other_device),
            json={
                "request_id": str(uuid4()),
                "expected_revision": version,
                "target_version": version,
            },
        )
        assert reply.status_code == 200, reply.text
    with sessions() as db:
        stored = db.get(OpeningJob, job["id"])
        assert stored.completed == 2500 and stored.status == "completed"
        state = db.get(Account, user["account_id"]).state
        assert state["packs"]["a"] == 0 and state["cards"]["a-1"] == 2500


def test_concurrent_step_cancel_and_mode_change_never_redraw(opening):
    client, sessions, user = opening
    job = create(client, user)
    path = f"/v1/opening-jobs/{job['id']}"
    bodies = [
        {"request_id": str(uuid4()), "expected_revision": 0, "target_version": 0} for _ in range(2)
    ]
    with ThreadPoolExecutor(2) as pool:
        replies = list(
            pool.map(lambda b: client.post(path + "/step", headers=headers(user), json=b), bodies)
        )
    assert sorted(r.status_code for r in replies) == [200, 409]
    with sessions() as db:
        account = db.get(Account, user["account_id"])
        account.state = {**account.state, "openingMode": "realistic"}
        db.commit()
    body = {"request_id": str(uuid4()), "expected_revision": 1, "target_version": 1}
    assert client.post(path + "/step", headers=headers(user), json=body).status_code == 409
    with sessions() as db:
        assert db.get(OpeningJob, job["id"]).completed == 1000
        assert db.get(Account, user["account_id"]).state["packs"]["a"] == 1500
    assert client.post(path + "/cancel", headers=headers(user), json=body).status_code == 200
    assert client.post(path + "/cancel", headers=headers(user), json=body).status_code == 200
    body["request_id"] = str(uuid4())
    assert client.post(path + "/step", headers=headers(user), json=body).status_code == 409


def test_jobs_are_private_and_rule_failure_rolls_back(opening, monkeypatch):
    client, sessions, user = opening
    job = create(client, user)
    stranger = registered(client, email="stranger@example.com")
    path = f"/v1/opening-jobs/{job['id']}/step"
    body = {"request_id": str(uuid4()), "expected_revision": 0, "target_version": 0}
    assert client.get("/v1/opening-jobs", headers=headers(stranger)).json()["items"] == []
    assert client.post(path, headers=headers(stranger), json=body).status_code == 404

    def fail(*args, **kwargs):
        raise HTTPException(503, "rules_failed")

    monkeypatch.setattr(Rules, "apply", fail)
    assert client.post(path, headers=headers(user), json=body).status_code == 503
    with sessions() as db:
        assert db.get(OpeningJob, job["id"]).completed == 0
        assert db.get(Account, user["account_id"]).state["packs"]["a"] == 2500


@pytest.mark.parametrize(
    "backend", ["python"] + (["swift"] if os.getenv("PPB_TEST_RULES_EXECUTABLE") else [])
)
def test_real_native_job_preserves_statistics_and_replay(auth, backend):
    from app.game_api import get_rules
    from app.main import app
    from app.native_rules import PythonRules
    from app.rules import SwiftRules

    client, sessions = auth
    rules = (
        PythonRules()
        if backend == "python"
        else SwiftRules(os.environ["PPB_TEST_RULES_EXECUTABLE"])
    )
    app.dependency_overrides[get_rules] = lambda: rules
    user = registered(client)
    with sessions() as db:
        account = db.get(Account, user["account_id"])
        account.state = {**account.state, "packs": {"sv8pt5": 1001}}
        db.commit()
    version = client.get("/v1/rules", headers=headers(user)).json()["rules_version"]
    reply = client.post(
        "/v1/opening-jobs",
        headers=headers(user),
        json={
            "request_id": str(uuid4()),
            "expected_revision": 0,
            "set_id": "sv8pt5",
            "count": 1001,
            "rules_version": version,
        },
    )
    assert reply.status_code == 200, reply.text
    job = reply.json()["result"]["job"]
    path = f"/v1/opening-jobs/{job['id']}/step"
    for revision, size in [(0, 1000), (1, 1)]:
        body = {
            "request_id": str(uuid4()),
            "expected_revision": revision,
            "target_version": revision,
        }
        reply = client.post(path, headers=headers(user), json=body)
        assert reply.status_code == 200, reply.text
        assert len(reply.json()["result"]["packs"]["packs"]) == size
        replay = client.post(path, headers=headers(user), json=body)
        assert replay.json()["result"] == reply.json()["result"]
    stats = client.get("/v1/stats", headers=headers(user)).json()
    assert stats["totals"]["opened"] == 1001
    assert reply.json()["result"]["job"]["status"] == "completed"
    assert reply.json()["snapshot"]["state"]["packsOpened"] == 1001
