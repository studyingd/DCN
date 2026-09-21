"""Add PVE guest target metadata to automation targets."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_automation_pve_targets"
down_revision: Union[str, None] = "0013_webhooks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("automation_job_targets")}
    if "target_type" not in cols:
        op.add_column(
            "automation_job_targets",
            sa.Column(
                "target_type", sa.String(20), nullable=False, server_default="device"
            ),
        )
    if "pve_connection_id" not in cols:
        op.add_column(
            "automation_job_targets",
            sa.Column("pve_connection_id", sa.Integer(), nullable=True),
        )
    if "pve_guest_type" not in cols:
        op.add_column(
            "automation_job_targets",
            sa.Column("pve_guest_type", sa.String(8), nullable=True),
        )
    if "pve_vmid" not in cols:
        op.add_column(
            "automation_job_targets", sa.Column("pve_vmid", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    for name in ("pve_vmid", "pve_guest_type", "pve_connection_id", "target_type"):
        op.drop_column("automation_job_targets", name)
