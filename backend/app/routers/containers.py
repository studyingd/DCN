"""Docker 容器管理 API —— 普通设备与 PVE Guest 探测展示 + 启停控制。

- GET /api/containers             全部服务器的容器概览(RBAC 过滤)
- GET /api/containers/{device_id} 单台设备的容器明细
- POST /api/containers/{device_id}/control  启停/重启容器(device:manage 或 pve:manage 权限+审计)
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.rate_limiter import limiter
from app.models.device import OPS_TARGET_TYPES, Device, is_windows_os
from app.models.device_container import (
    ContainerAction,
    DeviceContainer,
    DeviceDockerStatus,
)
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.models.user import User
from app.services.containers_collector import (
    _spawn_collect_one,
    decode_pve_target_id,
    fetch_container_logs,
    load_container_target,
    pve_target_id,
    remove_container,
    run_container_action,
)
from app.services.permissions import (
    get_user_device_ids,
    get_user_pve_guest_keys,
    require_any_permission,
    require_permission,
    user_can_access_device,
    user_can_access_pve,
    user_has_any_permission,
)
from app.services.pve_guest_status import get_guest_snapshot
from app.services.ws_ticket import issue_ticket
from app.validators import ALLOWED_CONTAINER_ACTIONS, validate_container_name

router = APIRouter(prefix="/api/containers", tags=["containers"])


class ControlRequest(BaseModel):
    action: str = Field(description="start / stop / restart / remove")
    name: str = Field(min_length=1, max_length=128, description="容器名")
    # 仅 remove 生效:True=保留匿名卷(docker rm),False=连带删除(docker rm -v)。
    # 具名卷与 bind mount 任何选项下都保留。其余动作忽略该字段。
    keep_volumes: bool = Field(default=True, description="删除容器时是否保留其数据卷")


class ContainerTerminalTicketRequest(BaseModel):
    container: str = Field(min_length=1, max_length=128, description="容器名")


def _container_dicts(
    db: Session, target_id: int, *, binding_id: int | None = None
) -> list[dict]:
    query = db.query(DeviceContainer)
    query = query.filter(
        DeviceContainer.pve_guest_binding_id == binding_id
        if binding_id is not None
        else DeviceContainer.device_id == target_id
    )
    rows = query.order_by(DeviceContainer.name).all()
    return [
        {
            "container_id": c.container_id,
            "name": c.name,
            "image": c.image,
            "state": c.state,
            "status": c.status,
            "ports": c.ports,
            "cpu_pct": c.cpu_pct,
            "mem_used_mb": c.mem_used_mb,
            "mem_limit_mb": c.mem_limit_mb,
            "mem_pct": c.mem_pct,
        }
        for c in rows
    ]


def _status_entry(
    db: Session, *, device_id: int | None = None, binding_id: int | None = None
):
    query = db.query(DeviceDockerStatus)
    query = query.filter(
        DeviceDockerStatus.pve_guest_binding_id == binding_id
        if binding_id is not None
        else DeviceDockerStatus.device_id == device_id
    )
    return query.first()


def _device_entry(db: Session, device: Device) -> dict:
    st = _status_entry(db, device_id=device.id)
    return {
        "device_id": device.id,
        "device_name": device.name,
        "ip_address": device.ip_address,
        "os_system": device.os_system,
        "device_status": device.status,
        "credential_id": None,
        "has_credential": device.has_credential,
        "available": bool(st.available) if st else False,
        "version": st.version if st else None,
        "container_count": st.container_count if st else 0,
        "last_error": st.last_error if st else None,
        "commands": json.loads(st.command_status_json)
        if st and st.command_status_json
        else {},
        "updated_at": st.updated_at.isoformat() if st and st.updated_at else None,
        "target_type": "device",
        "pve_connection_id": None,
        "pve_guest_type": None,
        "pve_vmid": None,
        "containers": _container_dicts(db, device.id),
    }


def _guest_key(target) -> tuple[int, str, int] | None:
    """ContainerTarget → 角色 ACL 使用的 guest 稳定身份。"""
    binding = getattr(target, "binding", None)
    if binding is None:
        return None
    return (binding.connection_id, binding.guest_type, binding.vmid)


def _pve_entries(db: Session, current_user: User | None = None) -> list[dict]:
    if current_user is not None and not user_can_access_pve(current_user, db):
        return []
    # 虚拟机级 ACL：device_scope='selected' 时只保留角色白名单内的 guest。
    allowed_guests = (
        None if current_user is None else get_user_pve_guest_keys(current_user, db)
    )
    if allowed_guests is not None and not allowed_guests:
        return []
    connections = {
        conn.id: conn
        for conn in db.query(PveConnection).filter(PveConnection.enabled == 1).all()
    }
    entries: list[dict] = []
    bindings = (
        db.query(PveGuestBinding)
        .filter(
            PveGuestBinding.enabled == 1,
            PveGuestBinding.connection_id.in_(list(connections) or [0]),
        )
        .order_by(PveGuestBinding.connection_id, PveGuestBinding.vmid)
        .all()
    )
    for binding in bindings:
        if (
            allowed_guests is not None
            and (
                binding.connection_id,
                binding.guest_type,
                binding.vmid,
            )
            not in allowed_guests
        ):
            continue
        conn = connections[binding.connection_id]
        target_id = pve_target_id(binding.connection_id, binding.vmid)
        st = _status_entry(db, binding_id=binding.id)
        # 容器管理是 Docker 清单视图，没有发现容器的 PVE Guest 不显示。
        # 未采集、Docker 未安装或采集结果为空时，都不会出现在列表中。
        if not st or int(st.container_count or 0) <= 0:
            continue
        # The binding table deliberately stores only access metadata. Avoid a
        # live PVE API call on every 20s UI poll; guest status/name come from
        # the in-memory pve_guest_status snapshot (30s collector, zero API
        # cost), while Docker state comes from the guest probe itself.
        # Snapshot not ready yet (connection just added) → fall back to the
        # binding identity (QEMU VM 101).
        snap = get_guest_snapshot(binding.connection_id, binding.vmid) or {}
        snap_name = str(snap.get("name") or "").strip()
        snap_status = str(snap.get("status") or "").strip()
        guest_status = snap_status or "unknown"
        device_name = (
            f"[{conn.name}] {snap_name}"
            if snap_name
            else f"[{conn.name}] {binding.guest_type.upper()} VM {binding.vmid}"
        )
        has_credential = bool(
            binding.username and (binding.password_enc or binding.ssh_key_enc)
        )
        entries.append(
            {
                "device_id": target_id,
                "device_name": device_name,
                "ip_address": binding.ip_address,
                "os_system": binding.os_system,
                # Do not present an unqueried PVE guest as offline.  The
                # container snapshot is authoritative for Docker availability;
                # guest power state is intentionally not fetched on every poll.
                "device_status": guest_status,
                "credential_id": None,
                "has_credential": has_credential,
                "available": bool(st.available) if st else False,
                "version": st.version if st else None,
                "container_count": st.container_count if st else 0,
                "last_error": st.last_error
                if st
                else (None if has_credential else "未绑定有效凭据"),
                "commands": json.loads(st.command_status_json)
                if st and st.command_status_json
                else {},
                "updated_at": st.updated_at.isoformat()
                if st and st.updated_at
                else None,
                "target_type": "pve_guest",
                "pve_connection_id": binding.connection_id,
                "pve_guest_type": binding.guest_type,
                "pve_vmid": binding.vmid,
                "guest_status": guest_status,
                "containers": _container_dicts(db, target_id, binding_id=binding.id),
            }
        )
    return entries


def _target_entry(
    db: Session, target_id: int, current_user: User | None = None
) -> dict | None:
    if target_id > 0:
        device = db.query(Device).filter(Device.id == target_id).first()
        return _device_entry(db, device) if device else None
    decoded = decode_pve_target_id(target_id)
    if not decoded:
        return None
    conn_id, vmid = decoded
    return next(
        (
            item
            for item in _pve_entries(db, current_user)
            if item["pve_connection_id"] == conn_id and item["pve_vmid"] == vmid
        ),
        None,
    )


@router.get("")
def list_containers(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """普通纳管设备(server/cloud_server/host) + PVE 页面已绑定 guest 的 Docker 概览。"""
    query = (
        db.query(Device).filter(Device.type.in_(OPS_TARGET_TYPES)).order_by(Device.id)
    )
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        query = query.filter(Device.id.in_(allowed_ids))
    items = [_device_entry(db, d) for d in query.all()]
    # PVE guests are separately bound in the PVE module; unlike a physical
    # device they have no RoleDeviceAccess row to match against.
    items.extend(_pve_entries(db, current_user))
    return {"items": items}


@router.get("/{device_id}")
def get_device_containers(
    device_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    if device_id < 0:
        if not user_can_access_pve(current_user, db):
            raise HTTPException(status_code=403, detail="无权访问 PVE 虚拟机")
        # _pve_entries 已按角色 ACL 过滤，取不到即为无权或不存在。
        entry = _target_entry(db, device_id, current_user)
        if entry is None:
            raise HTTPException(status_code=404, detail="虚拟机不存在或未配置运维接入")
        return entry
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    if not user_can_access_device(current_user, device_id, db):
        raise HTTPException(status_code=403, detail="无权访问该设备")
    return _device_entry(db, device)


@router.post("/{device_id}/control")
def control_container(
    device_id: int,
    body: ControlRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_any_permission("device:manage", "pve:manage")),
):
    """启停/重启容器(白名单动作 + 容器名校验 + 审计)。"""
    action = body.action.strip().lower()
    if action not in ALLOWED_CONTAINER_ACTIONS:
        raise HTTPException(status_code=400, detail=f"不支持的操作: {action}")
    try:
        name = validate_container_name(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    target = load_container_target(db, device_id)
    if target is None:
        raise HTTPException(status_code=404, detail="设备或虚拟机不存在/未配置运维接入")
    # 权限分域:设备容器要 device:manage,虚机容器要 pve:manage。端点门是
    # "任一即可"(路由签名简洁),但 device 分支只查 ACL 不查权限键的话,
    # 仅持 pve:manage 的用户能控制物理设备容器——与项目定调相悖。
    if target.kind == "device" and not (
        user_can_access_device(current_user, target.entity_id, db)
        and user_has_any_permission(current_user, ("device:manage",), db)
    ):
        raise HTTPException(status_code=403, detail="无权访问该设备")
    if target.kind == "pve_guest" and not user_can_access_pve(
        current_user, db, manage=True, guest=_guest_key(target)
    ):
        raise HTTPException(status_code=403, detail="无权控制 PVE 虚拟机容器")
    if not target.username or (not target.password and not target.ssh_key):
        raise HTTPException(status_code=400, detail="目标未绑定有效凭据")

    try:
        if action == "remove":
            # 删除有专门分支(stop + rm[-v]),不直接拼 `docker remove`——
            # docker CLI 没有 remove 子命令,白名单放行它只是路由层的动作名。
            code, out, err = remove_container(
                target, name, keep_volumes=body.keep_volumes
            )
        else:
            code, out, err = run_container_action(target, action, name)
    except Exception as exc:
        code, out, err = 1, "", str(exc)

    success = code == 0
    message = (out or err or "").strip()[:300] or (
        "操作成功" if success else "操作失败"
    )

    # 审计
    db.add(
        ContainerAction(
            user_id=current_user.id,
            device_id=target.entity_id if target.kind == "device" else None,
            pve_guest_binding_id=target.entity_id
            if target.kind == "pve_guest"
            else None,
            device_name=target.name,
            container_name=name,
            action=action,
            success=1 if success else 0,
            message=message,
            keep_volumes=(bool(body.keep_volumes) if action == "remove" else None),
        )
    )
    db.commit()

    # 后台立即全量刷新该设备的容器快照(含 stats:持久化是整批重写,
    # 跳过 stats 会把整台机器的指标抹成 NULL),让前端尽快看到新状态。
    # 同目标的在采探测去重,避免连点操作时并发开多条 SSH 探测同一台主机。
    _spawn_collect_one(device_id)

    if not success:
        raise HTTPException(status_code=502, detail=message)
    return {"success": True, "message": message or "操作已执行"}


@router.post("/{device_id}/terminal-ticket")
def container_terminal_ticket(
    device_id: int,
    body: ContainerTerminalTicketRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("pve:manage")),
):
    """为 PVE 虚机上的容器签发交互式终端票据(kind=pve-remote + container)。

    普通设备的容器终端走既有 ``/api/terminal/ticket``(device:remote);
    这里只服务负数合成 target_id 的虚机。开票不调 PVE API——binding 的
    SSH 通道正是容器采集器在用的通道(能列出容器就说明它活着),
    target_ip 直接取 binding.ip_address。权限与 /ws/pve-terminal 的
    复验口径(pve:manage + 虚拟机级 ACL)一致。
    """
    if device_id >= 0:
        raise HTTPException(
            status_code=400, detail="设备容器终端请使用 /api/terminal/ticket"
        )
    decoded = decode_pve_target_id(device_id)
    if not decoded:
        raise HTTPException(status_code=404, detail="虚拟机不存在或未配置运维接入")
    conn_id, vmid = decoded
    try:
        container = validate_container_name(body.container)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    binding = (
        db.query(PveGuestBinding)
        .filter_by(connection_id=conn_id, vmid=vmid, enabled=1)
        .first()
    )
    if binding is None:
        raise HTTPException(status_code=404, detail="虚拟机未配置或已停用运维接入")
    conn = db.query(PveConnection).filter_by(id=conn_id, enabled=1).first()
    if conn is None:
        raise HTTPException(status_code=404, detail="PVE 平台不存在或已停用")
    if not user_can_access_pve(
        current_user, db, manage=True, guest=(conn_id, binding.guest_type, vmid)
    ):
        raise HTTPException(status_code=403, detail="无权访问 PVE 虚拟机")
    # 排除法:binding.os_system 只存 windows/linux 或 NULL,NULL 按项目口径视作 Linux
    if is_windows_os(binding.os_system):
        raise HTTPException(
            status_code=400, detail="Windows 虚机容器终端请使用 RDP 控制台"
        )
    if not binding.ip_address or not binding.username:
        raise HTTPException(status_code=400, detail="虚拟机未配置运维接入")
    if not (binding.password_enc or binding.ssh_key_enc):
        raise HTTPException(status_code=400, detail="虚拟机未绑定有效凭据")

    payload = {
        "kind": "pve-remote",
        "mode": "ssh",
        "binding_id": binding.id,
        "conn_id": conn_id,
        "gtype": binding.guest_type,
        "vmid": vmid,
        "target_ip": binding.ip_address,
        "container": container,
        "user_id": current_user.id,
        "session_version": int(getattr(current_user, "session_version", 0)),
    }
    return {"ticket": issue_ticket(payload)}


@router.get("/{device_id}/logs/{container_name}")
# 每次抓日志都是一趟 SSH/WinRM 往返。前端日志弹窗默认开 5s 自动刷新(=12 次/分钟)，
# 所以限额给到 120/minute:留 10 倍余量，同时挡住拿这个接口做持续探测的行为。
@limiter.limit("120/minute")
def get_container_logs(
    device_id: int,
    container_name: str,
    request: Request,
    tail: int = Query(
        100, ge=1, le=5000, description="返回最近多少行(默认与前端日志弹窗一致)"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """抓取容器最近 tail 行日志(只读;Linux 走 SSH、Windows 走 WinRM)。"""
    try:
        name = validate_container_name(container_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    target = load_container_target(db, device_id)
    if target is None:
        raise HTTPException(status_code=404, detail="设备或虚拟机不存在/未配置运维接入")
    if target.kind == "device" and not user_can_access_device(
        current_user, target.entity_id, db
    ):
        raise HTTPException(status_code=403, detail="无权访问该设备")
    if target.kind == "pve_guest" and not user_can_access_pve(
        current_user, db, guest=_guest_key(target)
    ):
        raise HTTPException(status_code=403, detail="无权访问 PVE 虚拟机容器")
    if not target.username or (not target.password and not target.ssh_key):
        raise HTTPException(status_code=400, detail="目标未绑定有效凭据")

    try:
        code, out, err = fetch_container_logs(target, name, tail)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"获取日志失败: {exc}") from exc

    text = out or err or ""
    if code != 0 and not text.strip():
        raise HTTPException(status_code=502, detail=f"获取日志失败(exit={code})")
    return {"container": name, "tail": tail, "exit_code": code, "log": text}
