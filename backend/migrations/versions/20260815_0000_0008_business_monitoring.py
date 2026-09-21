"""Create business-monitoring tables.

- ``businesses`` — 业务(逻辑视角)。
- ``service_interfaces`` — 被监控的 HTTP 接口(URL/方法/预期状态码/超时/开关)。
- ``business_servers`` — 业务↔服务器 多对多。
- ``business_interfaces`` — 业务↔接口 多对多。
- ``interface_probes`` — 接口最新探测结果(每接口一行,采集器 upsert)。

幂等:检查现有表后再建。

Revision ID: 0008_business_monitoring
Revises: 0007_create_docker_tables
Create Date: 2026-08-15 00:00:00.000001

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_business_monitoring"
down_revision: Union[str, None] = "0007_create_docker_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "businesses" not in tables:
        op.create_table(
            "businesses",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )

    if "service_interfaces" not in tables:
        op.create_table(
            "service_interfaces",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column(
                "method", sa.String(length=16), nullable=False, server_default="GET"
            ),
            sa.Column(
                "expected_status", sa.Integer(), nullable=False, server_default="200"
            ),
            sa.Column("timeout", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    if "business_servers" not in tables:
        op.create_table(
            "business_servers",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("business_id", sa.Integer(), nullable=False),
            sa.Column("device_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["business_id"], ["businesses.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("business_id", "device_id", name="uq_biz_device"),
        )

    if "business_interfaces" not in tables:
        op.create_table(
            "business_interfaces",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("business_id", sa.Integer(), nullable=False),
            sa.Column("interface_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["business_id"], ["businesses.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["interface_id"], ["service_interfaces.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("business_id", "interface_id", name="uq_biz_iface"),
        )

    if "interface_probes" not in tables:
        op.create_table(
            "interface_probes",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("interface_id", sa.Integer(), nullable=False),
            sa.Column("up", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("checked_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["interface_id"], ["service_interfaces.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("interface_id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    for t in (
        "interface_probes",
        "business_interfaces",
        "business_servers",
        "service_interfaces",
        "businesses",
    ):
        if t in tables:
            op.drop_table(t)
