"""Add unified automation jobs, targets and steps."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_unified_automation"
down_revision: Union[str, None] = "0011_repair_session_version"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "automation_jobs" not in tables:
        op.create_table(
            "automation_jobs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("job_type", sa.String(length=24), nullable=False),
            sa.Column("trigger_type", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("risk_level", sa.String(length=20), nullable=False),
            sa.Column("config_json", sa.JSON(), nullable=False),
            sa.Column("summary_json", sa.JSON(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_by_name", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_automation_jobs_created_at", "automation_jobs", ["created_at"]
        )
        op.create_index(
            "ix_automation_jobs_type_status", "automation_jobs", ["job_type", "status"]
        )

    if "automation_job_targets" not in tables:
        op.create_table(
            "automation_job_targets",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column("device_id", sa.Integer(), nullable=True),
            sa.Column("device_name", sa.String(length=255), nullable=False),
            sa.Column("device_ip", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("result_json", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(
                ["job_id"], ["automation_jobs.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_automation_targets_job_status",
            "automation_job_targets",
            ["job_id", "status"],
        )

    if "automation_job_steps" not in tables:
        op.create_table(
            "automation_job_steps",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("target_id", sa.Integer(), nullable=False),
            sa.Column("step_type", sa.String(length=40), nullable=False),
            sa.Column("step_name", sa.String(length=255), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("command", sa.Text(), nullable=True),
            sa.Column("exit_code", sa.Integer(), nullable=True),
            sa.Column("output", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["target_id"], ["automation_job_targets.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    if "automation_schedules" not in tables:
        op.create_table(
            "automation_schedules",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("job_type", sa.String(length=24), nullable=False),
            sa.Column("target_ids", sa.JSON(), nullable=False),
            sa.Column("config_json", sa.JSON(), nullable=False),
            sa.Column("schedule_type", sa.String(length=20), nullable=False),
            sa.Column("scheduled_at", sa.DateTime(), nullable=True),
            sa.Column("cron_expression", sa.String(length=100), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("last_run_at", sa.DateTime(), nullable=True),
            sa.Column("next_run_at", sa.DateTime(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_by_name", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_automation_schedules_next_run",
            "automation_schedules",
            ["status", "next_run_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "automation_schedules" in tables:
        op.drop_index(
            "ix_automation_schedules_next_run", table_name="automation_schedules"
        )
        op.drop_table("automation_schedules")
    if "automation_job_steps" in tables:
        op.drop_table("automation_job_steps")
    if "automation_job_targets" in tables:
        op.drop_index(
            "ix_automation_targets_job_status", table_name="automation_job_targets"
        )
        op.drop_table("automation_job_targets")
    if "automation_jobs" in tables:
        op.drop_index("ix_automation_jobs_type_status", table_name="automation_jobs")
        op.drop_index("ix_automation_jobs_created_at", table_name="automation_jobs")
        op.drop_table("automation_jobs")
