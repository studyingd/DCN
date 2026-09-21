"""Migrate secrets onto devices and remove the global credentials catalogue."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_remove_global_credentials"
down_revision: Union[str, None] = "0020_pve_guest_local_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "devices" not in tables:
        return
    cols = {c["name"] for c in inspector.get_columns("devices")}
    for name, typ in (
        ("remote_username", sa.Text()),
        ("remote_password_enc", sa.Text()),
        ("remote_ssh_key_enc", sa.Text()),
    ):
        if name not in cols:
            op.add_column("devices", sa.Column(name, typ, nullable=True))
    # Copy each device's existing global credential into device-owned fields.
    if "credentials" in tables:
        op.execute(
            sa.text("""
            UPDATE devices d JOIN credentials c ON c.id = d.credential_id
            SET d.remote_username = c.username,
                d.remote_password_enc = c.password_enc,
                d.remote_ssh_key_enc = c.ssh_key_enc
        """)
        )
    # Scheduled tasks now resolve credentials from their target devices.
    if "scheduled_tasks" in tables:
        for fk in sa.inspect(bind).get_foreign_keys("scheduled_tasks"):
            if fk.get("constrained_columns") == ["credential_id"]:
                op.drop_constraint(fk["name"], "scheduled_tasks", type_="foreignkey")
        op.drop_column("scheduled_tasks", "credential_id")
    # Remove legacy foreign keys/columns before dropping the catalogue.
    if "devices" in tables and "credential_id" in {
        c["name"] for c in sa.inspect(bind).get_columns("devices")
    }:
        for fk in sa.inspect(bind).get_foreign_keys("devices"):
            if fk.get("constrained_columns") == ["credential_id"]:
                op.drop_constraint(fk["name"], "devices", type_="foreignkey")
        op.drop_column("devices", "credential_id")
    if "pve_guest_bindings" in tables and "credential_id" in {
        c["name"] for c in sa.inspect(bind).get_columns("pve_guest_bindings")
    }:
        for fk in sa.inspect(bind).get_foreign_keys("pve_guest_bindings"):
            if fk.get("constrained_columns") == ["credential_id"]:
                op.drop_constraint(fk["name"], "pve_guest_bindings", type_="foreignkey")
        op.drop_column("pve_guest_bindings", "credential_id")
    if "credentials" in tables:
        op.drop_table("credentials")


def downgrade() -> None:
    # Irreversible by design: plaintext-equivalent encrypted values cannot be
    # reconstructed into a shared global catalogue without inventing IDs.
    raise RuntimeError("global credentials removal is irreversible")
