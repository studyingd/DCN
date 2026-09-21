"""Ensure state alert rules always use critical severity."""

from typing import Sequence, Union

from alembic import op

revision: str = "0025_state_severity"
down_revision: Union[str, None] = "0024_simplify_alert_metrics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE alert_rules SET severity='critical', threshold=0, operator='gt' "
        "WHERE metric IN ('host_status', 'container_status', 'business_status')"
    )


def downgrade() -> None:
    pass
