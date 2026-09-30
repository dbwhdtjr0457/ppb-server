"""No UUID-only fallback. Passwords use Argon2id; random sessions/link codes
are stored only as SHA-256 digests. Database access is the operator boundary.
"""

import hashlib
import hmac
import ipaddress
import secrets
import time
from typing import Annotated, NamedTuple
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from email_validator import EmailNotValidError, validate_email
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import (
    Account,
    AccountLinkCode,
    AuthEvent,
    AuthRateLimit,
    LoginSession,
    ManagedDevice,
    PasswordIdentity,
    RecoveryCode,
)

SESSION_SECONDS = 7 * 24 * 60 * 60
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_email(value: str) -> str:
    try:
        return validate_email(value.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as error:
        raise ValueError("올바른 이메일 주소를 입력하세요") from error


def valid_password(value: str) -> str:
    # No truncation, trimming, arbitrary character classes, or silent normalization.
    if not PASSWORD_MIN_LENGTH <= len(value) <= PASSWORD_MAX_LENGTH:
        raise ValueError(f"비밀번호는 {PASSWORD_MIN_LENGTH}~{PASSWORD_MAX_LENGTH}자여야 합니다")
    return value


def verify_password(password: str, encoded: str) -> bool:
    try:
        return hasher.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


class Principal(NamedTuple):
    account_id: str
    device_id: str
    session_hash: str | None


def gateway(x_ppb_gateway_key: Annotated[str, Header()] = ""):
    if settings.gateway_key and not hmac.compare_digest(settings.gateway_key, x_ppb_gateway_key):
        raise HTTPException(403, "gateway_access_denied")


def local_peer(request: Request) -> bool:
    try:
        return request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def client_address(request: Request) -> str:
    """Trust only the explicitly enabled, loopback Cloudflare connector, never XFF."""
    address = request.client.host if request.client else "unknown"
    if settings.trust_cloudflare_proxy and local_peer(request):
        forwarded = request.headers.get("cf-connecting-ip")
        if forwarded is not None:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError as error:
                raise HTTPException(400, "invalid_proxy_address") from error
    return address


def diagnostics_allowed(request: Request) -> bool:
    # A connector is also loopback: forwarding headers distinguish it from local probes.
    return local_peer(request) and not any(
        header in request.headers
        for header in ("cf-connecting-ip", "cf-ray", "forwarded", "x-forwarded-for")
    )


def require_session(db: Session, who: Principal) -> LoginSession:
    session = db.get(LoginSession, who.session_hash)
    if (
        session is None
        or session.revoked
        or session.expires_at <= int(time.time())
        or session.account_id != who.account_id
        or session.device_id != who.device_id
    ):
        raise HTTPException(401, "login_required", headers={"WWW-Authenticate": "Bearer"})
    return session


def identity(
    x_ppb_device_id: Annotated[UUID, Header()],
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str, Header()] = "",
    x_ppb_account_id: Annotated[UUID | None, Header()] = None,
    _=Depends(gateway),
) -> Principal:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not 32 <= len(token) <= 256:
        raise HTTPException(401, "login_required", headers={"WWW-Authenticate": "Bearer"})
    session = db.get(LoginSession, digest(token))
    if session is None:
        raise HTTPException(401, "login_required", headers={"WWW-Authenticate": "Bearer"})
    who = Principal(session.account_id, str(x_ppb_device_id), session.token_hash)
    require_session(db, who)
    if x_ppb_account_id is not None and str(x_ppb_account_id) != who.account_id:
        raise HTTPException(403, "account_mismatch")
    # Game commands acquire their write lock before reading account resources.
    db.rollback()
    return who


def rate_limit(db: Session, email: str, address: str):
    now = int(time.time())
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        db.execute(delete(AuthRateLimit).where(AuthRateLimit.window < now // 60 - 10))
        exceeded = False
        for key, limit in [(digest("email:" + email), 10), (digest("ip:" + address), 30)]:
            row = db.get(AuthRateLimit, key)
            if row is None:
                row = AuthRateLimit(key=key, window=now // 60, attempts=0)
                db.add(row)
            if row.window != now // 60:
                row.window, row.attempts = now // 60, 0
            row.attempts += 1
            exceeded |= row.attempts > limit
        if exceeded:
            db.add(AuthEvent(action="rate_limited"))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    if exceeded:
        raise HTTPException(429, "too_many_attempts", headers={"Retry-After": str(60 - now % 60)})


def create_session(db: Session, account_id: str, device_id: str, email: str, name: str = "Mac"):
    # Logging in again on the same installation invalidates its old session.
    db.execute(
        update(LoginSession)
        .where(LoginSession.account_id == account_id, LoginSession.device_id == device_id)
        .values(revoked=True)
    )
    token = secrets.token_urlsafe(32)
    expires = int(time.time()) + SESSION_SECONDS
    device = db.get(ManagedDevice, (account_id, device_id))
    if device is None:
        device = ManagedDevice(account_id=account_id, device_id=device_id, name=name)
        db.add(device)
    device.last_login = int(time.time())
    db.add(
        LoginSession(
            token_hash=digest(token),
            account_id=account_id,
            device_id=device_id,
            expires_at=expires,
            revoked=False,
        )
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires,
        "account_id": account_id,
        "device_id": device_id,
        "email": email,
    }


def new_link_code(db: Session, account_id: str) -> str:
    """Stage a code inside the caller's write transaction; never commit independently."""
    db.execute(
        update(AccountLinkCode)
        .where(AccountLinkCode.account_id == account_id)
        .values(consumed=True)
    )
    code = secrets.token_urlsafe(32)
    db.add(
        AccountLinkCode(
            code_hash=digest(code),
            account_id=account_id,
            expires_at=int(time.time()) + 600,
            consumed=False,
        )
    )
    db.add(AuthEvent(account_id=account_id, action="link_code_issued"))
    return code


def issue_link_code(db: Session, account_id: str) -> str:
    account_id = str(UUID(account_id))
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        if db.get(Account, account_id) is None or db.get(PasswordIdentity, account_id) is not None:
            raise ValueError("An existing unlinked account is required")
        code = new_link_code(db, account_id)
        db.commit()
        return code
    except BaseException:
        db.rollback()
        raise


def reset_password(db: Session, email: str, password: str):
    email = normalize_email(email)
    encoded = hasher.hash(valid_password(password))
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        user = db.scalar(select(PasswordIdentity).where(PasswordIdentity.email == email))
        if user is None:
            raise ValueError("Account not found")
        user.password_hash = encoded
        db.execute(
            update(RecoveryCode)
            .where(RecoveryCode.account_id == user.account_id)
            .values(consumed=True)
        )
        db.execute(
            update(LoginSession)
            .where(LoginSession.account_id == user.account_id)
            .values(revoked=True)
        )
        db.add(AuthEvent(account_id=user.account_id, action="operator_password_reset"))
        db.commit()
    except BaseException:
        db.rollback()
        raise
