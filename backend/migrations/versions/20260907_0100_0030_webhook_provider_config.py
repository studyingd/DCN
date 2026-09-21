"""Provider-specific webhook config.

Alert delivery only had ``url`` + ``headers`` + a single ``secret``, which is
enough for inbound-style endpoints (Feishu Bitable workflow, group bots) but not
for a Feishu custom app that talks to ``im/v1/messages``: that needs an App ID
plus an explicit receiver list (open_id / email / chat_id) and a message style.
This adds a ``webhooks.config`` JSON column for those provider-owned settings,
while the App Secret keeps using the existing encrypted ``secret_enc`` column.

MySQL cannot give a JSON column a DEFAULT, so the column is added as nullable,
backfilled, then tightened to NOT NULL to match ``Base.metadata.create_all``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030_webhook_provider_config"
down_revision: Union[str, None] = "0029_business_pve_guests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "webhooks"
COLUMN = "config"


def _columns(bind) -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    columns = _columns(bind)
    if COLUMN not in columns:
        op.add_column(TABLE, sa.Column(COLUMN, sa.JSON(), nullable=True))
    op.execute(
        sa.text(f"UPDATE {TABLE} SET {COLUMN} = JSON_OBJECT() WHERE {COLUMN} IS NULL")
    )
    if COLUMN not in columns or columns[COLUMN].get("nullable"):
        op.alter_column(TABLE, COLUMN, existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    if COLUMN in _columns(bind):
        op.drop_column(TABLE, COLUMN)
