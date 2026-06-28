"""Scheduled tasks CRUD API."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.device import Device
from app.models.scheduled_task import ScheduledTask
from app.models.user import User
from app.schemas.scheduled_task import (
    ScheduledTaskCreate,
    ScheduledTaskResponse,
    ScheduledTaskUpdate,
)
from app.services.cron import next_cron_time, parse_cron
from app.services.permissions import require_permission, user_can_access_device
from app.utils import apply_update, utcnow
from app.validators import validate_script_command

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scheduled-tasks"])


def _compute_next_run(body: ScheduledTaskCreate) -> datetime:
    now = utcnow()
    if body.schedule_type == "once":
        if body.scheduled_at is None:
            raise ValueError("一次性任务需要指定执行时间")
        return body.scheduled_at
    else:
        if not body.cron_expression:
            raise ValueError("周期任务需要指定 Cron 表达式")
        parse_cron(body.cron_expression)
        return next_cron_time(body.cron_expression, now)


def _to_response(task: ScheduledTask) -> ScheduledTaskResponse:
    return ScheduledTaskResponse.model_validate(task)


@router.get("/api/scheduled-tasks", response_model=list[ScheduledTaskResponse])
def list_scheduled_tasks(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("script:manage")),
):
    tasks = db.query(ScheduledTask).order_by(ScheduledTask.id.desc()).all()
    return [_to_response(t) for t in tasks]


@router.post(
    "/api/scheduled-tasks",
    response_model=ScheduledTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_scheduled_task(
    body: ScheduledTaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("script:manage")),
):
    # Validate device access
    devices = db.query(Device).filter(Device.id.in_(body.device_ids)).all()
    device_map = {d.id: d for d in devices}
    for did in body.device_ids:
        if did not in device_map:
            raise HTTPException(status_code=404, detail=f"设备 {did} 不存在")
        if not user_can_access_device(current_user, did, db):
            raise HTTPException(
                status_code=403, detail=f"无权访问设备 {device_map[did].name}"
            )

    # Reject dangerous commands before persisting a scheduled task
    try:
        validate_script_command(body.command)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        next_run = _compute_next_run(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if (
        body.schedule_type == "once"
        and body.scheduled_at
        and body.scheduled_at < utcnow()
    ):
        raise HTTPException(status_code=400, detail="执行时间必须在当前时间之后")

    initial_status = "active" if body.schedule_type == "recurring" else "pending"

    task = ScheduledTask(
        name=body.name,
        command=body.command,
        device_ids=body.device_ids,
        schedule_type=body.schedule_type,
        scheduled_at=body.scheduled_at,
        cron_expression=body.cron_expression,
        timeout=body.timeout,
        status=initial_status,
        next_run_at=next_run,
        credential_id=body.credential_id,
        created_by=current_user.id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return _to_response(task)


@router.put("/api/scheduled-tasks/{task_id}", response_model=ScheduledTaskResponse)
def update_scheduled_task(
    task_id: int,
    body: ScheduledTaskUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("script:manage")),
):
    task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    update_data = body.model_dump(exclude_unset=True)

    # Handle status changes
    if "status" in update_data:
        new_status = update_data.pop("status")
        if new_status in ("active",):
            if task.schedule_type == "recurring" and task.cron_expression:
                task.next_run_at = next_cron_time(task.cron_expression, utcnow())
            elif task.schedule_type == "once" and task.scheduled_at:
                task.next_run_at = task.scheduled_at
        elif new_status in ("paused", "disabled"):
            task.next_run_at = None
        task.status = new_status

    # Validate cron if changed
    if "cron_expression" in update_data and update_data["cron_expression"]:
        try:
            parse_cron(update_data["cron_expression"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # Validate command if changed (block dangerous patterns)
    if "command" in update_data and update_data["command"]:
        try:
            validate_script_command(update_data["command"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # Validate device access if changed
    if "device_ids" in update_data:
        for did in update_data["device_ids"]:
            if not user_can_access_device(current_user, did, db):
                raise HTTPException(status_code=403, detail=f"无权访问设备 {did}")

    apply_update(
        task,
        update_data,
        ["name", "command", "device_ids", "schedule_type", "scheduled_at",
         "cron_expression", "timeout", "credential_id"],
    )

    # Recompute next_run if schedule fields changed and task is active
    if task.status in ("active", "pending"):
        if task.schedule_type == "recurring" and task.cron_expression:
            task.next_run_at = next_cron_time(task.cron_expression, utcnow())
        elif task.schedule_type == "once" and task.scheduled_at:
            task.next_run_at = task.scheduled_at

    db.commit()
    db.refresh(task)
    return _to_response(task)


@router.delete("/api/scheduled-tasks/{task_id}")
def delete_scheduled_task(
    task_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("script:manage")),
):
    task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    db.delete(task)
    db.commit()
    return {"message": "任务已删除"}
