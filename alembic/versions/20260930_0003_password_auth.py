"""Password identities, revocable sessions and operator-only legacy linking."""

import sqlalchemy as sa

from alembic import op

revision = "20260930_0003"
down_revision = "20260930_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "password_identities",
        sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(512), nullable=False),
    )
    op.create_table(
        "login_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_login_sessions_account_id", "login_sessions", ["account_id"])
    op.create_table(
        "account_link_codes",
        sa.Column("code_hash", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_account_link_codes_account_id", "account_link_codes", ["account_id"])
    op.create_table(
        "auth_rate_limits",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("window", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.create_table(
        "auth_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.id")),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_auth_events_account_id", "auth_events", ["account_id"])


def downgrade():
    for table in [
        "auth_events",
        "auth_rate_limits",
        "account_link_codes",
        "login_sessions",
        "password_identities",
    ]:
        op.drop_table(table)
