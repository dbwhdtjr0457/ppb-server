"""The daily rotation becomes server listings on the online market."""

import sqlalchemy as sa

from alembic import op

revision = "20261014_0008"
down_revision = "20261008_0007"
branch_labels = None
depends_on = None


def checks():
    # Batch mode rebuilds the table from reflection, which drops unnamed CHECKs.
    return (sa.CheckConstraint("quantity >= 0"), sa.CheckConstraint("unit_tokens > 0"))


def upgrade():
    # SQLite cannot drop NOT NULL in place; batch mode rebuilds the table.
    with op.batch_alter_table("market_listings", table_args=checks()) as batch:
        batch.alter_column("seller", existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column("source", sa.String(20), nullable=True))
    op.create_table(
        "market_purchases",
        sa.Column(
            "listing_id",
            sa.String(36),
            sa.ForeignKey("market_listings.id"),
            primary_key=True,
        ),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("market_purchases")
    op.execute("DELETE FROM market_listings WHERE seller IS NULL")
    with op.batch_alter_table("market_listings", table_args=checks()) as batch:
        batch.drop_column("source")
        batch.alter_column("seller", existing_type=sa.String(36), nullable=False)
