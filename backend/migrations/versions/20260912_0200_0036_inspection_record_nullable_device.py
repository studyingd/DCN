"""``inspection_records.device_id`` 允许为空：支持 PVE 虚拟机巡检留档。

PVE 虚拟机是合成负数 target_id，没有对应的真实 ``devices`` 行。要让它能跑
健康巡检并留档出 PDF，``inspection_records.device_id`` 必须允许 NULL——
虚拟机的设备名/IP/系统类型仍照常写入冗余列(device_name/device_ip/
target_type),PDF 报告读的正是这些冗余列,不 join devices 表,因此 NULL 的
device_id 不影响报告渲染。权限过滤处对 NULL 单独放行(虚拟机走 pve ACL)。

幂等:已是 nullable 时跳过。MySQL 需要同时放宽外键,用 MODIFY 保留外键但允许 NULL。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036_inspection_nullable_dev"
down_revision: Union[str, None] = "0035_drop_role_pve_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "inspection_records"
COLUMN = "device_id"


def _column_nullable(bind) -> bool | None:
    if TABLE not in sa.inspect(bind).get_table_names():
        return None
    for col in sa.inspect(bind).get_columns(TABLE):
        if col["name"] == COLUMN:
            return bool(col["nullable"])
    return None


def upgrade() -> None:
    bind = op.get_bind()
    nullable = _column_nullable(bind)
    if nullable is None or nullable:
        return
    # MySQL:放宽 device_id 为可空,保留外键约束到 devices.id。
    op.alter_column(TABLE, COLUMN, existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    nullable = _column_nullable(bind)
    if nullable is None or not nullable:
        return
    # 回退前把 NULL 行清掉,否则 NOT NULL 约束会失败。这些行本就是虚拟机巡检
    # 记录,回退即放弃这部分留档。
    op.execute(sa.text(f"DELETE FROM {TABLE} WHERE {COLUMN} IS NULL"))
    op.alter_column(TABLE, COLUMN, existing_type=sa.Integer(), nullable=False)
