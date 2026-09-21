"""Persist operational access settings for PVE guests."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_pve_guest_bindings"
down_revision: Union[str, None] = "0018_monitoring_reliability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "pve_guest_bindings" in inspector.get_table_names():
        cols = {
            column["name"] for column in inspector.get_columns("pve_guest_bindings")
        }
        if "ssh_host_key" not in cols:
            op.add_column(
                "pve_guest_bindings",
                sa.Column("ssh_host_key", sa.Text(), nullable=True),
            )
        return
    op.create_table(
        "pve_guest_bindings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("guest_type", sa.String(length=8), nullable=False),
        sa.Column("vmid", sa.Integer(), nullable=False),
        sa.Column("ip_address", sa.String(length=255), nullable=False),
        sa.Column(
            "os_system", sa.String(length=16), nullable=False, server_default="linux"
        ),
        sa.Column("ssh_port", sa.Integer(), nullable=False, server_default="22"),
        sa.Column("winrm_port", sa.Integer(), nullable=False, server_default="5985"),
        sa.Column("ssh_host_key", sa.Text(), nullable=True),
        sa.Column("credential_id", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_test_status", sa.String(length=20), nullable=True),
        sa.Column("last_test_error", sa.Text(), nullable=True),
        sa.Column("last_test_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["pve_connections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"], ["credentials.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "connection_id", "guest_type", "vmid", name="uq_pve_guest_binding"
        ),
    )
    op.create_index(
        "ix_pve_guest_bindings_credential", "pve_guest_bindings", ["credential_id"]
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "pve_guest_bindings" in inspector.get_table_names():
        op.drop_index(
            "ix_pve_guest_bindings_credential", table_name="pve_guest_bindings"
        )
        op.drop_table("pve_guest_bindings")
