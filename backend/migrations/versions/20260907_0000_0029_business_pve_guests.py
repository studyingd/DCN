"""Link PVE guests to business monitoring.

Business monitoring could only associate ``devices`` rows, so PVE virtual
machines — which live in ``pve_guest_bindings`` and are addressed by
``connection_id + guest_type + vmid`` — were not selectable.  This adds a
dedicated association table keyed by that stable identity plus a display-name
snapshot, so a business keeps showing which VM it depends on even while the
PVE platform is unreachable.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_business_pve_guests"
down_revision: Union[str, None] = "0028_alert_schema_alignment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "business_pve_guests"
INDEX = "ix_business_pve_guests_connection"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE in inspector.get_table_names():
        return

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("guest_type", sa.String(length=8), nullable=False),
        sa.Column("vmid", sa.Integer(), nullable=False),
        sa.Column("guest_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["pve_connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_id",
            "connection_id",
            "guest_type",
            "vmid",
            name="uq_biz_pve_guest",
        ),
    )
    # 连接删除时的级联清理按 connection_id 查找，单独建索引避免全表扫描。
    op.create_index(INDEX, TABLE, ["connection_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return
    if INDEX in {index["name"] for index in inspector.get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
