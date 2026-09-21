from datetime import datetime, timezone

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc)
    )

    racks: Mapped[list["Rack"]] = relationship(  # noqa: F821
        "Rack",
        back_populates="room",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
