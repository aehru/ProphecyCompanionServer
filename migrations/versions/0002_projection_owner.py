"""projections.owner: who shared the entry (gm PNJ vs player character)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-22

"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projections",
        sa.Column("owner", sa.String(length=8), nullable=False, server_default="player"),
    )


def downgrade() -> None:
    op.drop_column("projections", "owner")
