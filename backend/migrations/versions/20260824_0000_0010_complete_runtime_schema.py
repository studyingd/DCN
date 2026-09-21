"""Complete runtime schema for sessions, monitoring, and permission data."""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_complete_runtime_schema"
down_revision: Union[str, None] = "0009_pve_connections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()

    if "session_version" not in _columns(bind, "users"):
        op.add_column(
            "users",
            sa.Column(
                "session_version", sa.Integer(), nullable=False, server_default="0"
            ),
        )

    if "winrm_port" not in _columns(bind, "devices"):
        op.add_column(
            "devices",
            sa.Column(
                "winrm_port", sa.Integer(), nullable=False, server_default="5985"
            ),
        )

    tables = _tables(bind)
    if "device_metric_samples" not in tables:
        op.create_table(
            "device_metric_samples",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("device_id", sa.Integer(), nullable=False),
            sa.Column("ts", sa.DateTime(), nullable=False),
            sa.Column("cpu_pct", sa.Float(), nullable=True),
            sa.Column("mem_pct", sa.Float(), nullable=True),
            sa.Column("mem_used_mb", sa.Integer(), nullable=True),
            sa.Column("mem_total_mb", sa.Integer(), nullable=True),
            sa.Column("disk_max_pct", sa.Float(), nullable=True),
            sa.Column("disks_json", sa.Text(), nullable=True),
            sa.Column("load1", sa.Float(), nullable=True),
            sa.Column("load5", sa.Float(), nullable=True),
            sa.Column("load15", sa.Float(), nullable=True),
            sa.Column("uptime_sec", sa.BigInteger(), nullable=True),
            sa.Column("net_rx_bps", sa.Float(), nullable=True),
            sa.Column("net_tx_bps", sa.Float(), nullable=True),
            sa.Column("disk_read_bps", sa.Float(), nullable=True),
            sa.Column("disk_write_bps", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_device_metric_samples_device_ts",
            "device_metric_samples",
            ["device_id", "ts"],
        )

    if "agent_runs" not in tables:
        op.create_table(
            "agent_runs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("device_id", sa.Integer(), nullable=True),
            sa.Column("device_name", sa.Text(), nullable=False),
            sa.Column("device_ip", sa.Text(), nullable=True),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column(
                "status", sa.String(length=32), nullable=False, server_default="running"
            ),
            sa.Column("steps_json", sa.Text(), nullable=True),
            sa.Column("report", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_agent_runs_created_at", "agent_runs", ["created_at"])

    permission_map = {
        "device:read": "device:view",
        "room:read": "device:view",
        "device:write": "device:manage",
        "device:delete": "device:manage",
        "room:write": "device:manage",
        "device:terminal": "device:remote",
        "credential:read": "credential:manage",
        "credential:write": "credential:manage",
    }
    rows = bind.execute(sa.text("SELECT id, permissions FROM roles")).fetchall()
    for role_id, raw in rows:
        try:
            old = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            old = []
        new = list(dict.fromkeys(permission_map.get(p, p) for p in old))
        if "script:manage" in new and "inspection:manage" not in new:
            new.append("inspection:manage")
        if "device:remote" in new and "script:manage" not in new:
            new.append("script:manage")
        if new != old:
            bind.execute(
                sa.text("UPDATE roles SET permissions=:permissions WHERE id=:id"),
                {"permissions": json.dumps(new), "id": role_id},
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "agent_runs" in tables:
        op.drop_index("ix_agent_runs_created_at", table_name="agent_runs")
        op.drop_table("agent_runs")
    if "device_metric_samples" in tables:
        op.drop_index(
            "ix_device_metric_samples_device_ts", table_name="device_metric_samples"
        )
        op.drop_table("device_metric_samples")
    if "winrm_port" in _columns(bind, "devices"):
        op.drop_column("devices", "winrm_port")
    if "session_version" in _columns(bind, "users"):
        op.drop_column("users", "session_version")
