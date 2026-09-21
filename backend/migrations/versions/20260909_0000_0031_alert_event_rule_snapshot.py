"""Keep alert event history when its rule is deleted.

``alert_events.rule_id`` was ``NOT NULL`` with ``ON DELETE CASCADE``, so removing
an alert rule silently destroyed every event it had ever produced - the audit
trail of what actually alerted, when, and what the AI attribution concluded.

Deleting a *rule definition* should not delete *operational history*: the rule
is configuration, the events are records.  This revision

  * adds ``alert_events.rule_name`` as a snapshot, so history stays readable
    after the rule row is gone (the rule may also have been renamed in between);
  * relaxes ``rule_id`` to nullable and switches the foreign key to
    ``ON DELETE SET NULL``, mirroring what 0028 already did for ``device_id``.

Rows are backfilled before the constraint change so existing history keeps its
rule name.  Safe to re-run: every step checks the current schema first.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031_alert_event_rule_snapshot"
down_revision: Union[str, None] = "0030_webhook_provider_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "alert_events"
COLUMN = "rule_name"
FK_NAME = "fk_alert_events_rule_id_alert_rules"


def _columns(bind) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(bind).get_columns(TABLE)}


def _drop_rule_foreign_key(bind) -> bool:
    dropped = False
    for foreign_key in sa.inspect(bind).get_foreign_keys(TABLE):
        if foreign_key.get("constrained_columns") == ["rule_id"] and foreign_key.get(
            "name"
        ):
            op.drop_constraint(foreign_key["name"], TABLE, type_="foreignkey")
            dropped = True
    return dropped


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    columns = _columns(bind)
    if COLUMN not in columns:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=255), nullable=True))
    # 先补历史数据的规则名，再放宽约束，避免旧记录变成"未知规则"。
    op.execute(
        sa.text(
            f"UPDATE {TABLE} e JOIN alert_rules r ON r.id = e.rule_id "
            f"SET e.{COLUMN} = r.name WHERE e.{COLUMN} IS NULL"
        )
    )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}
    # MySQL 需要 rule_id 上有索引才能建外键;原来的复合索引可能正是它的支撑索引，
    # 所以先补一个独立索引再动约束(与 0028 的处理一致)。
    if "ix_alert_events_rule_resource_status" not in indexes:
        op.create_index(
            "ix_alert_events_rule_resource_status",
            TABLE,
            ["rule_id", "resource_type", "resource_id", "status"],
        )
    _drop_rule_foreign_key(bind)
    if columns.get("rule_id", {}).get("nullable") is False:
        op.alter_column(TABLE, "rule_id", existing_type=sa.Integer(), nullable=True)
    op.create_foreign_key(
        FK_NAME, TABLE, "alert_rules", ["rule_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    # 恢复 CASCADE 前先清掉失去规则的事件，否则 NOT NULL 约束无法恢复。
    op.execute(sa.text(f"DELETE FROM {TABLE} WHERE rule_id IS NULL"))
    _drop_rule_foreign_key(bind)
    if _columns(bind).get("rule_id", {}).get("nullable"):
        op.alter_column(TABLE, "rule_id", existing_type=sa.Integer(), nullable=False)
    op.create_foreign_key(
        "fk_alert_events_rule_id_alert_rules_cascade",
        TABLE,
        "alert_rules",
        ["rule_id"],
        ["id"],
        ondelete="CASCADE",
    )
    if COLUMN in _columns(bind):
        op.drop_column(TABLE, COLUMN)
