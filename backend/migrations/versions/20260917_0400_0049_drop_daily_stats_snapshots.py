"""Drop dead daily stats snapshot table

Revision ID: 0049_drop_daily_stats
Revises: 0048_drop_3d_legacy
Create Date: 2026-09-17

daily_stats_snapshots 表由 monitor 循环每 30s upsert 一次,但全后端/前端
零读者——首页统计走 /dashboard/overview 直查 devices 表(2026-09-17 用户
拍板清理)。连带删除 /api/monitor/stats 端点(前端同样零调用)。

**不要再加回来**:需要「历史每日在线率」类功能时应按当时的产品方案重新
设计数据模型,而不是恢复这张无消费方的快照表。设备维护数(maintenance)
一直是写死的 0,dashboard 的 device_maintenance 来自 overview 直查,与此表
无关。
"""

import sqlalchemy as sa
from alembic import op

revision = "0049_drop_daily_stats"
down_revision = "0048_drop_3d_legacy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("daily_stats_snapshots")


def downgrade() -> None:
    op.create_table(
        "daily_stats_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("snapshot_date", sa.String(10), nullable=False, unique=True),
        sa.Column("device_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("device_online", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("device_offline", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "device_maintenance", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
