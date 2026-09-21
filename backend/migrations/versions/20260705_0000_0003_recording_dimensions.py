"""session_recordings: native width/height for RDP recordings

Adds ``width`` and ``height`` columns so the replay modal can be sized to the
recording's aspect ratio before the first frame arrives (eliminating the
open → info round-trip resize jump). Populated at RDP finalize time; legacy
rows are backfilled lazily on first replay.

Idempotent: inspects existing columns before adding, so it is safe to run after
the lifespan startup hook (``_ensure_recording_dim_columns``) has already added
them.

Revision ID: 0003_recording_dimensions
Revises: 0002_security_hardening
Create Date: 2026-07-05 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_recording_dimensions"
down_revision: Union[str, None] = "0002_security_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(bind) -> set[str]:
    inspector = sa.inspect(bind)
    return {c["name"] for c in inspector.get_columns("session_recordings")}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_columns(bind)
    if "width" not in existing:
        op.add_column(
            "session_recordings",
            sa.Column("width", sa.Integer(), nullable=True),
        )
    if "height" not in existing:
        op.add_column(
            "session_recordings",
            sa.Column("height", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("session_recordings", "height")
    op.drop_column("session_recordings", "width")
