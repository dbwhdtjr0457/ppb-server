"""Daily price history: changed values per day and each day's biggest moves."""

import sqlalchemy as sa

from alembic import op

revision = "20261023_0010"
down_revision = "20261015_0009"
branch_labels = None
depends_on = None


def upgrade():
    # Filled from the stored snapshots by the price job once the server starts.
    op.create_table(
        "price_points",
        sa.Column("kind", sa.String(8), primary_key=True),
        sa.Column("item", sa.String(160), primary_key=True),
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("usd", sa.Float(), nullable=False),
        sqlite_with_rowid=False,
    )
    op.create_index("ix_price_points_day", "price_points", ["day"])
    op.create_table(
        "price_days",
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("snapshot", sa.String(64), nullable=False),
        sa.Column("since", sa.String(10)),
        sa.Column("moves", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("price_days")
    op.drop_index("ix_price_points_day", table_name="price_points")
    op.drop_table("price_points")
