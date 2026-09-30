"""Self-service account controls; never expose another account or secret in logs."""

import hmac
import secrets
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select, text, update

from app.auth_api import Database, Identity
from app.auth_service import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    client_address,
    digest,
    gateway,
    hasher,
    normalize_email,
    rate_limit,
    require_session,
    verify_password,
)
from app.models import (
    AuthEvent,
    LoginSession,
    ManagedDevice,
    PasswordIdentity,
    RecoveryCode,
    TokenPolicy,
)

router = APIRouter(prefix="/auth", tags=["account"], dependencies=[Depends(gateway)])


class ConfirmPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class RenameDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("기기 이름을 확인하세요")
        return value.strip()


class SetTokenPolicy(ConfirmPassword):
    collector_device_id: UUID | None = None
    expected_version: int = Field(ge=0)


@router.get("/token-policy")
def token_policy(db: Database, who: Identity, response: Response):
    response.headers["Cache-Control"] = "no-store"
    row = db.get(TokenPolicy, who.account_id)
    return {
        "collector_device_id": row.collector_device_id if row else None,
        "version": row.version if row else 0,
    }


@router.post("/token-policy")
def set_token_policy(payload: SetTokenPolicy, db: Database, who: Identity, request: Request):
    rate_limit(db, who.account_id, client_address(request))
    db.execute(text("BEGIN IMMEDIATE"))
    authenticate_password(db, who, payload)
    target = str(payload.collector_device_id) if payload.collector_device_id else None
    # Designate only the currently authenticated installation; never guess someone else's ID.
    if target is not None and target != who.device_id:
        raise HTTPException(403, "collector_must_be_current_device")
    row = db.get(TokenPolicy, who.account_id)
    if row is None:
        row = TokenPolicy(account_id=who.account_id, version=0, collector_device_id=None)
        db.add(row)
    if row.version != payload.expected_version:
        raise HTTPException(409, "token_policy_changed")
    if row.collector_device_id != target:
        row.collector_device_id = target
        row.version += 1
        db.add(AuthEvent(account_id=who.account_id, action="token_policy_changed"))
    result = {"collector_device_id": row.collector_device_id, "version": row.version}
    db.commit()
    return result


class Recover(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    code: SecretStr = Field(min_length=32, max_length=128)
    new_password: SecretStr = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("email")
    @classmethod
    def email_address(cls, value):
        return normalize_email(value)


def authenticate_password(db, who, payload):
    require_session(db, who)
    user = db.get(PasswordIdentity, who.account_id)
    if not verify_password(payload.password.get_secret_value(), user.password_hash):
        raise HTTPException(401, "invalid_credentials")


@router.get("/devices")
def devices(db: Database, who: Identity, response: Response):
    response.headers["Cache-Control"] = "no-store"
    sessions = list(
        db.scalars(
            select(LoginSession).where(
                LoginSession.account_id == who.account_id,
                LoginSession.revoked.is_(False),
                LoginSession.expires_at > int(time.time()),
            )
        )
    )
    result = []
    for device_id in sorted({s.device_id for s in sessions}):
        device = db.get(ManagedDevice, (who.account_id, device_id))
        result.append(
            {
                "device_id": device_id,
                "name": device.name if device else "이전 연결 기기",
                "last_login": device.last_login if device else None,
                "current": device_id == who.device_id,
                "expires_at": max(s.expires_at for s in sessions if s.device_id == device_id),
            }
        )
    return {"items": result}


@router.post("/devices/{device_id}/rename", status_code=204)
def rename(device_id: UUID, payload: RenameDevice, db: Database, who: Identity):
    db.execute(text("BEGIN IMMEDIATE"))
    require_session(db, who)
    device = db.get(ManagedDevice, (who.account_id, str(device_id)))
    if device is None:
        raise HTTPException(404, "device_not_found")
    device.name = payload.name
    db.commit()


@router.post("/devices/{device_id}/revoke", status_code=204)
def revoke(device_id: UUID, db: Database, who: Identity):
    db.execute(text("BEGIN IMMEDIATE"))
    require_session(db, who)
    # Scoped to this account, including harmless retries after the target was revoked.
    db.execute(
        update(LoginSession)
        .where(
            LoginSession.account_id == who.account_id,
            LoginSession.device_id == str(device_id),
        )
        .values(revoked=True)
    )
    db.add(AuthEvent(account_id=who.account_id, action="device_revoked"))
    db.commit()


@router.get("/recovery")
def recovery_status(db: Database, who: Identity, response: Response):
    response.headers["Cache-Control"] = "no-store"
    code = db.get(RecoveryCode, who.account_id)
    return {
        "active": code is not None and not code.consumed,
        "created_at": code.created_at if code else None,
    }


@router.post("/recovery")
def issue_recovery(
    payload: ConfirmPassword, db: Database, who: Identity, request: Request, response: Response
):
    rate_limit(db, who.account_id, client_address(request))
    db.execute(text("BEGIN IMMEDIATE"))
    authenticate_password(db, who, payload)
    value = secrets.token_urlsafe(32)
    row = db.get(RecoveryCode, who.account_id)
    if row is None:
        row = RecoveryCode(account_id=who.account_id)
        db.add(row)
    row.code_hash, row.created_at, row.consumed = digest(value), int(time.time()), False
    db.add(AuthEvent(account_id=who.account_id, action="recovery_issued"))
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    # Not replayable: loss of this response requires explicit regeneration with password.
    return {"code": value, "created_at": row.created_at}


@router.post("/recover", status_code=204)
def recover(payload: Recover, db: Database, request: Request):
    rate_limit(db, payload.email, client_address(request))
    encoded = hasher.hash(payload.new_password.get_secret_value())
    db.execute(text("BEGIN IMMEDIATE"))
    user = db.scalar(select(PasswordIdentity).where(PasswordIdentity.email == payload.email))
    code = db.get(RecoveryCode, user.account_id) if user else None
    matches = hmac.compare_digest(
        code.code_hash if code else "0" * 64, digest(payload.code.get_secret_value())
    )
    if not matches or code is None or code.consumed:
        raise HTTPException(401, "invalid_recovery")
    user.password_hash, code.consumed = encoded, True
    db.execute(
        update(LoginSession)
        .where(
            LoginSession.account_id == user.account_id,
        )
        .values(revoked=True)
    )
    db.add(AuthEvent(account_id=user.account_id, action="account_recovered"))
    db.commit()
