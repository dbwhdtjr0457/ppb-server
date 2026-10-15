"""Trades can carry money; buyers can offer a price on a listing."""

import sqlalchemy as sa

from alembic import op

revision = "20261015_0009"
down_revision = "20261014_0008"
branch_labels = None
depends_on = None


def upgrade():
    for column in ("offered_tokens", "requested_tokens"):
        op.add_column(
            "card_trades",
            sa.Column(column, sa.Integer(), nullable=False, server_default="0"),
        )
    op.create_table(
        "market_offers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("listing_id", sa.String(36), sa.ForeignKey("market_listings.id"), nullable=False),
        sa.Column("buyer", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_tokens", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity >= 1"),
        sa.CheckConstraint("unit_tokens > 0"),
    )
    op.create_index("ix_market_offers_listing_id", "market_offers", ["listing_id"])
    op.create_index("ix_market_offers_buyer", "market_offers", ["buyer"])
    op.create_index("ix_market_offers_expiry", "market_offers", ["status", "expires_at"])


def downgrade():
    op.drop_table("market_offers")
    with op.batch_alter_table("card_trades") as batch:
        batch.drop_column("requested_tokens")
        batch.drop_column("offered_tokens")
