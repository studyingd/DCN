from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Connection(Base):
    __tablename__ = "connections"
    # A given (ordered) device pair can only be linked once. Callers normalize
    # the order (min/max ids) before insert so A→B and B→A collapse to one row.
    __table_args__ = (
        UniqueConstraint("device_a_id", "device_b_id", name="uq_connection_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_a_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    device_b_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    conn_type: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # ethernet/fiber/serial
    bandwidth: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    device_a: Mapped["Device"] = relationship(  # noqa: F821
        "Device", foreign_keys=[device_a_id], overlaps="connections_as_a"
    )
    device_b: Mapped["Device"] = relationship(  # noqa: F821
        "Device", foreign_keys=[device_b_id], overlaps="connections_as_b"
    )
