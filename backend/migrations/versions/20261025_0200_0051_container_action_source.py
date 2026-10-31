"""container_actions.source 操作来源标记

Revision ID: 0051_container_src
Revises: 0050_agent_actions
Create Date: 2026-10-25

容器操作审计此前 user_id 为 NULL 时无法区分「告警自愈」与其它非用户来源。
加 source 列:user(界面手动)/auto_remediation(告警自愈)/agent(Agent 动作)。
存量行按 user 语义回填 'user'。列已就绪,历史列表 UI 未铺(现无该页面)。
"""

import sqlalchemy as sa
from alembic import op

revision = "0051_container_src"
down_revision = "0050_agent_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "container_actions",
        sa.Column("source", sa.String(16), nullable=False, server_default="user"),
    )


def downgrade() -> None:
    op.drop_column("container_actions", "source")
