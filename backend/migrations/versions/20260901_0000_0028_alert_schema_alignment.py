"""Align alert event schema with the runtime model.

Earlier alert migrations added generic resource identity but left the legacy
device index/foreign key in place and allowed ``resource_type`` to remain NULL.
This reconciliation is safe for both fresh and upgraded MySQL installations.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_alert_schema_alignment"
down_revision: Union[str, None] = "0027_pve_guest_containers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # All persisted events use a generic resource identity.  Normalize legacy
    # rows before enforcing the non-null contract used by AlertEvent.
    columns = {
        column["name"]: column for column in inspector.get_columns("alert_events")
    }
    if "resource_type" in columns:
        op.execute(
            "UPDATE alert_events SET resource_type='device' WHERE resource_type IS NULL"
        )
        op.alter_column(
            "alert_events",
            "resource_type",
            existing_type=sa.String(length=32),
            nullable=False,
        )

    # Replace the original CASCADE relationship with SET NULL so deleting a
    # device preserves the alert event history.
    device_foreign_key_found = False
    for foreign_key in inspector.get_foreign_keys("alert_events"):
        if foreign_key.get("constrained_columns") == ["device_id"] and foreign_key.get(
            "name"
        ):
            device_foreign_key_found = True
            op.drop_constraint(foreign_key["name"], "alert_events", type_="foreignkey")
    if device_foreign_key_found:
        op.create_foreign_key(
            "fk_alert_events_device_id_devices",
            "alert_events",
            "devices",
            ["device_id"],
            ["id"],
            ondelete="SET NULL",
        )

    indexes = {index["name"] for index in inspector.get_indexes("alert_events")}
    # Create the replacement first: the legacy composite index may also be the
    # leftmost supporting index for the rule_id foreign key on MySQL.
    if "ix_alert_events_rule_resource_status" not in indexes:
        op.create_index(
            "ix_alert_events_rule_resource_status",
            "alert_events",
            ["rule_id", "resource_type", "resource_id", "status"],
        )
    if "ix_alert_events_rule_device_status" in indexes:
        op.drop_index("ix_alert_events_rule_device_status", table_name="alert_events")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for foreign_key in inspector.get_foreign_keys("alert_events"):
        if foreign_key.get("constrained_columns") == ["device_id"] and foreign_key.get(
            "name"
        ):
            op.drop_constraint(foreign_key["name"], "alert_events", type_="foreignkey")

    indexes = {index["name"] for index in inspector.get_indexes("alert_events")}
    if "ix_alert_events_rule_device_status" not in indexes:
        op.create_index(
            "ix_alert_events_rule_device_status",
            "alert_events",
            ["rule_id", "device_id", "status"],
        )
    if "ix_alert_events_rule_resource_status" in indexes:
        op.drop_index("ix_alert_events_rule_resource_status", table_name="alert_events")
    op.create_foreign_key(
        "fk_alert_events_device_id_devices_cascade",
        "alert_events",
        "devices",
        ["device_id"],
        ["id"],
        ondelete="CASCADE",
    )
    if "resource_type" in {
        column["name"] for column in inspector.get_columns("alert_events")
    }:
        op.alter_column(
            "alert_events",
            "resource_type",
            existing_type=sa.String(length=32),
            nullable=True,
        )
