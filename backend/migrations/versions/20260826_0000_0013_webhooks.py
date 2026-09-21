"""Add webhook configuration storage."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_webhooks"
down_revision: Union[str, None] = "0012_unified_automation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "webhooks" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "webhooks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret_enc", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("headers", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_test_at", sa.DateTime(), nullable=True),
        sa.Column("last_test_status", sa.String(length=24), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhooks_enabled", "webhooks", ["enabled"])


def downgrade() -> None:
    bind = op.get_bind()
    if "webhooks" in sa.inspect(bind).get_table_names():
        op.drop_index("ix_webhooks_enabled", table_name="webhooks")
        op.drop_table("webhooks")
