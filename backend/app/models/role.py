from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    permissions: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    device_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="all")
    is_builtin: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # System administrator flag — implicit full permissions + device scope.
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    users: Mapped[list["User"]] = relationship("User", back_populates="role_ref")  # type: ignore[name-defined]
    device_access: Mapped[list["RoleDeviceAccess"]] = relationship(  # type: ignore[name-defined]
        "RoleDeviceAccess", back_populates="role", cascade="all, delete-orphan"
    )
