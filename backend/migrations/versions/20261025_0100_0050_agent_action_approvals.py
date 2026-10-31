"""Agent action approvals + agent_runs.mode

Revision ID: 0050_agent_actions
Revises: 0049_drop_daily_stats
Create Date: 2026-10-25

分级动作系统(2026-10-25 设计定稿):L0 白名单读自动;L1「恢复方向」写
(start 已停容器/服务)标准模式下免审自动执行,与告警自愈同集;L2「中断代价」
写(restart/stop 运行中对象)需人工审批——command 在提议时渲染定稿入库,
审批人批准的就是执行器执行的这份不可变字符串。L3(删文件/改配置/任意写)
永不开放,不进注册表。

agent_runs.mode 记录本次诊断的动作模式(readonly/standard/strict):
readonly=只有读工具;standard=L1 自动+L2 审批;strict=写动作全审批。
"""

import sqlalchemy as sa
from alembic import op

revision = "0050_agent_actions"
down_revision = "0049_drop_daily_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_action_approvals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action_key", sa.String(64), nullable=False),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.String(4), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column(
            "decided_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_agent_approvals_run", "agent_action_approvals", ["run_id"])
    op.create_index("ix_agent_approvals_status", "agent_action_approvals", ["status"])
    op.add_column(
        "agent_runs",
        sa.Column("mode", sa.String(16), nullable=False, server_default="readonly"),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "mode")
    op.drop_index("ix_agent_approvals_status", table_name="agent_action_approvals")
    op.drop_index("ix_agent_approvals_run", table_name="agent_action_approvals")
    op.drop_table("agent_action_approvals")
