"""Consolidate state alert metrics."""

from typing import Sequence, Union

from alembic import op

revision: str = "0024_simplify_alert_metrics"
down_revision: Union[str, None] = "0023_alert_resources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE alert_rules SET metric='host_status', threshold=0, operator='gt' WHERE metric='host_online'"
    )
    op.execute(
        "UPDATE alert_rules SET metric='container_status', threshold=0, operator='gt' WHERE metric IN ('container_running','container_restarting')"
    )
    op.execute(
        "UPDATE alert_rules SET metric='business_status', threshold=0, operator='gt' WHERE metric IN ('business_health','business_server_online','business_interface_up')"
    )


def downgrade() -> None:
    op.execute("UPDATE alert_rules SET metric='host_online' WHERE metric='host_status'")
    op.execute(
        "UPDATE alert_rules SET metric='container_running' WHERE metric='container_status'"
    )
    op.execute(
        "UPDATE alert_rules SET metric='business_health' WHERE metric='business_status'"
    )
