"""Store PVE guest login credentials independently of retired global credentials."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_pve_guest_local_credentials"
down_revision: Union[str, None] = "0019_pve_guest_bindings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("pve_guest_bindings")}
    if "username" not in columns:
        op.add_column(
            "pve_guest_bindings",
            sa.Column("username", sa.String(length=255), nullable=True),
        )
    if "password_enc" not in columns:
        op.add_column(
            "pve_guest_bindings", sa.Column("password_enc", sa.Text(), nullable=True)
        )
    if "ssh_key_enc" not in columns:
        op.add_column(
            "pve_guest_bindings", sa.Column("ssh_key_enc", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("pve_guest_bindings")}
    for name in ("ssh_key_enc", "password_enc", "username"):
        if name in columns:
            op.drop_column("pve_guest_bindings", name)
