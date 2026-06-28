from sqlalchemy import Float, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Rack(Base):
    __tablename__ = "racks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)  # 'cabinet' or 'shelf'
    position_x: Mapped[float] = mapped_column(Float, default=0)
    position_y: Mapped[float] = mapped_column(Float, default=0)
    position_z: Mapped[float] = mapped_column(Float, default=0)
    rotation: Mapped[float] = mapped_column(Float, default=0)
    capacity_u: Mapped[int | None] = mapped_column(Integer, nullable=True)

    room: Mapped["Room"] = relationship(  # noqa: F821
        "Room", back_populates="racks"
    )
    devices: Mapped[list["Device"]] = relationship(  # noqa: F821
        "Device",
        back_populates="rack",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
