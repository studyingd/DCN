"""Drop ``inspection_records.vendor`` — 网络设备巡检子系统已整体下线。

交换机/路由器/防火墙不再纳管，``target_type`` 只剩 linux/windows 两种，记录厂商
(huawei/cisco/h3c/ruijie/zte)的 ``vendor`` 列随之失去意义。生产库该表为空、这一列
从未写入过数据，所以直接删列，不需要任何数据迁移。

与 0032 一样做幂等保护:表或列不存在时静默跳过，重复执行安全。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033_drop_inspection_vendor"
down_revision: Union[str, None] = "0032_alert_remediation_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "inspection_records"
COLUMN = "vendor"


def _existing_columns(bind) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    if COLUMN in _existing_columns(bind):
        op.drop_column(TABLE, COLUMN)


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    if COLUMN not in _existing_columns(bind):
        # 恢复成删除前的定义:可空的厂商名字符串。
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=30), nullable=True))
