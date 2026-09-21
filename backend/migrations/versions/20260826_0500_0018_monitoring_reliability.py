"""Add Docker command diagnostics and business interface request validation settings."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_monitoring_reliability"
down_revision: Union[str, None] = "0017_alert_sustain_duration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "command_status_json" not in _columns("device_docker_status"):
        op.add_column(
            "device_docker_status",
            sa.Column("command_status_json", sa.Text(), nullable=True),
        )
    cols = _columns("service_interfaces")
    additions = {
        "request_headers_json": sa.Column(
            "request_headers_json", sa.Text(), nullable=True
        ),
        "auth_type": sa.Column(
            "auth_type", sa.String(length=16), nullable=False, server_default="none"
        ),
        "auth_username": sa.Column(
            "auth_username", sa.String(length=255), nullable=True
        ),
        "auth_password_enc": sa.Column("auth_password_enc", sa.Text(), nullable=True),
        "auth_token_enc": sa.Column("auth_token_enc", sa.Text(), nullable=True),
        "request_body": sa.Column("request_body", sa.Text(), nullable=True),
        "response_contains": sa.Column("response_contains", sa.Text(), nullable=True),
    }
    for name, column in additions.items():
        if name not in cols:
            op.add_column("service_interfaces", column)
    # 避免后续新建记录依赖数据库默认值，保留模型侧默认即可。
    if "auth_type" not in cols:
        op.alter_column("service_interfaces", "auth_type", server_default=None)


def downgrade() -> None:
    for table, name in (
        ("device_docker_status", "command_status_json"),
        ("service_interfaces", "response_contains"),
        ("service_interfaces", "request_body"),
        ("service_interfaces", "auth_token_enc"),
        ("service_interfaces", "auth_password_enc"),
        ("service_interfaces", "auth_username"),
        ("service_interfaces", "auth_type"),
        ("service_interfaces", "request_headers_json"),
    ):
        if name in _columns(table):
            op.drop_column(table, name)
