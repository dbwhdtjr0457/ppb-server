from datetime import UTC, datetime

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


def utcnow():
    return datetime.now(UTC)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (CheckConstraint("revision >= 0"), CheckConstraint("balance >= 0"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(default=0)
    balance: Mapped[int] = mapped_column(default=0)
    # Versioned canonical PPB state, not client-overwritable synchronization data.
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (CheckConstraint("collected_total >= 0"),)

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    collected_total: Mapped[int] = mapped_column(default=0)
    policy_version: Mapped[int] = mapped_column(default=0)
    seen_at: Mapped[datetime] = mapped_column(default=utcnow)


class GameEvent(Base):
    __tablename__ = "game_events"
    __table_args__ = (
        UniqueConstraint("account_id", "request_id"),
        UniqueConstraint("account_id", "revision"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    device_id: Mapped[str] = mapped_column(String(36))
    request_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int]
    command: Mapped[str] = mapped_column(String(40))
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    balance_before: Mapped[int]
    balance_after: Mapped[int]
    rules_version: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PasswordIdentity(Base):
    __tablename__ = "password_identities"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    device_id: Mapped[str] = mapped_column(String(36))
    expires_at: Mapped[int]
    revoked: Mapped[bool] = mapped_column(default=False)


class AccountLinkCode(Base):
    __tablename__ = "account_link_codes"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    expires_at: Mapped[int]
    consumed: Mapped[bool] = mapped_column(default=False)


class ManagedDevice(Base):
    __tablename__ = "managed_devices"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    last_login: Mapped[int]


class TokenPolicy(Base):
    __tablename__ = "token_policies"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    collector_device_id: Mapped[str | None] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(default=0)


class RecoveryCode(Base):
    __tablename__ = "recovery_codes"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[int]
    consumed: Mapped[bool] = mapped_column(default=False)


class OpeningJob(Base):
    __tablename__ = "opening_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    set_id: Mapped[str] = mapped_column(String(160))
    total: Mapped[int]
    completed: Mapped[int] = mapped_column(default=0)
    version: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")
    opening_mode: Mapped[str] = mapped_column(String(20))
    rules_version: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[int]
    __table_args__ = (CheckConstraint("completed >= 0 AND completed <= total"),)


class AuthRateLimit(Base):
    __tablename__ = "auth_rate_limits"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window: Mapped[int]
    attempts: Mapped[int]


class AuthEvent(Base):
    __tablename__ = "auth_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    action: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ServerJob(Base):
    __tablename__ = "server_jobs"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    next_run: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[int] = mapped_column(default=0)
    owner: Mapped[str | None] = mapped_column(String(36))
    active_snapshot: Mapped[str | None] = mapped_column(String(64))
    last_success: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(String(200))


class GameStatistic(Base):
    __tablename__ = "game_statistics"
    event_id: Mapped[int] = mapped_column(ForeignKey("game_events.id"), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    created_at: Mapped[datetime]
    set_id: Mapped[str | None] = mapped_column(String(160))
    mode: Mapped[str] = mapped_column(String(20))
    metrics: Mapped[dict] = mapped_column(JSON)


class SocialProfile(Base):
    __tablename__ = "social_profiles"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    friend_code: Mapped[str] = mapped_column(String(32), unique=True)
    nickname: Mapped[str] = mapped_column(String(40))
    collection_public: Mapped[bool] = mapped_column(default=False)
    wishlist_public: Mapped[bool] = mapped_column(default=False)
    binder_public: Mapped[bool] = mapped_column(default=False)
    # Spare copies a friend may request in a trade. Public by default: trades
    # only happen between friends, and spares are what a trade binder holds.
    trade_list_public: Mapped[bool] = mapped_column(default=True)
    wishlist: Mapped[list] = mapped_column(JSON, default=list)
    binder: Mapped[list] = mapped_column(JSON, default=list)


class Friendship(Base):
    __tablename__ = "friendships"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sender: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    recipient: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[int]


class UserBlock(Base):
    __tablename__ = "user_blocks"
    actor: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    other: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)


class Inventory(Base):
    __tablename__ = "inventory"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    printing: Mapped[str] = mapped_column(String(160), primary_key=True)
    quantity: Mapped[int]
    __table_args__ = (CheckConstraint("quantity >= 0"),)


class Reservation(Base):
    __tablename__ = "reservations"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    printing: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner: Mapped[str] = mapped_column(String(36), primary_key=True)
    quantity: Mapped[int]
    __table_args__ = (CheckConstraint("quantity > 0"),)


class CardTrade(Base):
    __tablename__ = "card_trades"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sender: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    recipient: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    offered: Mapped[dict] = mapped_column(JSON)
    requested: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[int]
    # The trade this one answers with changed cards, if it is a counter-offer.
    counter_of: Mapped[str | None] = mapped_column(String(36), default=None)


class MarketListing(Base):
    __tablename__ = "market_listings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    seller: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    printing: Mapped[str] = mapped_column(String(160), index=True)
    quantity: Mapped[int]
    unit_tokens: Mapped[int]
    status: Mapped[str] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[int]
    created_at: Mapped[int]
    __table_args__ = (CheckConstraint("quantity >= 0"), CheckConstraint("unit_tokens > 0"))


class OnlineReceipt(Base):
    __tablename__ = "online_receipts"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    target: Mapped[str] = mapped_column(String(160))
    read: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[int]
