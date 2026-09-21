"""Remove the deprecated device connection/topology module."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_remove_device_connections"
down_revision: Union[str, None] = "0014_automation_pve_targets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "connections" in tables:
        op.drop_table("connections")

    if "daily_stats_snapshots" in tables:
        columns = {
            column["name"] for column in inspector.get_columns("daily_stats_snapshots")
        }
        if "connection_count" in columns:
            op.drop_column("daily_stats_snapshots", "connection_count")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # Restore the legacy schema only when explicitly rolling this migration
    # back.  The application has no model/router for this table, so a normal
    # upgrade remains fully removed while Alembic state stays reversible.
    if "connections" not in tables:
        op.create_table(
            "connections",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("device_a_id", sa.Integer(), nullable=False),
            sa.Column("device_b_id", sa.Integer(), nullable=False),
            sa.Column("conn_type", sa.Text(), nullable=True),
            sa.Column("bandwidth", sa.Text(), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(
                ["device_a_id"], ["devices.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["device_b_id"], ["devices.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "device_a_id", "device_b_id", name="uq_connection_pair"
            ),
        )

    if "daily_stats_snapshots" in tables:
        columns = {
            column["name"]
            for column in sa.inspect(bind).get_columns("daily_stats_snapshots")
        }
        if "connection_count" not in columns:
            op.add_column(
                "daily_stats_snapshots",
                sa.Column(
                    "connection_count",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("0"),
                ),
            )
