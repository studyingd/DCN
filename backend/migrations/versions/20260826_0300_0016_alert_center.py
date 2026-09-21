"""Add alert rules and alert event history."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_alert_center"
down_revision: Union[str, None] = "0015_remove_device_connections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "alert_rules" not in tables:
        op.create_table(
            "alert_rules",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("metric", sa.String(length=32), nullable=False),
            sa.Column("operator", sa.String(length=8), nullable=False),
            sa.Column("threshold", sa.Float(), nullable=False),
            sa.Column("severity", sa.String(length=16), nullable=False),
            sa.Column("target_device_ids", sa.JSON(), nullable=True),
            sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_alert_rules_enabled", "alert_rules", ["enabled"])
        op.create_index("ix_alert_rules_metric", "alert_rules", ["metric"])
    if "alert_events" not in tables:
        op.create_table(
            "alert_events",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("rule_id", sa.Integer(), nullable=False),
            sa.Column("device_id", sa.Integer(), nullable=False),
            sa.Column("metric", sa.String(length=32), nullable=False),
            sa.Column("value", sa.Float(), nullable=False),
            sa.Column("threshold", sa.Float(), nullable=False),
            sa.Column("severity", sa.String(length=16), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("first_triggered_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.Column("occurrence_count", sa.Integer(), nullable=False),
            sa.Column("notification_status", sa.String(length=16), nullable=False),
            sa.Column("notification_results", sa.JSON(), nullable=True),
            sa.Column("last_notified_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["rule_id"], ["alert_rules.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_alert_events_status", "alert_events", ["status"])
        op.create_index(
            "ix_alert_events_rule_device_status",
            "alert_events",
            ["rule_id", "device_id", "status"],
        )
        op.create_index(
            "ix_alert_events_created", "alert_events", ["first_triggered_at"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "alert_events" in tables:
        op.drop_index("ix_alert_events_created", table_name="alert_events")
        op.drop_index("ix_alert_events_rule_device_status", table_name="alert_events")
        op.drop_index("ix_alert_events_status", table_name="alert_events")
        op.drop_table("alert_events")
    if "alert_rules" in tables:
        op.drop_index("ix_alert_rules_metric", table_name="alert_rules")
        op.drop_index("ix_alert_rules_enabled", table_name="alert_rules")
        op.drop_table("alert_rules")
