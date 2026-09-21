"""Unified automation job API."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models.automation import AutomationJob, AutomationJobTarget, AutomationSchedule
from app.models.device import Device
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.models.user import User
from app.schemas.automation import (
    AutomationJobCreate,
    AutomationJobListItem,
    AutomationJobResponse,
    AutomationScheduleCreate,
    AutomationScheduleResponse,
    AutomationScheduleStatusBody,
    AutomationScheduleUpdate,
)
from app.services.auth import get_current_user
from app.services.automation import create_job_record, start_automation_job
from app.services.containers_collector import (
    decode_pve_target_id as _decode_pve_target_id,
)
from app.services.containers_collector import (
    pve_target_id as _pve_target_id,
)
from app.services.cron import next_cron_time
from app.services.metrics_collector import get_latest_metrics
from app.services.permissions import (
    get_user_device_ids,
    is_admin_user,
    user_can_access_device,
    user_can_access_pve,
    user_can_access_pve_vmid,
    user_can_use_credential,
    user_has_permission,
)
from app.services.pve import build_client
from app.utils import as_utc_aware

router = APIRouter(prefix="/api/automation", tags=["automation"])

TYPE_PERMISSION = {
    "agent": "device:remote",
    "inspection": "automation:manage",
    "script": "automation:manage",
    "power": "automation:manage",
}


def _require_any_automation_permission(user: User, db: Session) -> None:
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账户已被禁用")
    if not any(user_has_permission(user, p, db) for p in set(TYPE_PERMISSION.values())):
        raise HTTPException(status_code=403, detail="权限不足")


def _validate_config(job_type: str, config: dict) -> dict:
    if job_type == "agent":
        question = str(config.get("question") or "").strip()
        if len(question) < 2 or len(question) > 500:
            raise HTTPException(status_code=400, detail="诊断问题长度应为 2-500 个字符")
        return {"question": question}
    if job_type == "inspection":
        # 自动化运维的健康巡检不再让用户挑模式，前端固定发 core；
        # quick / standard / custom 保留给巡检中心以及历史任务配置的回放。
        mode = str(config.get("mode") or "core")
        if mode not in {"core", "quick", "standard", "custom"}:
            raise HTTPException(status_code=400, detail="巡检模式无效")
        items = config.get("items") if mode == "custom" else None
        if mode == "custom" and not items:
            raise HTTPException(status_code=400, detail="自定义巡检至少选择一个检查项")
        return {
            "mode": mode,
            "items": items,
            "timeout": min(max(int(config.get("timeout") or 30), 5), 300),
        }
    if job_type == "script":
        command = str(config.get("command") or "").strip()
        if not command:
            raise HTTPException(status_code=400, detail="请输入脚本命令")
        return {
            "command": command,
            "timeout": min(max(int(config.get("timeout") or 30), 5), 300),
        }
    action = str(config.get("action") or "")
    if action not in {"shutdown", "reboot"}:
        raise HTTPException(status_code=400, detail="电源操作无效")
    return {"action": action}


@router.get("/devices")
def list_automation_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_any_automation_permission(current_user, db)
    query = db.query(Device).order_by(Device.name)
    allowed = get_user_device_ids(current_user, db)
    if allowed is not None:
        query = query.filter(Device.id.in_(allowed))
    # Windows 目标的通道连通性信号:最近一轮指标采集对 Windows 走的就是
    # WinRM,采集持续失败(而非"还没采集过")≈ WinRM 确认不通。前端选择器
    # 据此把「确认不通」的设备置为不可选;Linux/未知状态不拦——SSH 通道
    # 与指标采集失败原因交集太小,误伤大于收益。
    metrics_latest = get_latest_metrics()
    result = [
        {
            "id": device.id,
            "name": device.name,
            "ip_address": device.ip_address,
            "type": device.type,
            "status": device.status,
            "os_system": device.os_system,
            "has_credential": device.has_credential,
            "rack_id": device.rack_id,
            "metrics_failed": bool(
                device.is_windows
                and metrics_latest.get(device.id, {}).get("available") is False
            ),
        }
        for device in query.all()
    ]
    # PVE guests are represented by stable synthetic IDs. They are executable
    # for power jobs; shell/Agent jobs still require a regular device record.
    if not user_can_access_pve(current_user, db):
        return result
    for conn in db.query(PveConnection).filter(PveConnection.enabled == 1).all():
        try:
            guests = build_client(conn).list_guest_resources()
        except Exception:
            continue
        for guest in guests:
            try:
                vmid = int(guest.get("vmid"))
            except (TypeError, ValueError):
                continue
            guest_status = str(guest.get("status") or "unknown")
            gtype = guest.get("type") or "qemu"
            # 虚拟机级 ACL：device_scope='selected' 时只列出角色白名单内的 guest。
            if not user_can_access_pve_vmid(current_user, db, conn.id, vmid):
                continue
            binding = (
                db.query(PveGuestBinding)
                .filter_by(connection_id=conn.id, guest_type=gtype, vmid=vmid)
                .first()
            )
            result.append(
                {
                    "id": _pve_target_id(conn.id, vmid),
                    "name": f"[{conn.name}] {guest.get('name') or f'VM {vmid}'}",
                    "ip_address": binding.ip_address if binding else None,
                    "type": "pve_guest",
                    "status": "online" if guest_status == "running" else "offline",
                    "os_system": binding.os_system if binding else None,
                    "has_credential": bool(
                        binding
                        and binding.enabled
                        and binding.username
                        and (binding.password_enc or binding.ssh_key_enc)
                    ),
                    "rack_id": 0,
                    "target_type": "pve_guest",
                    "pve_connection_id": conn.id,
                    "pve_guest_type": gtype,
                    "pve_vmid": vmid,
                    "pve_node": guest.get("node"),
                    "guest_status": guest_status,
                }
            )
    return result


@router.post("/jobs", response_model=AutomationJobResponse)
def create_job(
    body: AutomationJobCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    permission = TYPE_PERMISSION[body.job_type]
    if not user_has_permission(current_user, permission, db):
        raise HTTPException(status_code=403, detail="权限不足")
    if body.job_type == "agent" and len(body.device_ids) != 1:
        raise HTTPException(
            status_code=400, detail="Agent 智能诊断一次只能选择一台设备"
        )

    pve_ids = [item for item in body.device_ids if _decode_pve_target_id(item)]
    if pve_ids and not user_can_access_pve(
        current_user, db, manage=body.job_type == "power"
    ):
        raise HTTPException(status_code=403, detail="无权访问或控制 PVE 虚拟机")
    for target_id in pve_ids:
        decoded = _decode_pve_target_id(target_id)
        if decoded and not user_can_access_pve_vmid(
            current_user, db, decoded[0], decoded[1]
        ):
            raise HTTPException(status_code=403, detail="无权访问该虚拟机")
    if pve_ids and body.job_type != "power":
        for target_id in pve_ids:
            decoded = _decode_pve_target_id(target_id)
            if not decoded:
                raise HTTPException(status_code=400, detail="PVE 虚拟机目标无效")
            conn_id, vmid = decoded
            gtype = "qemu"
            # Resolve type from current PVE inventory below; binding lookup is checked after inventory.
            guests_for_binding = []
            conn_check = (
                db.query(PveConnection)
                .filter(PveConnection.id == conn_id, PveConnection.enabled == 1)
                .first()
            )
            if not conn_check:
                raise HTTPException(status_code=404, detail="PVE 节点不存在或已禁用")
            try:
                guests_for_binding = build_client(conn_check).list_guest_resources()
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail=f"无法读取 PVE 虚拟机：{exc}"
                ) from exc
            guest_for_binding = next(
                (g for g in guests_for_binding if int(g.get("vmid", -1)) == vmid), None
            )
            gtype = (
                str(guest_for_binding.get("type") or "qemu")
                if guest_for_binding
                else gtype
            )
            binding = (
                db.query(PveGuestBinding)
                .filter_by(
                    connection_id=conn_id, guest_type=gtype, vmid=vmid, enabled=1
                )
                .first()
            )
            if (
                not binding
                or not binding.ip_address
                or not binding.username
                or not (binding.password_enc or binding.ssh_key_enc)
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"PVE 虚拟机 {vmid} 尚未配置有效的运维接入(IP/凭据)",
                )
            # 排除法:os_system 为空(None/历史 "unknown")按 Linux 处理——
            # Windows 需要 QGA/ostype/探测显式确证,Linux 不应要求显式识别。
    device_ids = [item for item in body.device_ids if item > 0]
    devices = db.query(Device).filter(Device.id.in_(device_ids)).all()
    device_map = {device.id: device for device in devices}
    for device_id in body.device_ids:
        pve_target = _decode_pve_target_id(device_id)
        if pve_target:
            conn_id, vmid = pve_target
            conn = (
                db.query(PveConnection)
                .filter(PveConnection.id == conn_id, PveConnection.enabled == 1)
                .first()
            )
            if not conn:
                raise HTTPException(status_code=404, detail="PVE 节点不存在或已禁用")
            continue
        device = device_map.get(device_id)
        if not device:
            raise HTTPException(status_code=404, detail=f"设备 {device_id} 不存在")
        if not user_can_access_device(current_user, device_id, db):
            raise HTTPException(status_code=403, detail=f"无权访问设备 {device.name}")
        if not user_can_use_credential(current_user, device, None, db):
            raise HTTPException(
                status_code=403, detail=f"无权使用设备 {device.name} 的凭据"
            )
        if not device.ip_address:
            raise HTTPException(
                status_code=400, detail=f"设备 {device.name} 未配置 IP 地址"
            )
        # 与 PVE 虚拟机目标同一口径:普通设备所有任务类型(含电源——电源也是
        # SSH/WinRM 连进机器执行,无带外通道)都依赖设备绑定的运维凭据,
        # 没绑的一律在提交时拦下,而不是等运行期逐台失败。
        if not device.has_credential:
            raise HTTPException(
                status_code=400,
                detail=f"设备 {device.name} 尚未配置有效的运维接入(IP/凭据)",
            )

    pve_targets = []
    for target_id in pve_ids:
        decoded = _decode_pve_target_id(target_id)
        if not decoded:
            continue
        conn_id, vmid = decoded
        conn = (
            db.query(PveConnection)
            .filter(PveConnection.id == conn_id, PveConnection.enabled == 1)
            .first()
        )
        if not conn:
            raise HTTPException(status_code=404, detail="PVE 节点不存在或已禁用")
        try:
            guest = next(
                (
                    g
                    for g in build_client(conn).list_guest_resources()
                    if int(g.get("vmid", -1)) == vmid
                ),
                None,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"无法读取 PVE 虚拟机：{exc}"
            ) from exc
        if not guest:
            raise HTTPException(status_code=404, detail=f"PVE 虚拟机 {vmid} 不存在")
        guest_type = guest.get("type") or "qemu"
        binding = (
            db.query(PveGuestBinding)
            .filter_by(
                connection_id=conn_id, guest_type=guest_type, vmid=vmid, enabled=1
            )
            .first()
        )
        pve_targets.append(
            {
                "connection_id": conn_id,
                "vmid": vmid,
                "guest_type": guest_type,
                "name": f"[{conn.name}] {guest.get('name') or f'VM {vmid}'}",
                "ip_address": binding.ip_address if binding else None,
            }
        )
    config = _validate_config(body.job_type, body.config)
    job = create_job_record(
        db,
        name=body.name,
        job_type=body.job_type,
        device_ids=device_ids,
        pve_targets=pve_targets,
        config=config,
        user_id=current_user.id,
        user_name=current_user.display_name or current_user.username,
    )
    job_id = job.id
    response = AutomationJobResponse.model_validate(job)
    start_automation_job(job_id)
    return response


@router.get("/jobs")
def list_jobs(
    job_type: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_any_automation_permission(current_user, db)
    # steps 一并预载:agent 任务的 steps_executed 聚合需要各目标的步骤数,
    # 惰性加载会在循环里逐目标发 N+1 查询。
    query = db.query(AutomationJob).options(
        selectinload(AutomationJob.targets).selectinload(AutomationJobTarget.steps)
    )
    allowed = get_user_device_ids(current_user, db)
    if allowed is not None:
        query = (
            query.join(AutomationJobTarget)
            .filter(
                or_(
                    AutomationJobTarget.device_id.in_(allowed),
                    AutomationJob.created_by == current_user.id,
                )
            )
            .distinct()
        )
    if job_type:
        query = query.filter(AutomationJob.job_type == job_type)
    if status:
        query = query.filter(AutomationJob.status == status)
    total = query.count()
    rows = (
        query.order_by(AutomationJob.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = []
    for job in rows:
        statuses = [target.status for target in job.targets]
        # Agent 任务实时进度:运行中目标已累计的步骤数(steps 由执行线程
        # 增量落库)。列表页进度条据此从 1% 平滑爬升,而不是等终态跳变。
        # 巡检/脚本任务的步骤是固定的四五个,粒度太粗没有折算价值,恒 0。
        steps_executed = (
            sum(
                len(target.steps)
                for target in job.targets
                if target.status == "running"
            )
            if job.job_type == "agent"
            else 0
        )
        items.append(
            AutomationJobListItem(
                id=job.id,
                name=job.name,
                job_type=job.job_type,
                trigger_type=job.trigger_type,
                status=job.status,
                risk_level=job.risk_level,
                created_by_name=job.created_by_name,
                created_at=job.created_at,
                started_at=job.started_at,
                finished_at=job.finished_at,
                target_total=len(statuses),
                succeeded=sum(item == "completed" for item in statuses),
                warning=sum(item == "warning" for item in statuses),
                failed=sum(item == "failed" for item in statuses),
                running=sum(item in {"pending", "running"} for item in statuses),
                steps_executed=steps_executed,
            )
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/jobs/{job_id}", response_model=AutomationJobResponse)
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_any_automation_permission(current_user, db)
    job = (
        db.query(AutomationJob)
        .options(
            selectinload(AutomationJob.targets).selectinload(AutomationJobTarget.steps)
        )
        .filter(AutomationJob.id == job_id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    for target in job.targets:
        if target.device_id and not user_can_access_device(
            current_user, target.device_id, db
        ):
            raise HTTPException(status_code=403, detail="无权查看该任务")
        if target.target_type == "pve_guest" and not user_can_access_pve(
            current_user, db
        ):
            raise HTTPException(status_code=403, detail="无权查看 PVE 虚拟机任务")
        if (
            target.target_type == "pve_guest"
            and target.pve_connection_id
            and target.pve_vmid
            and not user_can_access_pve_vmid(
                current_user, db, target.pve_connection_id, target.pve_vmid
            )
        ):
            raise HTTPException(status_code=403, detail="无权查看该虚拟机任务")
    return job


def _schedule_visible(
    current_user: User,
    db: Session,
    row: AutomationSchedule,
    allowed: list[int] | None,
) -> bool:
    """计划列表可见性：权限族 + 设备/虚拟机双维度 ACL。"""
    if not user_has_permission(current_user, TYPE_PERMISSION[row.job_type], db):
        return False
    if allowed is None:
        return True
    for target_id in row.target_ids:
        decoded = _decode_pve_target_id(target_id)
        if decoded:
            if not user_can_access_pve(
                current_user, db
            ) or not user_can_access_pve_vmid(current_user, db, decoded[0], decoded[1]):
                return False
        elif target_id not in allowed:
            return False
    return True


@router.get("/schedules", response_model=list[AutomationScheduleResponse])
def list_schedules(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_any_automation_permission(current_user, db)
    allowed = get_user_device_ids(current_user, db)
    rows = (
        db.query(AutomationSchedule)
        .order_by(AutomationSchedule.created_at.desc())
        .all()
    )
    return [row for row in rows if _schedule_visible(current_user, db, row, allowed)]


def _schedule_pve_binding_ready(db: Session, connection_id: int, vmid: int) -> bool:
    """定时计划里的 PVE 目标(非 power 任务)是否已配置有效运维接入。

    与 create_job 的口径一致(binding 存在且 ip/username/凭据齐全);不实时
    查 PVE inventory——计划是长期配置,到期执行时 _pve_runtime_device 还会
    用 QGA 复核最新 IP,创建时确认绑定完整即可。
    """
    binding = (
        db.query(PveGuestBinding)
        .filter_by(
            connection_id=connection_id,
            vmid=vmid,
            enabled=1,
        )
        .first()
    )
    return bool(
        binding
        and binding.ip_address
        and binding.username
        and (binding.password_enc or binding.ssh_key_enc)
    )


@router.post("/schedules", response_model=AutomationScheduleResponse)
def create_schedule(
    body: AutomationScheduleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # PVE 虚拟机定时计划已支持(与立即执行同口径):目标校验、到期授权、
    # create_job_record 的 pve_targets 拼装都按正/负 id 分流。
    permission = TYPE_PERMISSION[body.job_type]
    if not user_has_permission(current_user, permission, db):
        raise HTTPException(status_code=403, detail="权限不足")
    if body.job_type == "agent" and len(body.device_ids) != 1:
        raise HTTPException(
            status_code=400, detail="Agent 智能诊断计划一次只能选择一台设备"
        )
    pve_ids = [item for item in body.device_ids if _decode_pve_target_id(item)]
    if pve_ids and not user_can_access_pve(
        current_user, db, manage=body.job_type == "power"
    ):
        raise HTTPException(status_code=403, detail="无权访问或控制 PVE 虚拟机")
    for target_id in pve_ids:
        decoded = _decode_pve_target_id(target_id)
        if decoded and not user_can_access_pve_vmid(
            current_user, db, decoded[0], decoded[1]
        ):
            raise HTTPException(status_code=403, detail="无权访问该虚拟机")
        if (
            decoded
            and body.job_type != "power"
            and not _schedule_pve_binding_ready(db, decoded[0], decoded[1])
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"PVE 虚拟机 {decoded[1]} 尚未配置有效的运维接入(IP/凭据)，"
                    "无法创建该类型的定时计划"
                ),
            )
    device_ids = [item for item in body.device_ids if item > 0]
    devices = db.query(Device).filter(Device.id.in_(device_ids)).all()
    if len(devices) != len(device_ids):
        raise HTTPException(status_code=404, detail="部分目标设备不存在")
    for device in devices:
        if not user_can_access_device(current_user, device.id, db):
            raise HTTPException(status_code=403, detail=f"无权访问设备 {device.name}")
    config = _validate_config(body.job_type, body.config)
    now = datetime.now(timezone.utc)
    if body.schedule_type == "once":
        # 客户端没带时区的时间按 UTC 解释(库里的 DATETIME 就是 UTC 墙钟)
        scheduled_at = as_utc_aware(body.scheduled_at)
        if scheduled_at is None or scheduled_at <= now:
            raise HTTPException(status_code=400, detail="一次性计划必须选择未来时间")
        next_run = scheduled_at
    else:
        if not body.cron_expression:
            raise HTTPException(status_code=400, detail="周期计划需要 cron 表达式")
        try:
            next_run = next_cron_time(body.cron_expression, now)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"无效的 cron 表达式: {exc}"
            ) from exc
    schedule = AutomationSchedule(
        name=body.name.strip(),
        job_type=body.job_type,
        target_ids=body.device_ids,
        config_json=config,
        schedule_type=body.schedule_type,
        scheduled_at=scheduled_at if body.schedule_type == "once" else None,
        cron_expression=body.cron_expression,
        status="active",
        next_run_at=next_run,
        created_by=current_user.id,
        created_by_name=current_user.display_name or current_user.username,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.put("/schedules/{schedule_id}", response_model=AutomationScheduleResponse)
def update_schedule(
    schedule_id: int,
    body: AutomationScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """编辑计划:名称/目标/配置/周期可改;job_type 不可改。

    权限与目标校验同 create_schedule 口径(含 PVE 虚机 binding 检查);
    修改周期后 next_run_at 按新表达式重算——不等旧周期到期。
    paused 状态下编辑保留暂停:next_run_at 仍会重算,恢复后按新周期跑。
    """
    schedule = db.query(AutomationSchedule).filter_by(id=schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="计划不存在")
    if not (is_admin_user(current_user) or schedule.created_by == current_user.id):
        raise HTTPException(status_code=404, detail="计划不存在")
    if not user_has_permission(current_user, TYPE_PERMISSION[schedule.job_type], db):
        raise HTTPException(status_code=403, detail="权限不足")

    # 逐字段合并:未提供的保持原值
    new_name = (body.name or schedule.name).strip()
    new_device_ids = (
        body.device_ids
        if body.device_ids is not None
        else list(schedule.target_ids or [])
    )
    new_config = (
        _validate_config(schedule.job_type, body.config)
        if body.config is not None
        else dict(schedule.config_json or {})
    )
    new_schedule_type = body.schedule_type or schedule.schedule_type
    new_scheduled_at = (
        as_utc_aware(body.scheduled_at)
        if body.scheduled_at is not None
        else schedule.scheduled_at
    )
    new_cron = (
        body.cron_expression
        if body.cron_expression is not None
        else schedule.cron_expression
    )

    # 目标校验与创建同口径(设备存在+ACL+凭据/虚机 binding)
    pve_ids = [item for item in new_device_ids if _decode_pve_target_id(item)]
    if pve_ids and not user_can_access_pve(
        current_user, db, manage=schedule.job_type == "power"
    ):
        raise HTTPException(status_code=403, detail="无权访问或控制 PVE 虚拟机")
    for target_id in pve_ids:
        decoded = _decode_pve_target_id(target_id)
        if decoded and not user_can_access_pve_vmid(
            current_user, db, decoded[0], decoded[1]
        ):
            raise HTTPException(status_code=403, detail="无权访问该虚拟机")
        if (
            decoded
            and schedule.job_type != "power"
            and not _schedule_pve_binding_ready(db, decoded[0], decoded[1])
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"PVE 虚拟机 {decoded[1]} 尚未配置有效的运维接入(IP/凭据)，"
                    "无法设为该类型计划的目标"
                ),
            )
    device_ids = [item for item in new_device_ids if item > 0]
    devices = db.query(Device).filter(Device.id.in_(device_ids)).all()
    if len(devices) != len(device_ids):
        raise HTTPException(status_code=404, detail="部分目标设备不存在")
    for device in devices:
        if not user_can_access_device(current_user, device.id, db):
            raise HTTPException(status_code=403, detail=f"无权访问设备 {device.name}")

    # 周期重算
    now = datetime.now(timezone.utc)
    if new_schedule_type == "once":
        next_run = new_scheduled_at
        if next_run is None or next_run <= now:
            raise HTTPException(status_code=400, detail="一次性计划必须选择未来时间")
    else:
        if not new_cron:
            raise HTTPException(status_code=400, detail="周期计划需要 cron 表达式")
        try:
            next_run = next_cron_time(new_cron, now)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"无效的 cron 表达式: {exc}"
            ) from exc

    schedule.name = new_name
    schedule.target_ids = new_device_ids
    schedule.config_json = new_config
    schedule.schedule_type = new_schedule_type
    schedule.scheduled_at = new_scheduled_at if new_schedule_type == "once" else None
    schedule.cron_expression = new_cron if new_schedule_type == "recurring" else None
    schedule.next_run_at = next_run
    # completed(一次性已跑完)的计划编辑后重新激活
    if schedule.status in ("completed",):
        schedule.status = "active"
    db.commit()
    db.refresh(schedule)
    return schedule


@router.patch(
    "/schedules/{schedule_id}/status", response_model=AutomationScheduleResponse
)
def update_schedule_status(
    schedule_id: int,
    body: AutomationScheduleStatusBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """暂停/恢复计划。

    paused = 用户主动暂停,随时恢复,到期扫描跳过(disabled = 授权失效系统停用,
    需排查后重建,两者严格区分)。恢复时若 next_run_at 已过期,按 cron 重算
    到未来时刻——否则恢复瞬间立刻补跑一大堆积压周期。
    """
    schedule = db.query(AutomationSchedule).filter_by(id=schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="计划不存在")
    if not (is_admin_user(current_user) or schedule.created_by == current_user.id):
        raise HTTPException(status_code=404, detail="计划不存在")
    if not user_has_permission(current_user, TYPE_PERMISSION[schedule.job_type], db):
        raise HTTPException(status_code=403, detail="权限不足")
    if schedule.status == "disabled":
        raise HTTPException(
            status_code=400,
            detail="计划已被系统停用(权限失效)，请排查后重新创建",
        )

    if body.status == "paused":
        schedule.status = "paused"
        # next_run_at 保留:恢复时以此为基准判断是否需要重算
    else:
        schedule.status = "active"
        # 恢复时把过期的 next_run_at 重算到未来(只针对周期计划):
        # 暂停一周后恢复,不应该把过去一周的每个周期都补跑一遍。
        if schedule.schedule_type == "recurring" and schedule.cron_expression:
            now = datetime.now(timezone.utc)
            if schedule.next_run_at is None or schedule.next_run_at <= now:
                try:
                    schedule.next_run_at = next_cron_time(schedule.cron_expression, now)
                except ValueError:
                    pass  # 表达式本就无效(创建时已校验过),保持原值
    db.commit()
    db.refresh(schedule)
    return schedule


@router.delete("/schedules/{schedule_id}")
def delete_schedule(
    schedule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_any_automation_permission(current_user, db)
    schedule = db.query(AutomationSchedule).filter_by(id=schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="计划不存在")
    if not (is_admin_user(current_user) or schedule.created_by == current_user.id):
        raise HTTPException(status_code=404, detail="计划不存在")
    if not user_has_permission(current_user, TYPE_PERMISSION[schedule.job_type], db):
        raise HTTPException(status_code=403, detail="权限不足")
    for target_id in schedule.target_ids:
        decoded = _decode_pve_target_id(target_id)
        if decoded:
            if not user_can_access_pve(
                current_user, db, manage=schedule.job_type == "power"
            ) or not user_can_access_pve_vmid(current_user, db, decoded[0], decoded[1]):
                raise HTTPException(status_code=403, detail="无权管理 PVE 计划")
        elif not user_can_access_device(current_user, target_id, db):
            raise HTTPException(status_code=403, detail="无权管理该计划")
    db.delete(schedule)
    db.commit()
    return {"message": "计划已删除"}
