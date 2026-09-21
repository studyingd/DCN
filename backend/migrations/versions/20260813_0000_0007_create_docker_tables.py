"""Create Docker container management tables.

- ``device_containers`` — 每台设备的容器快照(采集器每周期整批刷新)。
- ``device_docker_status`` — 每台设备 Docker 可用性/版本/容器数汇总。
- ``container_actions`` — 容器启停操作审计。

幂等:检查现有表后再建。

Revision ID: 0007_create_docker_tables
Revises: 0006_drop_user_groups
Create Date: 2026-08-13 00:00:00.000001

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_create_docker_tables"
down_revision: Union[str, None] = "0006_drop_user_groups"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "device_docker_status" not in tables:
        op.create_table(
            "device_docker_status",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("device_id", sa.Integer(), nullable=False),
            sa.Column("available", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("version", sa.Text(), nullable=True),
            sa.Column(
                "container_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("device_id"),
        )

    if "device_containers" not in tables:
        op.create_table(
            "device_containers",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("device_id", sa.Integer(), nullable=False),
            sa.Column("container_id", sa.Text(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("image", sa.Text(), nullable=True),
            sa.Column("state", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=True),
            sa.Column("ports", sa.Text(), nullable=True),
            sa.Column("cpu_pct", sa.Float(), nullable=True),
            sa.Column("mem_used_mb", sa.Integer(), nullable=True),
            sa.Column("mem_limit_mb", sa.Integer(), nullable=True),
            sa.Column("mem_pct", sa.Float(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_device_containers_device_name",
            "device_containers",
            ["device_id", "name"],
            unique=True,
        )

    if "container_actions" not in tables:
        op.create_table(
            "container_actions",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("device_id", sa.Integer(), nullable=True),
            sa.Column("device_name", sa.Text(), nullable=True),
            sa.Column("container_name", sa.Text(), nullable=False),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("success", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    for t in ("container_actions", "device_containers", "device_docker_status"):
        if t in tables:
            op.drop_table(t)
