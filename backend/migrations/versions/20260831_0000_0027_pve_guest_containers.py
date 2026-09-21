"""Allow Docker snapshots and actions to belong to PVE guest bindings.

PVE guests are managed through ``pve_guest_bindings`` rather than ``devices``.
The existing container tables remain in place for ordinary servers; nullable
PVE binding columns make both target kinds share the same API and UI.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_pve_guest_containers"
down_revision: Union[str, None] = "0026_state_event_severity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _make_nullable(table: str, column: str) -> None:
    op.alter_column(table, column, existing_type=sa.Integer(), nullable=True)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "pve_guest_bindings" not in tables:
        return

    if "device_containers" in tables:
        columns = _columns("device_containers")
        if "device_id" in columns:
            # Existing installations have a non-nullable device_id.
            if any(
                column.get("nullable") is False
                for column in sa.inspect(bind).get_columns("device_containers")
                if column["name"] == "device_id"
            ):
                _make_nullable("device_containers", "device_id")
        if "pve_guest_binding_id" not in columns:
            op.add_column(
                "device_containers",
                sa.Column("pve_guest_binding_id", sa.Integer(), nullable=True),
            )
            op.create_foreign_key(
                "fk_device_containers_pve_guest_binding",
                "device_containers",
                "pve_guest_bindings",
                ["pve_guest_binding_id"],
                ["id"],
                ondelete="CASCADE",
            )
        if "ix_device_containers_pve_guest_name" not in _indexes("device_containers"):
            op.create_index(
                "ix_device_containers_pve_guest_name",
                "device_containers",
                ["pve_guest_binding_id", "name"],
                unique=True,
            )

    if "device_docker_status" in tables:
        columns = _columns("device_docker_status")
        if "device_id" in columns:
            if any(
                column.get("nullable") is False
                for column in sa.inspect(bind).get_columns("device_docker_status")
                if column["name"] == "device_id"
            ):
                _make_nullable("device_docker_status", "device_id")
        if "pve_guest_binding_id" not in columns:
            op.add_column(
                "device_docker_status",
                sa.Column("pve_guest_binding_id", sa.Integer(), nullable=True),
            )
            op.create_foreign_key(
                "fk_device_docker_status_pve_guest_binding",
                "device_docker_status",
                "pve_guest_bindings",
                ["pve_guest_binding_id"],
                ["id"],
                ondelete="CASCADE",
            )
            op.create_unique_constraint(
                "uq_device_docker_status_pve_guest_binding",
                "device_docker_status",
                ["pve_guest_binding_id"],
            )

    if "container_actions" in tables:
        columns = _columns("container_actions")
        if "pve_guest_binding_id" not in columns:
            op.add_column(
                "container_actions",
                sa.Column("pve_guest_binding_id", sa.Integer(), nullable=True),
            )
            op.create_foreign_key(
                "fk_container_actions_pve_guest_binding",
                "container_actions",
                "pve_guest_bindings",
                ["pve_guest_binding_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "container_actions" in tables and "pve_guest_binding_id" in _columns(
        "container_actions"
    ):
        op.drop_constraint(
            "fk_container_actions_pve_guest_binding",
            "container_actions",
            type_="foreignkey",
        )
        op.drop_column("container_actions", "pve_guest_binding_id")
    if "device_docker_status" in tables and "pve_guest_binding_id" in _columns(
        "device_docker_status"
    ):
        op.drop_constraint(
            "fk_device_docker_status_pve_guest_binding",
            "device_docker_status",
            type_="foreignkey",
        )
        op.drop_constraint(
            "uq_device_docker_status_pve_guest_binding",
            "device_docker_status",
            type_="unique",
        )
        op.drop_column("device_docker_status", "pve_guest_binding_id")
    if "device_containers" in tables and "pve_guest_binding_id" in _columns(
        "device_containers"
    ):
        op.drop_constraint(
            "fk_device_containers_pve_guest_binding",
            "device_containers",
            type_="foreignkey",
        )
        if "ix_device_containers_pve_guest_name" in _indexes("device_containers"):
            op.drop_index(
                "ix_device_containers_pve_guest_name", table_name="device_containers"
            )
        op.drop_column("device_containers", "pve_guest_binding_id")
