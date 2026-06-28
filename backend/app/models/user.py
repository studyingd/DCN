from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="viewer")
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    role_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("roles.id", ondelete="SET NULL"), nullable=True
    )
    group_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user_groups.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    # ── Security fields ──
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    role_ref: Mapped["Role | None"] = relationship("Role", back_populates="users")  # type: ignore[name-defined]
    group_ref: Mapped["UserGroup | None"] = relationship(
        "UserGroup", back_populates="users"
    )  # type: ignore[name-defined]

    @property
    def is_locked(self) -> bool:
        """Check if the account is currently locked due to too many failed attempts."""
        if self.locked_until is None:
            return False
        return self.locked_until > datetime.now(timezone.utc)

    @property
    def must_change_password(self) -> bool:
        """True if user has never changed password (first login)."""
        return self.password_changed_at is None
