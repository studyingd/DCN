"""Create pve_connections table (PVE 平台连接配置).

Revision ID: 0009_pve_connections
Revises: 0008_business_monitoring
Create Date: 2026-08-15 00:00:00.000002

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_pve_connections"
down_revision: Union[str, None] = "0008_business_monitoring"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "pve_connections" not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            "pve_connections",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("host", sa.String(length=255), nullable=False),
            sa.Column("port", sa.Integer(), nullable=False, server_default="8006"),
            sa.Column("token_id", sa.String(length=255), nullable=False),
            sa.Column("token_secret_enc", sa.Text(), nullable=False),
            sa.Column("verify_ssl", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "pve_connections" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("pve_connections")
