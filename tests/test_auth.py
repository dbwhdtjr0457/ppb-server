from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import admin
from app.auth_service import digest, hasher, issue_link_code, reset_password
from app.database import Base, get_db, make_engine
from app.game_api import get_rules
from app.game_service import initial_state
from app.main import app
from app.models import Account, AccountLinkCode, AuthEvent, LoginSession, PasswordIdentity

PASSWORD = "local test passphrase 2026!"


class Rules:
    def apply(self, state, command):
        state = deepcopy(state)
        if command["kind"] == "apply_tokens":
            state["usedSinceInstall"] += command["collected_total"]
        return state, {}, "test-rules"


@pytest.fixture
def auth(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/auth.db")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def database():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_rules] = Rules
    with TestClient(app) as client:
        yield client, sessions
    app.dependency_overrides.clear()
    engine.dispose()


def credentials(email="player@example.com", device=None, **extra):
    return {"email": email, "password": PASSWORD, "device_id": device or str(uuid4()), **extra}


def headers(reply):
    return {
        "Authorization": "Bearer " + reply["access_token"],
        "X-PPB-Account-ID": reply["account_id"],
        "X-PPB-Device-ID": reply["device_id"],
    }


def registered(client, **kwargs):
    response = client.post("/auth/register", json=credentials(**kwargs))
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_register_hashing_authentication_and_device_binding(auth):
    client, sessions = auth
    user = registered(client, email="Player@EXAMPLE.COM")
    assert user["email"] == "player@example.com"
    who = headers(user)
    assert client.get("/auth/me", headers=who).json()["email"] == user["email"]
    assert client.get("/v1/state", headers=who).status_code == 200
    assert client.get("/v1/state", headers={**who, "Authorization": ""}).status_code == 401
    assert (
        client.get("/v1/state", headers={**who, "X-PPB-Account-ID": str(uuid4())}).status_code
        == 403
    )
    assert (
        client.get("/v1/state", headers={**who, "X-PPB-Device-ID": str(uuid4())}).status_code == 401
    )
    with sessions() as db:
        identity = db.get(PasswordIdentity, user["account_id"])
        assert identity.password_hash.startswith("$argon2id$")
        assert hasher.verify(identity.password_hash, PASSWORD)
        assert PASSWORD not in identity.password_hash
        session = db.get(LoginSession, digest(user["access_token"]))
        assert session.token_hash != user["access_token"]
        assert session.expires_at == user["expires_at"]


def test_two_devices_share_resources_but_other_accounts_cannot(auth):
    client, _ = auth
    first = registered(client)
    second = client.post("/auth/login", json=credentials()).json()
    stranger = registered(client, email="stranger@example.com")
    assert second["account_id"] == first["account_id"]
    command = {
        "request_id": str(uuid4()),
        "expected_revision": 0,
        "command": {"kind": "report_tokens", "collected_total": 700},
    }
    assert client.post("/v1/commands", headers=headers(first), json=command).status_code == 200
    assert client.get("/v1/state", headers=headers(second)).json()["balance"] == 700
    assert client.get("/v1/state", headers=headers(stranger)).json()["balance"] == 0
    assert client.get("/v1/events", headers=headers(stranger)).json()["events"] == []
    spoof = {**headers(stranger), "X-PPB-Account-ID": first["account_id"]}
    assert client.post("/v1/commands", headers=spoof, json=command).status_code == 403


def test_wrong_password_unknown_email_and_rate_limit(auth):
    client, _ = auth
    registered(client)
    wrong = credentials(password="incorrect test passphrase")
    known = client.post("/auth/login", json=wrong)
    unknown = client.post("/auth/login", json={**wrong, "email": "unknown@example.com"})
    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json() == {"detail": "invalid_credentials"}
    replies = [client.post("/auth/login", json=wrong) for _ in range(12)]
    assert replies[-1].status_code == 429
    assert int(replies[-1].headers["retry-after"]) > 0


def test_email_password_registration_needs_no_code_and_invites_stay_optional(
    auth, monkeypatch
):
    client, sessions = auth
    monkeypatch.setattr(admin, "SessionLocal", sessions)
    monkeypatch.setattr(admin, "rules", Rules())
    # A deployment that still carries the old invite-only setting signs up normally.
    monkeypatch.setenv("PPB_REGISTRATION_MODE", "link-code-only")
    response = client.post("/auth/register", json=credentials())
    assert response.status_code == 201
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Account)) == 1
    # An operator-created account can still be claimed with its one-time code.
    invitation = admin.create_invite()
    claimed = client.post(
        "/auth/register",
        json=credentials(email="second@example.com", link_code=invitation["link_code"]),
    )
    assert claimed.status_code == 201
    assert claimed.json()["account_id"] == invitation["account_id"]
    reused = client.post(
        "/auth/register",
        json=credentials(email="third@example.com", link_code=invitation["link_code"]),
    )
    assert reused.status_code == 409
    assert client.post("/auth/login", json=credentials()).status_code == 200


def test_logout_expiry_and_revocation(auth):
    client, sessions = auth
    first = registered(client)
    second = client.post("/auth/login", json=credentials()).json()
    assert client.post("/auth/logout", headers=headers(first)).status_code == 204
    assert client.get("/v1/state", headers=headers(first)).status_code == 401
    assert client.get("/v1/state", headers=headers(second)).status_code == 200
    with sessions() as db:
        db.get(LoginSession, digest(second["access_token"])).expires_at = 0
        db.commit()
    assert client.get("/v1/state", headers=headers(second)).status_code == 401
    a = client.post("/auth/login", json=credentials()).json()
    b = client.post("/auth/login", json=credentials()).json()
    assert client.post("/auth/logout-all", headers=headers(a)).status_code == 204
    assert client.get("/v1/state", headers=headers(b)).status_code == 401


def test_password_change_and_operator_reset_revoke_all_sessions(auth):
    client, sessions = auth
    first = registered(client)
    second = client.post("/auth/login", json=credentials()).json()
    changed = "different test passphrase 2026"
    reply = client.post(
        "/auth/change-password",
        headers=headers(first),
        json={"current_password": PASSWORD, "new_password": changed},
    )
    assert reply.status_code == 204
    assert client.get("/auth/me", headers=headers(second)).status_code == 401
    assert client.post("/auth/login", json=credentials()).status_code == 401
    logged_in = client.post("/auth/login", json=credentials(password=changed)).json()
    with sessions() as db:
        reset_password(db, first["email"], PASSWORD)
    assert client.get("/auth/me", headers=headers(logged_in)).status_code == 401
    assert client.post("/auth/login", json=credentials()).status_code == 200


def test_legacy_account_link_is_one_time_preserves_everything(auth):
    client, sessions = auth
    account = str(uuid4())
    state = {**initial_state(), "usedSinceInstall": 900, "cards": {"base1-4": 3}}
    with sessions() as db:
        db.add(Account(id=account, revision=7, balance=900, state=state))
        db.commit()
        code = issue_link_code(db, account)
    # Knowing the old UUID is no longer proof of ownership.
    legacy = {"X-PPB-Account-ID": account, "X-PPB-Device-ID": str(uuid4())}
    assert client.get("/v1/state", headers=legacy).status_code == 401
    assert client.post("/auth/register", json=credentials(account_id=account)).status_code == 422
    linked = registered(client, link_code=code)
    assert linked["account_id"] == account
    snapshot = client.get("/v1/state", headers=headers(linked)).json()
    assert snapshot["revision"] == 7 and snapshot["state"] == state
    replay = client.post(
        "/auth/register", json=credentials(email="other@example.com", link_code=code)
    )
    assert replay.status_code == 409
    with sessions() as db:
        assert db.get(AccountLinkCode, digest(code)).consumed
        assert db.scalar(select(func.count()).select_from(PasswordIdentity)) == 1


def test_expired_link_and_concurrent_registration(auth):
    client, sessions = auth
    account = str(uuid4())
    with sessions() as db:
        db.add(Account(id=account, revision=0, balance=0, state=initial_state()))
        db.commit()
        code = issue_link_code(db, account)
        db.get(AccountLinkCode, digest(code)).expires_at = 0
        db.commit()
    assert client.post("/auth/register", json=credentials(link_code=code)).status_code == 409
    with ThreadPoolExecutor(2) as pool:
        replies = list(
            pool.map(lambda _: client.post("/auth/register", json=credentials()), range(2))
        )
    assert sorted(reply.status_code for reply in replies) == [201, 409]


@pytest.mark.parametrize("password", ["1234567", "x" * 129])
def test_password_validation_does_not_echo_secrets(auth, password):
    client, _ = auth
    response = client.post("/auth/register", json=credentials(password=password))
    assert response.status_code == 422
    assert password not in response.text


@pytest.mark.parametrize("password", ["Ab12!xyz", "x" * 128, "가나다라마바사아", "e\u0301" * 4])
def test_password_length_boundaries_register_login_change_and_reset(auth, password):
    client, sessions = auth
    user = registered(client, password=password)
    assert client.post("/auth/login", json=credentials(password=password)).status_code == 200
    for invalid in ("1234567", "x" * 129):
        response = client.post(
            "/auth/change-password",
            headers=headers(user),
            json={"current_password": password, "new_password": invalid},
        )
        assert response.status_code == 422
        assert invalid not in response.text
        with sessions() as db, pytest.raises(ValueError):
            reset_password(db, user["email"], invalid)
    changed = client.post(
        "/auth/change-password",
        headers=headers(user),
        json={"current_password": password, "new_password": password},
    )
    assert changed.status_code == 204
    assert client.get("/auth/me", headers=headers(user)).status_code == 401
    with sessions() as db:
        reset_password(db, user["email"], password)
    assert client.post("/auth/login", json=credentials(password=password)).status_code == 200


def test_auth_logs_do_not_contain_passwords_tokens_or_emails(auth):
    client, sessions = auth
    user = registered(client)
    client.post("/auth/logout", headers=headers(user))
    with sessions() as db:
        events = list(db.scalars(select(AuthEvent)))
        assert {event.action for event in events} == {"registered", "logged_out"}
        assert all(event.account_id == user["account_id"] for event in events)


def test_same_device_relogin_revokes_previous_token(auth):
    client, _ = auth
    first = registered(client)
    new = client.post("/auth/login", json=credentials(device=first["device_id"])).json()
    assert new["account_id"] == first["account_id"]
    assert new["access_token"] != first["access_token"]
    assert client.get("/v1/state", headers=headers(first)).status_code == 401
    assert client.get("/v1/state", headers=headers(new)).status_code == 200


def test_game_write_rechecks_revocation_inside_transaction(auth):
    from fastapi import HTTPException

    from app.game_schemas import CommandRequest
    from app.game_service import execute

    client, sessions = auth
    user = registered(client)
    assert client.post("/auth/logout", headers=headers(user)).status_code == 204
    command = CommandRequest.model_validate(
        {
            "request_id": str(uuid4()),
            "expected_revision": 0,
            "command": {"kind": "report_tokens", "collected_total": 500},
        }
    )
    # A token validated by the HTTP dependency can be revoked before a waiting
    # writer acquires the database lock. The write must independently reject it.
    with sessions() as db, pytest.raises(HTTPException) as denied:
        execute(
            db,
            user["account_id"],
            user["device_id"],
            command,
            Rules(),
            session_hash=digest(user["access_token"]),
        )
    assert denied.value.status_code == 401
    with sessions() as db:
        account = db.get(Account, user["account_id"])
        assert account.balance == 0 and account.revision == 0
