"""Renewal is best effort; failed or revoked authentication never extends a session."""

import time
from uuid import uuid4

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_auth import Rules, auth, headers, registered  # noqa: F401

from app.auth_service import SESSION_SECONDS, digest
from app.models import LoginSession


def test_busy_renewal_keeps_valid_request_available_and_retries(auth, monkeypatch):  # noqa: F811
    client, sessions = auth
    user = registered(client)
    key = digest(user["access_token"])
    original_expiry = int(time.time()) + 3 * 86400
    with sessions() as db:
        db.get(LoginSession, key).expires_at = original_expiry
        db.commit()
    execute = Session.execute

    def busy_update(self, statement, *args, **kwargs):
        table = getattr(statement, "table", None)
        if table is not None and table.name == "login_sessions":
            raise OperationalError("synthetic renewal", {}, RuntimeError("database is locked"))
        return execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", busy_update)
    assert client.get("/v1/state", headers=headers(user)).status_code == 200
    with sessions() as db:
        assert db.get(LoginSession, key).expires_at == original_expiry
    monkeypatch.setattr(Session, "execute", execute)
    assert client.get("/v1/state", headers=headers(user)).status_code == 200
    with sessions() as db:
        assert db.get(LoginSession, key).expires_at >= int(time.time()) + SESSION_SECONDS - 60


def test_revoked_or_mismatched_authentication_cannot_extend_old_session(auth):  # noqa: F811
    client, sessions = auth
    user = registered(client)
    key = digest(user["access_token"])
    old_expiry = int(time.time()) + 3 * 86400
    with sessions() as db:
        db.get(LoginSession, key).expires_at = old_expiry
        db.commit()
    wrong_account = {**headers(user), "X-PPB-Account-ID": str(uuid4())}
    assert client.get("/v1/state", headers=wrong_account).status_code == 403
    wrong_device = {**headers(user), "X-PPB-Device-ID": str(uuid4())}
    assert client.get("/v1/state", headers=wrong_device).status_code == 401
    with sessions() as db:
        row = db.get(LoginSession, key)
        assert row.expires_at == old_expiry
        row.revoked = True
        db.commit()
    assert client.get("/v1/state", headers=headers(user)).status_code == 401
    with sessions() as db:
        assert db.get(LoginSession, key).expires_at == old_expiry
