"""Drop user_groups table and users.group_id column.

Removes the user-group feature entirely: users are attributed to roles only.
Idempotent: inspects existing schema before each change.

Revision ID: 0006_drop_user_groups
Revises: 0005_grant_settings_manage
Create Date: 2026-08-11 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_drop_user_groups"
down_revision: Union[str, None] = "0005_grant_settings_manage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "users" in tables:
        cols = {c["name"] for c in insp.get_columns("users")}
        if "group_id" in cols:
            # MySQL-only migration: alter the table directly.
            for fk in insp.get_foreign_keys("users"):
                if "group_id" in (fk.get("constrained_columns") or []) and fk.get(
                    "name"
                ):
                    op.drop_constraint(fk["name"], "users", type_="foreignkey")
            op.drop_column("users", "group_id")

    if "user_groups" in tables:
        op.drop_table("user_groups")


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "user_groups" not in tables:
        op.create_table(
            "user_groups",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("description", sa.String(length=256), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )

    cols = {c["name"] for c in sa.inspect(bind).get_columns("users")}
    if "group_id" not in cols:
        op.add_column("users", sa.Column("group_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_users_group_id",
            "users",
            "user_groups",
            ["group_id"],
            ["id"],
            ondelete="SET NULL",
        )
