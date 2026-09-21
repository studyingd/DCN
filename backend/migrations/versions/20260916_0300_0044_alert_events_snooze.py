"""``alert_events`` 补「已知晓(snooze)」字段。

单运维场景的告警降噪:点一下「已知晓」后,该事件冷却到期不再重发
alert.created(评估/归因/自动处置照常),恢复时正常推 alert.resolved。
字段:snoozed(标志) / snoozed_at(时间) / snoozed_by_name(站内点击时
记录认领人;飞书卡片链接点击无登录态,留空)。幂等:列已存在时跳过。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0044_alert_events_snooze"
down_revision: Union[str, None] = "0043_alert_events_owner_target"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "alert_events"


def _columns(bind) -> set[str]:
    if TABLE not in sa.inspect(bind).get_table_names():
        return set()
    return {col["name"] for col in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind)
    if "snoozed" not in existing:
        op.add_column(
            TABLE,
            sa.Column(
                "snoozed", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        )
    if "snoozed_at" not in existing:
        op.add_column(TABLE, sa.Column("snoozed_at", sa.DateTime(), nullable=True))
    if "snoozed_by_name" not in existing:
        op.add_column(
            TABLE, sa.Column("snoozed_by_name", sa.String(128), nullable=True)
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind)
    for name in ("snoozed_by_name", "snoozed_at", "snoozed"):
        if name in existing:
            op.drop_column(TABLE, name)
