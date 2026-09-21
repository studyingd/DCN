"""Drop audit_logs and session_recordings tables; trim daily_stats columns.

Removes the entire audit + session-recording subsystem:
- ``audit_logs`` table (the ``AuditLog`` model and all ``log_audit`` write sites
  have been deleted).
- ``session_recordings`` table (the ``SessionRecording`` model, the asciinema/RDP
  recording pipeline, and the replay endpoints have been removed).
- ``session_count`` / ``script_execution_count`` / ``login_count`` columns on
  ``daily_stats_snapshots`` (their only data source was ``audit_logs``; with it
  gone they would be permanently 0).

Terminal sessions no longer persist recordings.

Idempotent: inspects existing tables/columns before each change, so it is safe
to re-run or to run against a DB where the startup hook already dropped them.

Revision ID: 0004_drop_audit_and_recordings
Revises: 0003_recording_dimensions
Create Date: 2026-07-05 00:00:00.000001

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_drop_audit_and_recordings"
down_revision: Union[str, None] = "0003_recording_dimensions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "audit_logs" in tables:
        op.drop_table("audit_logs")
    if "session_recordings" in tables:
        op.drop_table("session_recordings")

    if "daily_stats_snapshots" in tables:
        existing = _columns(bind, "daily_stats_snapshots")
        for name in ("session_count", "script_execution_count", "login_count"):
            if name in existing:
                op.drop_column("daily_stats_snapshots", name)


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "daily_stats_snapshots" in tables:
        existing = _columns(bind, "daily_stats_snapshots")
        if "session_count" not in existing:
            op.add_column(
                "daily_stats_snapshots",
                sa.Column(
                    "session_count", sa.Integer(), nullable=False, server_default="0"
                ),
            )
        if "script_execution_count" not in existing:
            op.add_column(
                "daily_stats_snapshots",
                sa.Column(
                    "script_execution_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
            )
        if "login_count" not in existing:
            op.add_column(
                "daily_stats_snapshots",
                sa.Column(
                    "login_count", sa.Integer(), nullable=False, server_default="0"
                ),
            )

    if "session_recordings" not in tables:
        op.create_table(
            "session_recordings",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("session_id", sa.String(length=255), nullable=False),
            sa.Column("device_id", sa.Integer(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("device_name", sa.Text(), nullable=True),
            sa.Column("device_ip", sa.Text(), nullable=True),
            sa.Column("username", sa.Text(), nullable=True),
            sa.Column("conn_type", sa.Text(), nullable=False),
            sa.Column("file_path", sa.Text(), nullable=True),
            sa.Column("file_size", sa.Integer(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("ended_at", sa.DateTime(), nullable=True),
            sa.Column("duration_seconds", sa.Integer(), nullable=True),
            sa.Column("video_path", sa.Text(), nullable=True),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("session_id"),
        )

    if "audit_logs" not in tables:
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("session_id", sa.Text(), nullable=True),
            sa.Column("device_id", sa.Integer(), nullable=True),
            sa.Column("device_name", sa.Text(), nullable=True),
            sa.Column("device_ip", sa.Text(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("username", sa.Text(), nullable=True),
            sa.Column("event_type", sa.Text(), nullable=False),
            sa.Column("command", sa.Text(), nullable=True),
            sa.Column("blocked", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
