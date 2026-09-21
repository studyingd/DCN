"""Webhook 订阅补 ``alert.analysis`` 事件。

告警归因改为「限时等」后:宽限内归因未终态就先发干净告警卡(不再带
「AI 归因分析进行中…」话术)，结论产出后由独立事件 ``alert.analysis``
补发第二张卡片。订阅了 ``alert.created`` 的钩子显然想要告警卡，补发
的结论卡理应到达同一批接收人;这里给**启用中**的钩子批量补订阅，
停用中的钩子不动，由用户在页面上自行勾选。

回滚仅从订阅列表里去掉该事件，不碰其它配置。
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041_webhook_alert_analysis"
down_revision: Union[str, None] = "0040_container_action_keep_vol"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EVENT = "alert.analysis"
SOURCE = "alert.created"


def _load(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, str):
        return json.loads(raw)
    return list(raw)


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, events FROM webhooks WHERE enabled = 1")
    ).fetchall()
    for row in rows:
        events = _load(row.events)
        if SOURCE in events and EVENT not in events:
            events.append(EVENT)
            conn.execute(
                sa.text("UPDATE webhooks SET events = :events WHERE id = :id"),
                {"events": json.dumps(events, ensure_ascii=False), "id": row.id},
            )


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, events FROM webhooks")).fetchall()
    for row in rows:
        events = _load(row.events)
        if EVENT in events:
            events.remove(EVENT)
            conn.execute(
                sa.text("UPDATE webhooks SET events = :events WHERE id = :id"),
                {"events": json.dumps(events, ensure_ascii=False), "id": row.id},
            )
