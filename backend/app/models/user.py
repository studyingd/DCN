from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime
from app.utils import as_utc_aware


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
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc)
    )

    # ── Security fields ──
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_login: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    # Incremented on password changes to invalidate all previously issued JWTs.
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    role_ref: Mapped["Role | None"] = relationship("Role", back_populates="users")  # type: ignore[name-defined]

    @property
    def is_locked(self) -> bool:
        """Check if the account is currently locked due to too many failed attempts."""
        if self.locked_until is None:
            return False
        locked_until = as_utc_aware(self.locked_until)
        return bool(locked_until and locked_until > datetime.now(timezone.utc))

    @property
    def must_change_password(self) -> bool:
        """True if user has never changed password (first login)."""
        return self.password_changed_at is None
