"""``devices`` 表补充常用查询字段索引:rack_id / type / status。

dashboard、设备列表、监控状态聚合都按这三个字段过滤/分组;设备量上到几百台后,
全表扫描会拖慢首页与设备管理页。幂等:索引已存在时跳过。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039_devices_indexes"
down_revision: Union[str, None] = "0038_guest_binding_os_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "devices"
# MySQL 对 TEXT 列建索引需要前缀长度;type/status 都是 TEXT,取前 32 字符足够区分。
INDEXES = (
    ("ix_devices_rack_id", ["rack_id"], None),
    ("ix_devices_type", ["type"], 32),
    ("ix_devices_status", ["status"], 32),
)


def _existing_indexes(bind) -> set[str]:
    if TABLE not in sa.inspect(bind).get_table_names():
        return set()
    return {ix["name"] for ix in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_indexes(bind)
    for name, columns, prefix in INDEXES:
        if name in existing:
            continue
        if prefix:
            # 前缀索引:op.create_index 不直接支持,用裸 SQL
            col_list = ", ".join(f"`{c}`({prefix})" for c in columns)
            op.execute(f"CREATE INDEX `{name}` ON `{TABLE}` ({col_list})")
        else:
            op.create_index(name, TABLE, columns)


def downgrade() -> None:
    bind = op.get_bind()
    existing = _existing_indexes(bind)
    for name, _columns, _prefix in INDEXES:
        if name in existing:
            op.drop_index(name, TABLE)
