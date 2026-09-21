"""``alert_events`` 补 ``owner_target_id`` 宿主身份冗余列(含回填与索引)。

ACL 过滤(_allowed_query)旧版对 ``remediation_json`` 做
``cast(Text).like('%"target_id": -N%')`` 匹配虚拟机容器事件:cast 表达式无法走
索引,事件表随 90 天保留增长后,受限角色的每次告警列表/概览查询都是全表扫;
且 LIKE 模板耦合 MySQL 的 JSON 渲染格式(``": "`` 带空格),换库或改序列化
即静默失效。

新增整型冗余列,取值与现有身份字段同源:
* 设备事件(含设备容器/主机状态) = device_id(正数);
* 虚拟机指标与主机状态事件 = resource_id 里的合成负数 target_id;
* 虚拟机容器事件 = 处置记录 remediation_json.target_id(负数);
* 业务事件无宿主,保持 NULL。

回填用同一口径的 COALESCE;评价新建事件的路径随代码一并写入。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043_alert_events_owner_target"
down_revision: Union[str, None] = "0042_alert_events_metric_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "alert_events"
COLUMN = "owner_target_id"
INDEX = "ix_alert_events_owner_target_id"


def _columns(bind) -> set[str]:
    if TABLE not in sa.inspect(bind).get_table_names():
        return set()
    return {col["name"] for col in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if COLUMN in _columns(bind):
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.BigInteger(), nullable=True))
    # 回填口径与 _allowed_query 旧 LIKE 完全一致:设备行(device_id) >
    # resource_id 负数编码(虚拟机指标/主机状态) > 处置记录 target_id(虚拟机容器)。
    # 业务事件三处都取不到 → 保持 NULL(旧 LIKE 也匹配不到,可见性不变)。
    op.execute(
        f"""
        UPDATE {TABLE}
        SET {COLUMN} = COALESCE(
            device_id,
            CASE WHEN resource_id REGEXP '^-[0-9]+$'
                 THEN CAST(resource_id AS SIGNED) END,
            CASE WHEN JSON_TYPE(JSON_EXTRACT(remediation_json, '$.target_id')) = 'INTEGER'
                 THEN CAST(JSON_EXTRACT(remediation_json, '$.target_id') AS SIGNED) END
        )
        WHERE {COLUMN} IS NULL
        """
    )
    op.create_index(INDEX, TABLE, [COLUMN])


def downgrade() -> None:
    bind = op.get_bind()
    if COLUMN not in _columns(bind):
        return
    indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes(TABLE)}
    if INDEX in indexes:
        op.drop_index(INDEX, TABLE)
    op.drop_column(TABLE, COLUMN)
