"""Drop 3D-scene legacy columns (rooms.floor_plan, racks position/rotation)

Revision ID: 0048_drop_3d_legacy
Revises: 0047_drop_device_inf
Create Date: 2026-09-17

早期 3D 机房场景的遗留字段:房间平面图(floor_plan JSON)、机柜坐标
(position_x/y/z)与朝向(rotation)。2D 视图上线后前端零消费,前端表单
也从未暴露(2026-09-17 用户拍板清理)。**不要再加回来**——重做 3D
场景时按当时的坐标方案另起字段,不要复用这批名字。
"""

import sqlalchemy as sa
from alembic import op

revision = "0048_drop_3d_legacy"
down_revision = "0047_drop_device_inf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.drop_column("floor_plan")
    with op.batch_alter_table("racks") as batch_op:
        batch_op.drop_column("position_x")
        batch_op.drop_column("position_y")
        batch_op.drop_column("position_z")
        batch_op.drop_column("rotation")


def downgrade() -> None:
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.add_column(sa.Column("floor_plan", sa.JSON(), nullable=True))
    with op.batch_alter_table("racks") as batch_op:
        batch_op.add_column(
            sa.Column("position_x", sa.Float(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("position_y", sa.Float(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("position_z", sa.Float(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("rotation", sa.Float(), nullable=False, server_default="0")
        )
