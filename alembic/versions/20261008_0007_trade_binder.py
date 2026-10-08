"""Friends can browse each other's spare cards; trades can be countered."""

import sqlalchemy as sa

from alembic import op

revision = "20261008_0007"
down_revision = "20260930_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "social_profiles",
        sa.Column("trade_list_public", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("card_trades", sa.Column("counter_of", sa.String(36), nullable=True))


def downgrade():
    op.drop_column("card_trades", "counter_of")
    op.drop_column("social_profiles", "trade_list_public")
