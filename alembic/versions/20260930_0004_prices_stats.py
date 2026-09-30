"""Immutable online prices, durable job leases and event-based statistics."""

import sqlalchemy as sa

from alembic import op

revision = "20260930_0004"
down_revision = "20260930_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "price_snapshots",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "server_jobs",
        sa.Column("name", sa.String(40), primary_key=True),
        sa.Column("next_run", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(36)),
        sa.Column("active_snapshot", sa.String(64)),
        sa.Column("last_success", sa.Integer()),
        sa.Column("error", sa.String(200)),
    )
    op.create_table(
        "game_statistics",
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("game_events.id"), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("set_id", sa.String(160)),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
    )
    op.create_index("ix_game_statistics_account_id", "game_statistics", ["account_id"])


def downgrade():
    op.drop_table("game_statistics")
    op.drop_table("server_jobs")
    op.drop_table("price_snapshots")
