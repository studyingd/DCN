"""``pve_guest_bindings.os_name`` 新增列：持久化虚拟机精确操作系统名。

背景：虚拟机的精确 OS 名（如 'Debian GNU/Linux 12'）权威来源是 QGA 的
``osinfo.pretty-name``；但 QGA 未启用/不可用时（如 Test PVE 的 Require），
界面只能显示粗粒度的 "linux"/"windows"，即使运维接入里已配好凭据、
SSH 探测本可读出 ``/etc/os-release``。

本迁移加冗余列 ``os_name``：``detect-os`` 端点和自动化巡检
（``_pve_runtime_device``）拿到精确名时回写这里，binding 视图透出给前端
「系统」列显示。QGA 可用时实时精确名仍优先于该持久值。

幂等：列已存在时跳过。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038_guest_binding_os_name"
down_revision: Union[str, None] = "0037_inspection_os_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "pve_guest_bindings"
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
