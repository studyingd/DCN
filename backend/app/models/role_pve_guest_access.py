"""Per-role PVE guest ACL.

``role_device_access`` can only point at ``devices`` rows, so virtual machines
were outside every role's device scope: PVE access was an all-or-nothing
boolean (``pve:view`` / ``pve:manage``).  This mirrors ``business_pve_guests``
and keys a guest by its stable identity ``connection_id + guest_type + vmid``,
plus a display-name snapshot so the role editor keeps showing which VM was
granted even while the PVE platform is unreachable.
"""

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime


class RolePveGuestAccess(Base):
    __tablename__ = "role_pve_guest_access"
    __table_args__ = (
        UniqueConstraint(
            "role_id", "connection_id", "guest_type", "vmid", name="uq_role_pve_guest"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pve_connections.id", ondelete="CASCADE"), nullable=False
    )
    guest_type: Mapped[str] = mapped_column(String(8), nullable=False)
    vmid: Mapped[int] = mapped_column(Integer, nullable=False)
    guest_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    role: Mapped["Role"] = relationship(  # noqa: F821
        "Role", back_populates="pve_guest_access"
    )
