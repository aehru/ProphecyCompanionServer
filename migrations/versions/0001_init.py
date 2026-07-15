"""initial schema: campaigns + projections

Revision ID: 0001
Revises:
Create Date: 2026-07-15

"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("gm_token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_campaigns_code", "campaigns", ["code"], unique=True)
    op.create_table(
        "projections",
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("char_id", sa.String(length=64), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("projections")
    op.drop_index("ix_campaigns_code", table_name="campaigns")
    op.drop_table("campaigns")
