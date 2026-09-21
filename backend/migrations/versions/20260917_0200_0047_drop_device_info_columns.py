"""Drop unused device info columns (purpose/web_url/mac_address/owner)

Revision ID: 0047_drop_device_inf
Revises: 0046_drop_legacy_sched
Create Date: 2026-09-17

用途/负责人/管理地址/MAC 四个字段从未在前端表单暴露、无任何消费方
(2026-09-17 用户拍板不再需要,清理干净)。删除 devices 表列及全链路
schema/路由代码。**不要再加回来**——需要设备备注类信息时先过产品
口径,不要顺手恢复这四个字段名。
"""

import sqlalchemy as sa
from alembic import op

revision = "0047_drop_device_inf"
down_revision = "0046_drop_legacy_sched"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch_op:
        batch_op.drop_column("purpose")
        batch_op.drop_column("web_url")
        batch_op.drop_column("mac_address")
        batch_op.drop_column("owner")


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch_op:
        batch_op.add_column(sa.Column("purpose", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("web_url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("mac_address", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("owner", sa.Text(), nullable=True))
