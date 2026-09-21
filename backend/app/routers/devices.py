from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.device import Device
from app.models.rack import Rack
from app.models.user import User
from app.schemas.device import (
    DeviceCreate,
    DeviceCredentialInput,
    DeviceResponse,
    DeviceUpdate,
    OSDetectRequest,
    OSDetectResponse,
)
from app.services.auth import get_current_user
from app.services.crypto import decrypt, encrypt
from app.services.device_credentials import resolve_credentials
from app.services.metrics_collector import get_latest_metrics
from app.services.os_detect import detect_os
from app.services.permissions import (
    get_user_device_ids,
    require_permission,
    user_can_access_device,
    user_has_permission,
)
from app.services.power import reboot_device, shutdown_device
from app.services.winrm_setup import build_winrm_setup_script
from app.utils import apply_update

router = APIRouter(tags=["devices"])
RACK_CAPACITY_U = 24
DEVICE_SIZE_U = 2


class DevicePositionItem(BaseModel):
    device_id: int
    position_u: int


class DeviceReorderRequest(BaseModel):
    positions: list[DevicePositionItem]


def _assert_ip_unique(
    db: Session, ip: str | None, exclude_device_id: int | None = None
) -> None:
    """同 IP 双设备是隐雷:monitor/终端/自动化按 device_id 寻址虽不直接炸,
    但排查时看错机器、流量混看。创建/更新时查重(空 IP 放行)。"""
    if not ip:
        return
    query = db.query(Device.id).filter(Device.ip_address == ip)
    if exclude_device_id is not None:
        query = query.filter(Device.id != exclude_device_id)
    existing = query.first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"IP 地址 {ip} 已被设备 #{existing[0]} 使用，同一 IP 只能绑定一台设备",
        )


@router.get("/api/devices/winrm-setup-script", response_class=PlainTextResponse)
def download_winrm_setup_script(
    target_ip: str,
    winrm_port: int = 5985,
    _u: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a deployment-specific, DCN-source-scoped WinRM script."""
    # Device managers and PVE/system managers may generate the script. No
    # target credentials are accepted by this endpoint.
    if not (
        user_has_permission(_u, "device:manage", db)
        or user_has_permission(_u, "settings:manage", db)
    ):
        raise HTTPException(status_code=403, detail="权限不足")
    try:
        script, source_ip = build_winrm_setup_script(target_ip, winrm_port)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Windows PowerShell 5 defaults to the system ANSI code page for .ps1
    # files without a BOM. Emit UTF-8 with BOM so Chinese comments and quoted
    # arguments (including @{...}) are parsed correctly after download.
    return PlainTextResponse(
        script.encode("utf-8-sig"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="dcn-enable-winrm.ps1"',
            "X-DCN-WinRM-Source-IP": source_ip,
            "X-DCN-WinRM-Script-Version": "6",
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


def _find_device_position(
    rack: Rack,
    preferred: int | None = None,
    exclude_device_id: int | None = None,
) -> int:
    occupied: set[int] = set()
    for item in rack.devices or []:
        if item.id == exclude_device_id:
            continue
        start = item.position_u
        if start is None:
            continue
        start = min(RACK_CAPACITY_U - 1, max(1, start))
        start = start if start % 2 == 1 else start - 1
        for offset in range(DEVICE_SIZE_U):
            occupied.add(start + offset)

    candidates = list(range(1, RACK_CAPACITY_U, DEVICE_SIZE_U))
    if preferred is not None:
        snapped = min(RACK_CAPACITY_U - 1, max(1, preferred))
        snapped = snapped if snapped % 2 == 1 else snapped - 1
        candidates = [snapped, *[pos for pos in candidates if pos != snapped]]

    for position in candidates:
        if all(position + offset not in occupied for offset in range(DEVICE_SIZE_U)):
            return position
    raise HTTPException(status_code=422, detail="当前 24U 机柜已满，无法继续添加设备")


def _apply_remote_credentials(
    device: Device, remote: DeviceCredentialInput | None
) -> None:
    if remote is None:
        device.remote_username = None
        device.remote_password_enc = None
        device.remote_ssh_key_enc = None
        return
    if remote.username is not None:
        device.remote_username = remote.username
    if remote.password is not None:
        device.remote_password_enc = (
            encrypt(remote.password) if remote.password else None
        )
    if remote.ssh_key is not None:
        device.remote_ssh_key_enc = encrypt(remote.ssh_key) if remote.ssh_key else None
    if not device.remote_username:
        raise HTTPException(status_code=422, detail="请输入远程连接用户名")
    if not device.remote_password_enc and not device.remote_ssh_key_enc:
        raise HTTPException(status_code=422, detail="请输入远程连接密码或 SSH 私钥")


# Compatibility shim for older integrations; new code stores credentials on
# the device itself and no longer creates/reuses global credential records.
def _resolve_update_credential(db, device, device_name, remote, current_user):
    _apply_remote_credentials(device, remote)
    return None


@router.get("/api/racks/{rack_id}/devices", response_model=list[DeviceResponse])
def list_devices(
    rack_id: int,
    limit: int | None = None,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    rack = db.query(Rack).filter(Rack.id == rack_id).first()
    if not rack:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机架不存在")
    query = db.query(Device).filter(Device.rack_id == rack_id)
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        query = query.filter(Device.id.in_(allowed_ids))
    # 可选分页:不传 limit 时保持旧行为全量返回(前端多处依赖数组契约);
    # 传了则按 id 排序后截断,避免大机架一次拉几百台。
    query = query.order_by(Device.id)
    if limit is not None:
        query = query.offset(offset).limit(max(1, min(limit, 500)))
    devices = query.all()
    # Windows 通道连通信号(同 automation /devices 的 metrics_failed):
    # 告警中心等以设备列表为候选的选择器据此置灰 WinRM 确认不通的目标。
    metrics_latest = get_latest_metrics()
    return [
        DeviceResponse.model_validate(d).model_copy(
            update={
                "metrics_failed": bool(
                    d.is_windows
                    and metrics_latest.get(d.id, {}).get("available") is False
                )
            }
        )
        for d in devices
    ]


@router.post(
    "/api/racks/{rack_id}/devices",
    response_model=DeviceResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_device(
    rack_id: int,
    body: DeviceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    rack = db.query(Rack).filter(Rack.id == rack_id).first()
    if not rack:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机架不存在")
    _assert_ip_unique(db, body.ip_address)
    position_u = (
        _find_device_position(rack, body.position_u) if rack.type == "cabinet" else None
    )
    device = Device(
        rack_id=rack_id,
        name=body.name,
        type=body.type,
        ip_address=body.ip_address,
        os_system=body.os_system,
        position_u=position_u,
        size_u=DEVICE_SIZE_U if rack.type == "cabinet" else None,
        ssh_port=body.ssh_port,
        rdp_port=body.rdp_port,
        winrm_port=body.winrm_port,
    )
    if body.remote_credential:
        _resolve_update_credential(
            db, device, body.name, body.remote_credential, current_user
        )
    db.add(device)
    db.commit()
    db.refresh(device)
    return DeviceResponse.model_validate(device)


@router.get("/api/devices/{device_id}", response_model=DeviceResponse)
def get_device(
    device_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")
    if not user_can_access_device(current_user, device_id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该设备"
        )
    return DeviceResponse.model_validate(device)


@router.put("/api/racks/{rack_id}/devices/reorder")
def reorder_devices(
    rack_id: int,
    body: DeviceReorderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    rack = db.query(Rack).filter(Rack.id == rack_id).first()
    if not rack:
        raise HTTPException(status_code=404, detail="机柜不存在")
    device_map = {device.id: device for device in rack.devices or []}
    requested_ids = [item.device_id for item in body.positions]
    if len(requested_ids) != len(set(requested_ids)):
        raise HTTPException(status_code=422, detail="设备位置请求存在重复设备")
    occupied: set[int] = set()
    moving_ids = set(requested_ids)
    for device in rack.devices or []:
        if device.id in moving_ids or device.position_u is None:
            continue
        start = min(RACK_CAPACITY_U - 1, max(1, device.position_u))
        start = start if start % 2 == 1 else start - 1
        occupied.update({start, start + 1})
    for item in body.positions:
        device = device_map.get(item.device_id)
        if not device or not user_can_access_device(current_user, item.device_id, db):
            raise HTTPException(status_code=403, detail="无权移动该设备")
        position = item.position_u
        if position < 1 or position > RACK_CAPACITY_U - 1 or position % 2 == 0:
            raise HTTPException(status_code=422, detail="设备必须放置在有效的 2U 位置")
        if position in occupied or position + 1 in occupied:
            raise HTTPException(status_code=422, detail="目标位置已被占用")
        occupied.update({position, position + 1})
        device.position_u = position
        device.size_u = DEVICE_SIZE_U
    # 仅 cabinet 才回写容量;shelf 等其它类型保留原值,否则一次 reorder 就把
    # 非机柜机架的 capacity_u 覆盖成 24。
    if rack.type == "cabinet":
        rack.capacity_u = RACK_CAPACITY_U
    db.commit()
    return {"message": "设备位置已更新"}


@router.put("/api/devices/{device_id}", response_model=DeviceResponse)
def update_device(
    device_id: int,
    body: DeviceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")
    if not user_can_access_device(current_user, device_id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该设备"
        )
    _assert_ip_unique(db, body.ip_address, exclude_device_id=device_id)
    update_data = body.model_dump(exclude_unset=True)
    if device.rack and device.rack.type == "cabinet":
        # 机柜内一律按 2U 建模并吸附到奇数槽位——前端 3D 视图只认 24U/2U 网格,
        # 跨格尺寸会错位。schema 里 size_u 的 1-42 上限仅对非机柜(shelf)有意义。
        update_data["size_u"] = DEVICE_SIZE_U
        if "position_u" in update_data and update_data["position_u"] is not None:
            update_data["position_u"] = _find_device_position(
                device.rack, update_data["position_u"], exclude_device_id=device.id
            )
    remote_is_set = "remote_credential" in body.model_fields_set
    remote = body.remote_credential if remote_is_set else None
    update_data.pop("remote_credential", None)
    if remote_is_set and remote is None:
        _apply_remote_credentials(device, None)
    elif remote is not None:
        _resolve_update_credential(
            db, device, update_data.get("name", device.name), remote, current_user
        )
    apply_update(
        device,
        update_data,
        [
            "name",
            "type",
            "ip_address",
            "os_system",
            "position_u",
            "size_u",
            "ssh_port",
            "rdp_port",
            "winrm_port",
        ],
    )
    db.commit()
    db.refresh(device)
    return DeviceResponse.model_validate(device)


@router.delete("/api/devices/{device_id}")
def delete_device(
    device_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")
    if not user_can_access_device(current_user, device_id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该设备"
        )
    db.delete(device)
    db.commit()
    return {"message": "设备已删除"}


# ------------------------------------------------------------------
# OS Detection
# ------------------------------------------------------------------


@router.post("/api/devices/detect-os", response_model=OSDetectResponse)
async def detect_device_os(
    body: OSDetectRequest,
    current_user: User = Depends(require_permission("device:manage")),
    db: Session = Depends(get_db),
):
    """Detect the operating system of a device by its IP address.

    Probes SSH (22), WinRM (5985), RDP (3389), and SMB (445) ports.
    Parses the SSH banner to identify OS type and version.
    If credentials are supplied, Linux/Windows versions are probed precisely.
    """
    username = body.username
    password = body.password
    if body.device_id and not username and not password:
        device = db.query(Device).filter(Device.id == body.device_id).first()
        if device and user_can_access_device(current_user, device.id, db):
            username = device.remote_username or ""
            password = (
                decrypt(device.remote_password_enc)
                if device.remote_password_enc
                else ""
            )

    result = await detect_os(
        body.ip_address,
        username=username,
        password=password,
        winrm_port=body.winrm_port,
    )
    return OSDetectResponse(**result.to_dict())


# ------------------------------------------------------------------
# Power control
# ------------------------------------------------------------------

_POWER_ACTIONS = {"shutdown", "reboot"}


class PowerRequest(BaseModel):
    action: str  # shutdown / reboot
    username: str | None = None
    password: str | None = None


def _resolve_creds_for_power(
    device: Device, body: PowerRequest
) -> tuple[str, str, str]:
    """Resolve SSH credentials from request body or device's bound credential."""
    username, password, ssh_key = resolve_credentials(
        device, body.username, body.password
    )
    return username or "root", password, ssh_key


@router.post("/api/devices/{device_id}/power")
def power_control(
    device_id: int,
    body: PowerRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:remote")),
):
    if body.action not in _POWER_ACTIONS:
        raise HTTPException(status_code=400, detail=f"不支持的操作: {body.action}")

    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    if not user_can_access_device(current_user, device_id, db):
        raise HTTPException(status_code=403, detail="无权访问该设备")
    username, password, ssh_key = _resolve_creds_for_power(device, body)

    if body.action == "shutdown":
        return shutdown_device(device, username, password, private_key=ssh_key or None)
    if body.action == "reboot":
        return reboot_device(device, username, password, private_key=ssh_key or None)
