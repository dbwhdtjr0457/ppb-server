"""Private device labels and single-use account recovery."""

import sqlalchemy as sa

from alembic import op

revision = "20260930_0006"
down_revision = "20260930_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "devices", sa.Column("policy_version", sa.Integer(), nullable=False, server_default="0")
    )
    op.create_table(
        "token_policies",
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("collector_device_id", sa.String(36), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "opening_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("set_id", sa.String(160), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("completed", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("opening_mode", sa.String(20), nullable=False),
        sa.Column("rules_version", sa.String(300), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint("completed >= 0 AND completed <= total"),
    )
    op.create_index("ix_opening_jobs_account_id", "opening_jobs", ["account_id"])
    for table in ("friendships", "card_trades", "market_listings"):
        op.create_index(f"ix_{table}_expiry", table, ["status", "expires_at"])
    op.create_table(
        "managed_devices",
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("device_id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("last_login", sa.Integer(), nullable=False),
    )
    op.create_table(
        "recovery_codes",
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False),
    )


def downgrade():
    op.drop_table("token_policies")
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("policy_version")
    op.drop_table("opening_jobs")
    for table in ("friendships", "card_trades", "market_listings"):
        op.drop_index(f"ix_{table}_expiry", table_name=table)
    op.drop_table("recovery_codes")
    op.drop_table("managed_devices")
