"""``alert_events`` 补充 (metric, last_seen_at) 复合索引。

归因通知看门狗 ``sweep_expired_notification_holds`` 每个评估周期(30~60s)按
``metric IN (ANALYSIS_METRICS) AND last_seen_at >= 窗口`` 扫描被扣住的通知;
现有索引 (status, last_seen_at) 的首列是 status,服务不了这个查询,事件表随
90 天保留增长后每轮都是全表扫描。补 (metric, last_seen_at) 后走范围扫描。
幂等:索引已存在时跳过。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042_alert_events_metric_idx"
down_revision: Union[str, None] = "0041_webhook_alert_analysis"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "alert_events"
INDEXES = (("ix_alert_events_metric_last_seen", ["metric", "last_seen_at"]),)


def _existing_indexes(bind) -> set[str]:
    if TABLE not in sa.inspect(bind).get_table_names():
        return set()
    return {ix["name"] for ix in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_indexes(bind)
    for name, columns in INDEXES:
        if name in existing:
            continue
        op.create_index(name, TABLE, columns)


def downgrade() -> None:
    bind = op.get_bind()
    existing = _existing_indexes(bind)
    for name, _columns in INDEXES:
        if name in existing:
            op.drop_index(name, TABLE)
