"""删除 ``roles.pve_scope``：虚拟机授权并入「设备范围」。

0034 引入了与 ``device_scope`` 并列的 ``pve_scope``，角色表单因此出现两个范围
单选，运维配置时要分别回答"设备管哪些""虚机管哪些"。产品口径改为只有一个
「设备范围」：选 ``selected`` 时同一个选择器里既勾设备也勾虚拟机，两张 ACL 表
（``role_device_access`` / ``role_pve_guest_access``）都只在该范围下生效。

列本身刚由 0034 加上、生产库尚未产生有意义的取值，直接删除即可。
``role_pve_guest_access`` 表保留。幂等：列不存在时跳过。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035_drop_role_pve_scope"
down_revision: Union[str, None] = "0034_role_pve_scope_and_perms"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "roles"
COLUMN = "pve_scope"


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    if COLUMN in _columns(bind):
        op.drop_column(TABLE, COLUMN)


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    if COLUMN not in _columns(bind):
        op.add_column(
            TABLE,
            sa.Column(
                COLUMN, sa.String(length=16), nullable=False, server_default="all"
            ),
        )
