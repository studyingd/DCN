"""Unified automation job models.

Agent diagnosis, health inspection, scripts and power actions keep their own
specialised executors, but every run is represented by the same job contract.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime


class AutomationJob(Base):
    __tablename__ = "automation_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_type: Mapped[str] = mapped_column(String(24), nullable=False)
    trigger_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual"
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    risk_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="read_only"
    )
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    targets: Mapped[list["AutomationJobTarget"]] = relationship(
        "AutomationJobTarget",
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AutomationJobTarget.id",
    )

    __table_args__ = (
        Index("ix_automation_jobs_created_at", "created_at"),
        Index("ix_automation_jobs_type_status", "job_type", "status"),
    )


class AutomationJobTarget(Base):
    __tablename__ = "automation_job_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("automation_jobs.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    device_name: Mapped[str] = mapped_column(String(255), nullable=False)
    device_ip: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="device"
    )
    pve_connection_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pve_guest_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    pve_vmid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    job: Mapped[AutomationJob] = relationship("AutomationJob", back_populates="targets")
    steps: Mapped[list["AutomationJobStep"]] = relationship(
        "AutomationJobStep",
        back_populates="target",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AutomationJobStep.id",
    )

    __table_args__ = (Index("ix_automation_targets_job_status", "job_id", "status"),)


class AutomationJobStep(Base):
    __tablename__ = "automation_job_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("automation_job_targets.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_type: Mapped[str] = mapped_column(String(40), nullable=False)
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    target: Mapped[AutomationJobTarget] = relationship(
        "AutomationJobTarget", back_populates="steps"
    )


class AutomationSchedule(Base):
    __tablename__ = "automation_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_type: Mapped[str] = mapped_column(String(24), nullable=False)
    target_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cron_expression: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_automation_schedules_next_run", "status", "next_run_at"),
    )
