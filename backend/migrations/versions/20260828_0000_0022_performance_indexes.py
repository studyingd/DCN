"""Add indexes used by monitoring and business aggregation queries."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_performance_indexes"
down_revision: Union[str, None] = "0021_remove_global_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add(table: str, name: str, columns: list[str], unique: bool = False) -> None:
    bind = op.get_bind()
    existing = {i["name"] for i in sa.inspect(bind).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    _add("service_interfaces", "ix_service_interfaces_enabled", ["enabled"])
    _add("interface_probes", "ix_interface_probes_checked_at", ["checked_at"])
    _add("alert_events", "ix_alert_events_status_last_seen", ["status", "last_seen_at"])


def downgrade() -> None:
    bind = op.get_bind()
    for table, name in (
        ("alert_events", "ix_alert_events_status_last_seen"),
        ("interface_probes", "ix_interface_probes_checked_at"),
        ("service_interfaces", "ix_service_interfaces_enabled"),
    ):
        if name in {i["name"] for i in sa.inspect(bind).get_indexes(table)}:
            op.drop_index(name, table_name=table)
