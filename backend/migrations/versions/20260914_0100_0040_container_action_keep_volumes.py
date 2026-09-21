"""``container_actions`` 审计表补 ``keep_volumes`` 列。

容器删除动作支持「是否保留数据」:``docker rm -v`` 会连带删除匿名卷,
审计需要记录用户当时的取舍,便于事后追查"数据去哪了"。

回滚会删除该列;历史行为(其它动作)该列恒为 NULL。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0040_container_action_keep_vol"
down_revision: Union[str, None] = "0039_devices_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "container_actions"
COLUMN = "keep_volumes"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns(TABLE)}
    if COLUMN in columns:
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.Boolean(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns(TABLE)}
    if COLUMN not in columns:
        return
    op.drop_column(TABLE, COLUMN)
