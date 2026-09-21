"""Add alert targets and generic resource identity."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_alert_resources"
down_revision: Union[str, None] = "0022_performance_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("alert_rules")}
    for name in ("target_container_ids", "target_business_ids"):
        if name not in cols:
            op.add_column("alert_rules", sa.Column(name, sa.JSON(), nullable=True))
    cols = {c["name"] for c in insp.get_columns("alert_events")}
    for name, typ in (
        ("resource_type", sa.String(32)),
        ("resource_id", sa.String(128)),
        ("resource_name", sa.String(255)),
    ):
        if name not in cols:
            op.add_column("alert_events", sa.Column(name, typ, nullable=True))
    if "device_id" in cols:
        op.alter_column(
            "alert_events", "device_id", existing_type=sa.Integer(), nullable=True
        )
    op.execute(
        "UPDATE alert_events SET resource_type='device' WHERE resource_type IS NULL"
    )


def downgrade() -> None:
    for name in ("resource_name", "resource_id", "resource_type"):
        op.drop_column("alert_events", name)
    for name in ("target_business_ids", "target_container_ids"):
        op.drop_column("alert_rules", name)
