"""Drop legacy scheduled_tasks table (merged into automation_schedules)

Revision ID: 0046_drop_legacy_sched
Revises: 0045_maintenance_windows
Create Date: 2026-09-17

旧 ScheduledTask 定时系统已删除(前端入口/后端路由/调度执行全链路),
定时计划统一走 automation_schedules。删表前把仍活跃的旧计划折算成
AutomationSchedule(job_type='script',config 带 command/timeout);
已结束(completed/failed/disabled)的行不再迁移——执行历史本就不落旧表。
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0046_drop_legacy_sched"
down_revision = "0045_maintenance_windows"
branch_labels = None
depends_on = None

_ACTIVE_STATUSES = ("pending", "active", "paused")


def upgrade() -> None:
    conn = op.get_bind()
    # 实际库表可能缺 created_by_name(模型与 baseline 的历史偏差):
    # 先探测列集再迁移,避免对不存在的列发查询。
    cols = {
        r[0]
        for r in conn.execute(sa.text("SHOW COLUMNS FROM scheduled_tasks")).fetchall()
    }
    has_created_by_name = "created_by_name" in cols
    select_cols = (
        "name, command, device_ids, schedule_type, scheduled_at, "
        "cron_expression, timeout, status, created_by, "
        + ("created_by_name, " if has_created_by_name else "")
        + "created_at"
    )
    rows = conn.execute(
        sa.text(
            f"SELECT {select_cols} FROM scheduled_tasks WHERE status IN :statuses"
        ).bindparams(sa.bindparam("statuses", expanding=True)),
        {"statuses": list(_ACTIVE_STATUSES)},
    ).fetchall()
    for row in rows:
        (
            name,
            command,
            device_ids,
            stype,
            sat,
            cron,
            timeout,
            _status,
            uid,
            *rest,
        ) = row
        created_at = rest[-1]
        uname = rest[0] if has_created_by_name and len(rest) > 1 else None
        conn.execute(
            sa.text(
                "INSERT INTO automation_schedules "
                "(name, job_type, target_ids, config_json, schedule_type, "
                "scheduled_at, cron_expression, status, created_by, "
                "created_by_name, created_at) "
                "VALUES (:name, 'script', :target_ids, :config, :stype, :sat, "
                ":cron, 'active', :uid, :uname, :created_at)"
            ),
            {
                "name": name,
                # device_ids 在旧表是 JSON 数组,统一表同构直接透传
                "target_ids": json.dumps(device_ids if device_ids is not None else []),
                "config": json.dumps(
                    {"command": command, "timeout": int(timeout or 30)}
                ),
                "stype": stype,
                "sat": sat,
                "cron": cron,
                "uid": uid,
                "uname": uname,
                "created_at": created_at,
            },
        )
    op.drop_table("scheduled_tasks")


def downgrade() -> None:
    # 结构层面重建空表;已折算进统一计划的数据不回迁(执行历史在
    # automation_jobs,不属于旧表结构)。
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("device_ids", sa.JSON(), nullable=False),
        sa.Column("schedule_type", sa.String(20), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("cron_expression", sa.String(100), nullable=True),
        sa.Column("timeout", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_by_name", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
