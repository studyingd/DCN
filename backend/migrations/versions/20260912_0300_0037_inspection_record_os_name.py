"""``inspection_records.os_name`` 新增列：巡检时留档精确操作系统名。

背景：PDF 报告「系统」列对普通设备读 ``devices.os_system``、对 PVE 虚拟机读
``pve_guest_bindings.os_system``——而后者是 String(16) 粗粒度字段（只存
linux/windows），导致虚拟机一行只能显示回退的 "Windows"/"Linux"。

本迁移给 ``inspection_records`` 加冗余列 ``os_name``：巡检执行时把当时的精确
OS 名（设备台账值，或虚拟机经 QGA 探测到的 pretty-name）写进记录，报告直接读
冗余列，与 device_name/device_ip/target_type 同一套「留档自包含、不 join」的
口径。历史记录该列为 NULL，报告回退到旧的 device/binding 查询逻辑。

幂等：列已存在时跳过。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037_inspection_os_name"
down_revision: Union[str, None] = "0036_inspection_nullable_dev"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "inspection_records"
COLUMN = "os_name"


def _has_column(bind) -> bool | None:
    if TABLE not in sa.inspect(bind).get_table_names():
        return None
    return any(c["name"] == COLUMN for c in sa.inspect(bind).get_columns(TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    exists = _has_column(bind)
    if exists is None or exists:
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=255), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    exists = _has_column(bind)
    if not exists:
        return
    op.drop_column(TABLE, COLUMN)
