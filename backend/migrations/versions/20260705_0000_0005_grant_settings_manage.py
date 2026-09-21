"""Preserve settings access for roles that previously managed the audit center.

``settings:manage`` is retained for current system configuration (for example
Agent and PVE settings).  Older installations used ``audit:manage`` as the
effective administrator grant, so carry that grant forward once.  This is a
data-only, idempotent migration; no removed audit or terminal features are
reintroduced.

Revision ID: 0005_grant_settings_manage
Revises: 0004_drop_audit_and_recordings
Create Date: 2026-07-05 00:00:00.000002

"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_grant_settings_manage"
down_revision: Union[str, None] = "0004_drop_audit_and_recordings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _role_rows(bind):
    return bind.execute(sa.text("SELECT id, permissions FROM roles")).fetchall()


def _parse(perms_raw) -> list[str]:
    try:
        perms = json.loads(perms_raw) if perms_raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    return perms if isinstance(perms, list) else []


def upgrade() -> None:
    bind = op.get_bind()
    for role_id, perms_raw in _role_rows(bind):
        perms = _parse(perms_raw)
        # The alias resolution that used to make this implicit is gone; restore
        # the prior effective access explicitly.
        if "audit:manage" in perms and "settings:manage" not in perms:
            perms.append("settings:manage")
            bind.execute(
                sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
                {"p": json.dumps(perms), "id": role_id},
            )

    # Purge inert setting rows left over from the removed audit/recording
    # subsystem — nothing reads these keys anymore. ``key`` is a MySQL reserved
    # word, hence the backticks.
    bind.execute(
        sa.text(
            "DELETE FROM settings WHERE `key` IN "
            "('audit_enabled', 'recording_enabled', "
            "'audit_retention_days', 'recording_retention_days')"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    for role_id, perms_raw in _role_rows(bind):
        perms = _parse(perms_raw)
        if "settings:manage" in perms:
            perms = [p for p in perms if p != "settings:manage"]
            bind.execute(
                sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
                {"p": json.dumps(perms), "id": role_id},
            )
