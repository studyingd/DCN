"""Add sustained threshold duration to alert rules."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_alert_sustain_duration"
down_revision: Union[str, None] = "0016_alert_center"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("alert_rules")}
    if "sustain_seconds" not in columns:
        op.add_column(
            "alert_rules",
            sa.Column(
                "sustain_seconds",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("60"),
            ),
        )
        op.alter_column("alert_rules", "sustain_seconds", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("alert_rules")}
    if "sustain_seconds" in columns:
        op.drop_column("alert_rules", "sustain_seconds")
