"""Add the alert auto-remediation and AI attribution columns.

``alert_events`` grew six columns for the offline-guest auto-restart and the
Agent attribution features, but they were only ever added by an ad-hoc
``ALTER TABLE`` script that lived outside the Alembic chain (since removed), so
the chain itself never learned about them.  A database provisioned by ``alembic upgrade head`` alone
(exactly what ``docker/docker-entrypoint-backend.sh`` runs) therefore came up
without them, and the first alert insert died with
``Unknown column 'remediation_state' in 'field list'``.  Existing dev databases
only worked because the script had been run by hand at some point.

``create_all`` could not paper over this either: it creates missing *tables*,
never missing *columns* on a table that already exists - and ``alert_events``
is created by 0016.

This revision brings the chain in line with ``app/models/alert.py``.  Safe to
re-run: every column is checked first, so databases that already have them (by
whatever route) are left untouched.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032_alert_remediation_columns"
down_revision: Union[str, None] = "0031_alert_event_rule_snapshot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "alert_events"

# 列名 -> 列定义。与 app/models/alert.py 逐字对齐;NOT NULL 列必须带 server_default，
# 否则已有历史行无法回填。
COLUMNS: dict[str, sa.Column] = {
    # 自动处置(离线虚拟机/容器自动拉起): "" / running / verifying / succeeded / failed / skipped
    "remediation_state": sa.Column(
        sa.String(length=16), nullable=False, server_default=""
    ),
    # 面向用户的进度话术，会拼进 message 一起推送
    "remediation_detail": sa.Column(sa.Text(), nullable=True),
    # 处置上下文: base_message/action/target/attempts/errors/时间戳
    "remediation_json": sa.Column(sa.JSON(), nullable=True),
    # Agent 归因: "" / running / completed / failed / skipped
    "analysis_state": sa.Column(
        sa.String(length=16), nullable=False, server_default=""
    ),
    "analysis_text": sa.Column(sa.Text(), nullable=True),
    # 关联 agent_runs.id，但刻意不建外键:告警事件需长期留存，诊断记录可独立清理。
    "agent_run_id": sa.Column(sa.Integer(), nullable=True),
}


def _existing_columns(bind) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    existing = _existing_columns(bind)
    for name, column in COLUMNS.items():
        if name in existing:
            continue
        op.add_column(
            TABLE,
            sa.Column(
                name,
                column.type,
                nullable=column.nullable,
                server_default=column.server_default,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    existing = _existing_columns(bind)
    for name in reversed(COLUMNS):
        if name in existing:
            op.drop_column(TABLE, name)
