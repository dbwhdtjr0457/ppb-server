import time
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth_service import (
    DUMMY_HASH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    Principal,
    client_address,
    create_session,
    digest,
    gateway,
    hasher,
    identity,
    normalize_email,
    rate_limit,
    require_session,
    valid_password,
    verify_password,
)
from app.database import get_db
from app.game_api import get_rules
from app.game_service import balance, initial_state
from app.models import Account, AccountLinkCode, AuthEvent, LoginSession, PasswordIdentity

router = APIRouter(prefix="/auth", tags=["authentication"], dependencies=[Depends(gateway)])
Database = Annotated[Session, Depends(get_db)]
Identity = Annotated[Principal, Depends(identity)]


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    password: SecretStr = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    device_id: UUID
    device_name: str = Field(default="Mac", min_length=1, max_length=80)

    @field_validator("email")
    @classmethod
    def email_address(cls, value):
        return normalize_email(value)


class Register(Login):
    link_code: SecretStr | None = Field(default=None, min_length=32, max_length=128)

    @field_validator("password")
    @classmethod
    def strong_password(cls, value):
        valid_password(value.get_secret_value())
        return value


class ChangePassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: SecretStr = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: SecretStr = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


@router.post("/register", status_code=201)
def register(
    payload: Register, request: Request, response: Response, db: Database, rules=Depends(get_rules)
):
    rate_limit(db, payload.email, client_address(request))
    encoded = hasher.hash(payload.password.get_secret_value())
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        if db.scalar(select(PasswordIdentity).where(PasswordIdentity.email == payload.email)):
            raise HTTPException(409, "registration_unavailable")
        if payload.link_code:
            link = db.get(AccountLinkCode, digest(payload.link_code.get_secret_value()))
            if (
                link is None
                or link.consumed
                or link.expires_at <= int(time.time())
                or db.get(PasswordIdentity, link.account_id) is not None
            ):
                raise HTTPException(409, "link_code_invalid")
            account_id = link.account_id
            link.consumed = True
        else:
            account_id = str(uuid4())
            state, _, _ = rules.apply(initial_state(), {"kind": "initialize"})
            db.add(Account(id=account_id, revision=0, balance=balance(state), state=state))
            db.flush()
        db.add(PasswordIdentity(account_id=account_id, email=payload.email, password_hash=encoded))
        result = create_session(
            db, account_id, str(payload.device_id), payload.email, payload.device_name
        )
        db.add(
            AuthEvent(
                account_id=account_id, action="legacy_linked" if payload.link_code else "registered"
            )
        )
        db.commit()
        response.headers["Cache-Control"] = "no-store"
        return result
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, "registration_unavailable") from error
    except BaseException:
        db.rollback()
        raise


@router.post("/login")
def login(payload: Login, request: Request, response: Response, db: Database):
    rate_limit(db, payload.email, client_address(request))
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        user = db.scalar(select(PasswordIdentity).where(PasswordIdentity.email == payload.email))
        valid = verify_password(
            payload.password.get_secret_value(), user.password_hash if user else DUMMY_HASH
        )
        if not valid or user is None:
            db.add(AuthEvent(action="login_failed"))
            db.commit()
            raise HTTPException(401, "invalid_credentials")
        if hasher.check_needs_rehash(user.password_hash):
            user.password_hash = hasher.hash(payload.password.get_secret_value())
        result = create_session(
            db, user.account_id, str(payload.device_id), user.email, payload.device_name
        )
        db.add(AuthEvent(account_id=user.account_id, action="logged_in"))
        db.commit()
        response.headers["Cache-Control"] = "no-store"
        return result
    except BaseException:
        db.rollback()
        raise


@router.get("/me")
def me(db: Database, who: Identity, response: Response):
    user = db.get(PasswordIdentity, who.account_id)
    response.headers["Cache-Control"] = "no-store"
    return {"account_id": who.account_id, "email": user.email, "device_id": who.device_id}


@router.post("/logout", status_code=204)
def logout(db: Database, who: Identity):
    db.execute(text("BEGIN IMMEDIATE"))
    session = require_session(db, who)
    session.revoked = True
    db.add(AuthEvent(account_id=who.account_id, action="logged_out"))
    db.commit()


@router.post("/logout-all", status_code=204)
def logout_all(db: Database, who: Identity):
    db.execute(text("BEGIN IMMEDIATE"))
    require_session(db, who)
    db.execute(
        update(LoginSession).where(LoginSession.account_id == who.account_id).values(revoked=True)
    )
    db.add(AuthEvent(account_id=who.account_id, action="all_sessions_revoked"))
    db.commit()


@router.post("/change-password", status_code=204)
def change_password(payload: ChangePassword, db: Database, who: Identity, request: Request):
    rate_limit(db, who.account_id, client_address(request))
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        require_session(db, who)
        user = db.get(PasswordIdentity, who.account_id)
        if not verify_password(payload.current_password.get_secret_value(), user.password_hash):
            db.add(AuthEvent(account_id=who.account_id, action="password_change_failed"))
            db.commit()
            raise HTTPException(401, "invalid_credentials")
        user.password_hash = hasher.hash(valid_password(payload.new_password.get_secret_value()))
        from app.models import RecoveryCode

        db.execute(
            update(RecoveryCode)
            .where(RecoveryCode.account_id == who.account_id)
            .values(consumed=True)
        )
        db.execute(
            update(LoginSession)
            .where(LoginSession.account_id == who.account_id)
            .values(revoked=True)
        )
        db.add(AuthEvent(account_id=who.account_id, action="password_changed"))
        db.commit()
    except BaseException:
        db.rollback()
        raise
