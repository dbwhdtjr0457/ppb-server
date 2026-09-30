"""Social privacy, projected inventory, escrow, trades and marketplace receipts."""

import sqlalchemy as sa

from alembic import op

revision = "20260930_0005"
down_revision = "20260930_0004"
branch_labels = None
depends_on = None


def account(name, primary=False):
    return sa.Column(
        name, sa.String(36), sa.ForeignKey("accounts.id"), primary_key=primary, nullable=False
    )


def upgrade():
    op.create_table(
        "social_profiles",
        account("account_id", True),
        sa.Column("public_id", sa.String(36), unique=True, nullable=False),
        sa.Column("friend_code", sa.String(32), unique=True, nullable=False),
        sa.Column("nickname", sa.String(40), nullable=False),
        sa.Column("collection_public", sa.Boolean(), nullable=False),
        sa.Column("wishlist_public", sa.Boolean(), nullable=False),
        sa.Column("binder_public", sa.Boolean(), nullable=False),
        sa.Column("wishlist", sa.JSON(), nullable=False),
        sa.Column("binder", sa.JSON(), nullable=False),
    )
    op.create_table(
        "friendships",
        sa.Column("id", sa.String(36), primary_key=True),
        account("sender"),
        account("recipient"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
    )
    op.create_table("user_blocks", account("actor", True), account("other", True))
    op.create_table(
        "inventory",
        account("account_id", True),
        sa.Column("printing", sa.String(160), primary_key=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity >= 0"),
    )
    op.create_table(
        "reservations",
        account("account_id", True),
        sa.Column("printing", sa.String(160), primary_key=True),
        sa.Column("owner", sa.String(36), primary_key=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0"),
    )
    op.create_table(
        "card_trades",
        sa.Column("id", sa.String(36), primary_key=True),
        account("sender"),
        account("recipient"),
        sa.Column("offered", sa.JSON(), nullable=False),
        sa.Column("requested", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
    )
    op.create_table(
        "market_listings",
        sa.Column("id", sa.String(36), primary_key=True),
        account("seller"),
        sa.Column("printing", sa.String(160), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_tokens", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity >= 0"),
        sa.CheckConstraint("unit_tokens > 0"),
    )
    op.create_table(
        "online_receipts",
        account("account_id", True),
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        account("account_id"),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("target", sa.String(160), nullable=False),
        sa.Column("read", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )
    for table, columns in {
        "friendships": ["sender", "recipient"],
        "card_trades": ["sender", "recipient"],
        "market_listings": ["seller", "printing"],
        "notifications": ["account_id"],
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for table in [
        "notifications",
        "online_receipts",
        "market_listings",
        "card_trades",
        "reservations",
        "inventory",
        "user_blocks",
        "friendships",
        "social_profiles",
    ]:
        op.drop_table(table)
