"""security hardening: admin flag, ssh host-key pinning, unique connection pair

Adds the schema changes introduced by the security review:
  * roles.is_admin      — dedicated system-admin flag (replaces the free-text
                          ``role == 'admin'`` authorization bypass).
  * devices.ssh_host_key — pinned SSH host key for TOFU MITM prevention.
  * connections unique (device_a_id, device_b_id) — prevents duplicate topology
    edges. Exact-duplicate rows are removed first so the constraint can apply.
                          Callers normalize pair order (min/max) on insert so
                          A→B and B→A collapse to one row; mirror-image legacy
                          rows (if any) must be reconciled manually before upgrade.

Revision ID: 0002_security_hardening
Revises: be8ccc757984
Create Date: 2026-06-28 02:27:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_security_hardening"
down_revision: Union[str, None] = "be8ccc757984"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # System-admin flag on roles (default False; existing rows get False).
    op.add_column(
        "roles",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # Pinned SSH host key on devices (nullable; populated on first TOFU connect).
    op.add_column(
        "devices",
        sa.Column("ssh_host_key", sa.Text(), nullable=True),
    )

    # Remove exact-duplicate connection pairs so the unique constraint can apply.
    # MySQL forbids deleting from a table while the same table is referenced by
    # a subquery (error 1093).  A multi-table self-join delete is both native to
    # MySQL and keeps the lowest id for every exact device pair.
    op.execute(
        "DELETE duplicate FROM connections AS duplicate "
        "INNER JOIN connections AS original "
        "ON duplicate.device_a_id = original.device_a_id "
        "AND duplicate.device_b_id = original.device_b_id "
        "AND duplicate.id > original.id"
    )
    op.create_unique_constraint(
        "uq_connection_pair", "connections", ["device_a_id", "device_b_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_connection_pair", "connections", type_="unique")
    op.drop_column("devices", "ssh_host_key")
    op.drop_column("roles", "is_admin")
