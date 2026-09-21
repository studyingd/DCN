"""``maintenance_windows`` 静默窗口表。

窗口内对象的告警通知在出口处跳过(事件照常流转),窗口结束推汇总卡。
幂等:表已存在时跳过。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045_maintenance_windows"
down_revision: Union[str, None] = "0044_alert_events_snooze"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "maintenance_windows" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "maintenance_windows",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("target_ids", sa.JSON(), nullable=True),
        sa.Column("start_at", sa.DateTime(), nullable=False),
        sa.Column("end_at", sa.DateTime(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("muted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_state", sa.String(16), nullable=False, server_default=""),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_maintenance_windows_end_summary",
        "maintenance_windows",
        ["end_at", "summary_state"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "maintenance_windows" not in sa.inspect(bind).get_table_names():
        return
    op.drop_table("maintenance_windows")
