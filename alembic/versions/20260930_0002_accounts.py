"""Account state, device collection cursors and atomic command journal."""

import sqlalchemy as sa

from alembic import op

revision = "20260930_0002"
down_revision = "20260929_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("revision >= 0"),
        sa.CheckConstraint("balance >= 0"),
    )
    op.create_table(
        "devices",
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("collected_total", sa.Integer(), nullable=False),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("collected_total >= 0"),
    )
    op.create_table(
        "game_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("command", sa.String(40), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("balance_before", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("rules_version", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("account_id", "request_id"),
        sa.UniqueConstraint("account_id", "revision"),
    )
    op.create_index("ix_game_events_account_id", "game_events", ["account_id"])


def downgrade():
    op.drop_table("game_events")
    op.drop_table("devices")
    op.drop_table("accounts")
