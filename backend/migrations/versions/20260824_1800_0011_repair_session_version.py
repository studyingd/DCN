"""Repair databases stamped at 0010 before session_version was added.

Some existing installations applied an earlier copy of revision 0010 and were
therefore marked current even though ``users.session_version`` was absent.
This reconciliation migration enforces the schema contract without disturbing
databases where the column already exists.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_repair_session_version"
down_revision: Union[str, None] = "0010_complete_runtime_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("users")}
    if "session_version" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "session_version",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    # This is a reconciliation migration for a column owned by revision 0010.
    # Leaving it in place keeps the schema consistent with the 0010 contract.
    pass
