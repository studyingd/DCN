"""业务监控 API —— 业务/接口 CRUD + 关联管理 + 健康聚合总览。

业务下的"服务器"统一包含机房设备与 PVE 虚拟机/容器:设备健康取自 monitor 的
实时状态，虚拟机健康取自 pve_guest_status 的进程内快照(后台循环刷新，读取方
只做内存查询);接口健康取自 interface_probes 最新探测。
业务健康 = 全部下属组件正常 → healthy;全异常 → down;部分 → degraded;无组件 → unknown。
"""

import json
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.business import (
    Business,
    BusinessInterface,
    BusinessPveGuest,
    BusinessServer,
)
from app.models.device import OPS_TARGET_TYPES, Device
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.models.service_interface import InterfaceProbe, ServiceInterface
from app.models.user import User
from app.schemas.business import (
    BusinessBrief,
    BusinessCreate,
    BusinessDetail,
    BusinessHealthItem,
    BusinessUpdate,
    IdBatch,
    InterfaceCreate,
    InterfaceInBusiness,
    InterfaceUpdate,
    PveGuestBatch,
    PveGuestCandidate,
    PveGuestRef,
    ServerInBusiness,
)
from app.services.crypto import encrypt
from app.services.interface_prober import trigger_immediate_probe
from app.services.monitor import get_latest_statuses
from app.services.permissions import (
    get_user_device_ids,
    get_user_pve_guest_keys,
    require_permission,
    user_can_access_device,
    user_can_access_pve,
    user_has_permission,
)
from app.services.pve_guest_candidates import collect_pve_guest_candidates
from app.services.pve_guest_status import (
    ensure_fresh,
    get_connection_state,
    get_guest_snapshot,
    get_guests_by_connection,
    guest_link_state,
    judged_guest_health,
)
from app.utils import apply_update

router = APIRouter(tags=["business"])


def _health(
    server_total: int,
    server_online: int,
    interface_total: int,
    interface_up: int,
) -> str:
    """server_* 已合并 PVE 虚拟机(状态未知的虚拟机不计入)。"""
    total = server_total + interface_total
    if total == 0:
        return "unknown"
    up = server_online + interface_up
    if up == total:
        return "healthy"
    if up == 0:
        return "down"
    return "degraded"


def _headers(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _interface_response(
    interface: ServiceInterface,
    probe: InterfaceProbe | None,
    *,
    expose_sensitive: bool = False,
) -> dict:
    return {
        "interface_id": interface.id,
        "name": interface.name,
        "url": interface.url,
        "method": interface.method,
        "expected_status": interface.expected_status,
        "timeout": interface.timeout,
        "enabled": interface.enabled,
        "request_headers": _headers(interface.request_headers_json)
        if expose_sensitive
        else {},
        "auth_type": interface.auth_type or "none",
        "auth_username": interface.auth_username,
        "has_auth_secret": bool(
            interface.auth_password_enc or interface.auth_token_enc
        ),
        "request_body": interface.request_body if expose_sensitive else None,
        "response_contains": interface.response_contains if expose_sensitive else None,
        "description": interface.description,
        "up": probe.up if probe else 0,
        "status_code": probe.status_code if probe else None,
        "latency_ms": probe.latency_ms if probe else None,
        "error": probe.error if probe else "尚未完成首次探测",
        "checked_at": probe.checked_at.isoformat()
        if probe and probe.checked_at
        else None,
    }


def _interface_values(
    body: InterfaceCreate | InterfaceUpdate, existing: ServiceInterface | None = None
) -> dict:
    data = body.model_dump(exclude_unset=True)
    headers = data.pop("request_headers", None)
    if headers is not None:
        data["request_headers_json"] = json.dumps(headers, ensure_ascii=False)
    auth_password = data.pop("auth_password", None)
    auth_token = data.pop("auth_token", None)
    if auth_password is not None:
        data["auth_password_enc"] = encrypt(auth_password) if auth_password else None
    if auth_token is not None:
        data["auth_token_enc"] = encrypt(auth_token) if auth_token else None
    if data.get("auth_type") == "basic":
        data["auth_token_enc"] = None
    elif data.get("auth_type") == "bearer":
        data["auth_password_enc"] = None
    if data.get("auth_type") == "none":
        data["auth_password_enc"] = None
        data["auth_token_enc"] = None
        data["auth_username"] = None
    return data


def _guest_server_items(
    db: Session,
    business_id: int,
    *,
    include_pve: bool,
    allowed_guests: set[tuple[int, str, int]] | None = None,
) -> list[ServerInBusiness]:
    """业务关联的 PVE 虚拟机/容器 + 内存快照状态，包装成统一的服务器条目。

    ``include_pve=False``(当前用户无 PVE 权限)时返回空列表，避免把虚拟化资源
    暴露给只有设备权限的用户。``allowed_guests=None`` 表示虚拟机范围不受限，
    否则只保留角色白名单内的 guest。
    """
    if not include_pve:
        return []
    rows = (
        db.query(BusinessPveGuest, PveConnection, PveGuestBinding)
        .join(PveConnection, PveConnection.id == BusinessPveGuest.connection_id)
        .outerjoin(
            PveGuestBinding,
            and_(
                PveGuestBinding.connection_id == BusinessPveGuest.connection_id,
                PveGuestBinding.guest_type == BusinessPveGuest.guest_type,
                PveGuestBinding.vmid == BusinessPveGuest.vmid,
            ),
        )
        .filter(BusinessPveGuest.business_id == business_id)
        .order_by(BusinessPveGuest.connection_id, BusinessPveGuest.vmid)
        .all()
    )
    if not rows:
        return []
    # 快照由后台 30s 循环维护，请求路径只读内存(此前 GET 里会补采:单点部署
    # 下循环必然在跑，补采只在循环卡死时把请求拖满 6s×连接数)。展示名同步
    # 同样移入后台循环(_sync_business_guest_names_sync)，GET 保持只读。
    items: list[ServerInBusiness] = []
    for link, conn, binding in rows:
        if (
            allowed_guests is not None
            and (
                link.connection_id,
                link.guest_type,
                link.vmid,
            )
            not in allowed_guests
        ):
            continue
        snap = get_guest_snapshot(conn.id, link.vmid) or {}
        if snap.get("guest_type") and snap["guest_type"] != link.guest_type:
            snap = {}
        reachable = 1 if (get_connection_state(conn.id) or {}).get("reachable") else 0
        status = str(snap.get("status") or "unknown")
        live_name = str(snap.get("name") or "").strip()
        items.append(
            ServerInBusiness(
                kind="pve",
                link_id=link.id,
                connection_id=conn.id,
                connection_name=conn.name,
                guest_type=link.guest_type,
                vmid=link.vmid,
                name=live_name or link.guest_name or f"VM {link.vmid}",
                node=snap.get("node"),
                ip_address=binding.ip_address if binding else None,
                os_system=binding.os_system if binding else None,
                status=status,
                online=1 if status == "running" else 0,
                cpu=snap.get("cpu"),
                mem=snap.get("mem") or None,
                maxmem=snap.get("maxmem") or None,
                reachable=reachable,
            )
        )
    return items


def _business_detail_payload(
    db: Session, business: Business, current_user: User
) -> BusinessDetail:
    # 先取出标量：link/unlink 调用方在 commit 后传入 business，ORM 属性已
    # 过期；统一取快照避免构造响应中途触发逐属性重载。
    business_id = business.id
    business_name = business.name
    business_description = business.description
    business_created_at = business.created_at
    allowed_ids = get_user_device_ids(current_user, db)
    expose_sensitive = user_has_permission(current_user, "device:manage", db)
    include_pve = user_can_access_pve(current_user, db)
    allowed_guests = get_user_pve_guest_keys(current_user, db) if include_pve else None

    srv_query = (
        db.query(Device)
        .join(BusinessServer, BusinessServer.device_id == Device.id)
        .filter(BusinessServer.business_id == business_id)
        .order_by(Device.id)
    )
    if allowed_ids is not None:
        srv_query = srv_query.filter(Device.id.in_(allowed_ids))
    servers = srv_query.all()
    statuses = get_latest_statuses()
    device_items = [
        ServerInBusiness(
            kind="device",
            device_id=device.id,
            name=device.name,
            ip_address=device.ip_address,
            status=statuses.get(device.id, device.status or "offline"),
            online=1
            if statuses.get(device.id, device.status or "offline") == "online"
            else 0,
        )
        for device in servers
    ]
    device_online = sum(item.online for item in device_items)

    iface_rows = (
        db.query(ServiceInterface, InterfaceProbe)
        .join(
            BusinessInterface,
            BusinessInterface.interface_id == ServiceInterface.id,
        )
        .outerjoin(InterfaceProbe, InterfaceProbe.interface_id == ServiceInterface.id)
        .filter(BusinessInterface.business_id == business_id)
        .all()
    )
    iface_items = []
    for interface, probe in iface_rows:
        iface_items.append(
            InterfaceInBusiness(
                **_interface_response(
                    interface, probe, expose_sensitive=expose_sensitive
                )
            )
        )
    enabled_iface_items = [item for item in iface_items if item.enabled == 1]
    interface_up = sum(1 for item in enabled_iface_items if item.up == 1)

    # 虚拟机与设备合并成同一个"服务器"列表返回，界面不再区分两类资源。
    guest_items = _guest_server_items(
        db, business_id, include_pve=include_pve, allowed_guests=allowed_guests
    )
    health_guest_total, health_guest_online = judged_guest_health(
        [(item.online, item.reachable) for item in guest_items]
    )
    server_items = [*device_items, *guest_items]

    return BusinessDetail(
        id=business_id,
        name=business_name,
        description=business_description,
        created_at=business_created_at,
        servers=server_items,
        interfaces=iface_items,
        server_total=len(server_items),
        server_online=device_online + sum(item.online for item in guest_items),
        interface_total=len(enabled_iface_items),
        interface_up=interface_up,
        health=_health(
            len(device_items) + health_guest_total,
            device_online + health_guest_online,
            len(enabled_iface_items),
            interface_up,
        ),
    )


# ══════════════════════════════════════════════════════════════════
# 业务 CRUD
# ══════════════════════════════════════════════════════════════════


@router.get("/api/businesses", response_model=list[BusinessHealthItem])
def list_businesses(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """业务列表 + 各自聚合健康状态。"""
    allowed_ids = get_user_device_ids(current_user, db)
    can_access_pve = user_can_access_pve(current_user, db)
    businesses = db.query(Business).order_by(Business.id).all()
    if not businesses:
        return []

    business_ids = [business.id for business in businesses]
    device_query = (
        db.query(BusinessServer.business_id, Device.id, Device.status)
        .join(Device, Device.id == BusinessServer.device_id)
        .filter(BusinessServer.business_id.in_(business_ids))
    )
    if allowed_ids is not None:
        device_query = device_query.filter(Device.id.in_(allowed_ids))

    devices_by_business: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for business_id, device_id, device_status in device_query.all():
        devices_by_business[business_id].append((device_id, device_status))

    interfaces_by_business: dict[int, list[int]] = defaultdict(list)
    for business_id, _interface_id, up in (
        db.query(
            BusinessInterface.business_id,
            BusinessInterface.interface_id,
            InterfaceProbe.up,
        )
        .join(ServiceInterface, ServiceInterface.id == BusinessInterface.interface_id)
        .outerjoin(
            InterfaceProbe,
            InterfaceProbe.interface_id == BusinessInterface.interface_id,
        )
        .filter(
            BusinessInterface.business_id.in_(business_ids),
            ServiceInterface.enabled == 1,
        )
        .all()
    ):
        # 查询已 join 启用接口；没有探测结果的启用接口按异常计入。
        interfaces_by_business[business_id].append(up if up is not None else 0)

    # 列表接口被前端轮询，虚拟机状态只读内存快照，绝不同步调用 PVE。
    guests_by_business: dict[int, list[BusinessPveGuest]] = defaultdict(list)
    if can_access_pve:
        allowed_guests = get_user_pve_guest_keys(current_user, db)
        for link in (
            db.query(BusinessPveGuest)
            .filter(BusinessPveGuest.business_id.in_(business_ids))
            .all()
        ):
            if (
                allowed_guests is not None
                and (
                    link.connection_id,
                    link.guest_type,
                    link.vmid,
                )
                not in allowed_guests
            ):
                continue
            guests_by_business[link.business_id].append(link)

    statuses = get_latest_statuses()
    result = []
    for business in businesses:
        guest_rows = guests_by_business[business.id]
        # 设备范围受限的用户看不到任何授权设备时，若该业务挂了虚拟机且用户有
        # PVE 权限，仍然保留展示，否则纯虚拟化业务会被整体隐藏。
        if (
            allowed_ids is not None
            and not devices_by_business.get(business.id)
            and not guest_rows
        ):
            continue
        device_rows = devices_by_business[business.id]
        interface_rows = interfaces_by_business[business.id]
        device_online = sum(
            1
            for device_id, device_status in device_rows
            if statuses.get(device_id, device_status) == "online"
        )
        interface_up = sum(1 for up in interface_rows if up == 1)
        guest_states = [
            guest_link_state(link.connection_id, link.guest_type, link.vmid)
            for link in guest_rows
        ]
        guest_online = sum(1 for online, _reachable in guest_states if online)
        health_guest_total, health_guest_online = judged_guest_health(guest_states)
        result.append(
            BusinessHealthItem(
                id=business.id,
                name=business.name,
                description=business.description,
                created_at=business.created_at,
                server_total=len(device_rows) + len(guest_states),
                server_online=device_online + guest_online,
                interface_total=len(interface_rows),
                interface_up=interface_up,
                health=_health(
                    len(device_rows) + health_guest_total,
                    device_online + health_guest_online,
                    len(interface_rows),
                    interface_up,
                ),
            )
        )
    return result


# 必须声明在 "/api/businesses/{business_id}" 之前，否则会被 int 路径参数吞掉。
@router.get(
    "/api/businesses/pve-guest-candidates",
    response_model=list[PveGuestCandidate],
)
def list_pve_guest_candidates(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    """可关联到业务的 PVE 虚拟机/容器候选项。

    只在"添加虚拟机"弹窗打开时调用；数据取自内存快照，仅在快照明显过期时补采。
    """
    if not user_can_access_pve(current_user, db):
        return []
    return [
        PveGuestCandidate(**item)
        for item in collect_pve_guest_candidates(
            db, get_user_pve_guest_keys(current_user, db)
        )
    ]


@router.get("/api/businesses/{business_id}", response_model=BusinessDetail)
def get_business(
    business_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:view")),
):
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="业务不存在")

    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        visible = (
            db.query(BusinessServer.device_id)
            .filter(
                BusinessServer.business_id == business_id,
                BusinessServer.device_id.in_(allowed_ids),
            )
            .first()
        )
        # 纯虚拟化业务没有授权服务器，有 PVE 权限且至少一台 guest 在授权范围
        # 内时同样可见。
        if visible is None:
            allowed_guests = (
                get_user_pve_guest_keys(current_user, db)
                if user_can_access_pve(current_user, db)
                else set()
            )
            guest_query = db.query(BusinessPveGuest.id).filter(
                BusinessPveGuest.business_id == business_id
            )
            if allowed_guests is None:
                has_guest = guest_query.first() is not None
            elif not allowed_guests:
                has_guest = False
            else:
                has_guest = (
                    guest_query.filter(
                        or_(
                            and_(
                                BusinessPveGuest.connection_id == conn_id,
                                BusinessPveGuest.guest_type == gtype,
                                BusinessPveGuest.vmid == vmid,
                            )
                            for conn_id, gtype, vmid in allowed_guests
                        )
                    ).first()
                    is not None
                )
            if not has_guest:
                raise HTTPException(status_code=404, detail="业务不存在")
    return _business_detail_payload(db, business, current_user)


@router.post("/api/businesses", response_model=BusinessBrief, status_code=201)
def create_business(
    body: BusinessCreate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    if db.query(Business).filter(Business.name == body.name).first():
        raise HTTPException(status_code=400, detail="业务名称已存在")
    business = Business(name=body.name, description=body.description)
    db.add(business)
    db.commit()
    db.refresh(business)
    return BusinessBrief.model_validate(business)


@router.put("/api/businesses/{business_id}", response_model=BusinessBrief)
def update_business(
    business_id: int,
    body: BusinessUpdate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="业务不存在")
    apply_update(business, body.model_dump(exclude_unset=True), ["name", "description"])
    db.commit()
    db.refresh(business)
    return BusinessBrief.model_validate(business)


@router.delete("/api/businesses/{business_id}")
def delete_business(
    business_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="业务不存在")
    db.delete(business)
    db.commit()
    return {"message": "业务已删除"}


# ══════════════════════════════════════════════════════════════════
# 接口 CRUD
#
# 接口生命周期从属业务：创建走「新建并关联」(POST /businesses/{id}/interfaces)，
# 编辑/删除走 /api/service-interfaces/{id}。
# 曾存在独立的「接口管理页」端点(GET /service-interfaces 列表、
# POST /businesses/{id}/interfaces/{iid} 单个关联、batch 关联、单个关联设备)——
# 前端从未接入(接口库跨业务复用的 UI 没做)，2026-09-17 拍板删除，勿加回。
# ══════════════════════════════════════════════════════════════════


@router.post("/api/service-interfaces", status_code=201)
def create_interface(
    body: InterfaceCreate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    iface = ServiceInterface(**_interface_values(body))
    db.add(iface)
    db.commit()
    db.refresh(iface)
    # 立即补一次首探:不等下一个 60s 周期,消除「尚未完成首次探测」空窗期
    # (期间按异常计入业务健康,卡片会先 degraded 再恢复)。
    trigger_immediate_probe([iface.id])
    return {"message": "接口已创建", "id": iface.id}


@router.put("/api/service-interfaces/{interface_id}")
def update_interface(
    interface_id: int,
    body: InterfaceUpdate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    iface = (
        db.query(ServiceInterface).filter(ServiceInterface.id == interface_id).first()
    )
    if not iface:
        raise HTTPException(status_code=404, detail="接口不存在")
    apply_update(
        iface,
        _interface_values(body, iface),
        [
            "name",
            "url",
            "method",
            "expected_status",
            "timeout",
            "enabled",
            "description",
            "request_headers_json",
            "auth_type",
            "auth_username",
            "auth_password_enc",
            "auth_token_enc",
            "request_body",
            "response_contains",
        ],
    )
    db.commit()
    db.refresh(iface)
    # 配置(URL/状态码/认证等)变了立即重探,启用切换同理(停用接口不会被探)。
    trigger_immediate_probe([iface.id])
    return {"message": "接口已更新"}


@router.delete("/api/service-interfaces/{interface_id}")
def delete_interface(
    interface_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:manage")),
):
    iface = (
        db.query(ServiceInterface).filter(ServiceInterface.id == interface_id).first()
    )
    if not iface:
        raise HTTPException(status_code=404, detail="接口不存在")
    db.delete(iface)
    db.commit()
    return {"message": "接口已删除"}


# ══════════════════════════════════════════════════════════════════
# 业务关联管理(服务器 / 虚拟机 / 接口)
# ══════════════════════════════════════════════════════════════════


@router.post(
    "/api/businesses/{business_id}/servers/batch", response_model=BusinessDetail
)
def link_servers_batch(
    business_id: int,
    body: IdBatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="业务不存在")
    devices = (
        db.query(Device)
        .filter(Device.id.in_(body.ids), Device.type.in_(OPS_TARGET_TYPES))
        .all()
    )
    valid_ids = {device.id for device in devices}
    if valid_ids != set(body.ids):
        raise HTTPException(status_code=400, detail="包含不存在或不可纳管的设备")
    if any(
        not user_can_access_device(current_user, device_id, db)
        for device_id in body.ids
    ):
        raise HTTPException(status_code=403, detail="无权关联所选设备")
    existing_ids = {
        device_id
        for (device_id,) in (
            db.query(BusinessServer.device_id)
            .filter(
                BusinessServer.business_id == business_id,
                BusinessServer.device_id.in_(body.ids),
            )
            .all()
        )
    }
    db.add_all(
        BusinessServer(business_id=business_id, device_id=device_id)
        for device_id in body.ids
        if device_id not in existing_ids
    )
    db.commit()
    return _business_detail_payload(db, business, current_user)


@router.delete(
    "/api/businesses/{business_id}/servers/{device_id}", response_model=BusinessDetail
)
def unlink_server(
    business_id: int,
    device_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="业务不存在")
    row = (
        db.query(BusinessServer)
        .filter(
            BusinessServer.business_id == business_id,
            BusinessServer.device_id == device_id,
        )
        .first()
    )
    if row:
        db.delete(row)
        db.commit()
    return _business_detail_payload(db, business, current_user)


def _guest_display_name(item: PveGuestRef) -> str | None:
    """关联时记录的展示名快照：优先 PVE 实时名称，其次前端传入的名称。"""
    snap = get_guest_snapshot(item.connection_id, item.vmid)
    if snap and snap.get("guest_type") == item.guest_type and snap.get("name"):
        return str(snap["name"])[:255]
    return (item.name or "").strip()[:255] or None


@router.post(
    "/api/businesses/{business_id}/guests/batch", response_model=BusinessDetail
)
def link_guests_batch(
    business_id: int,
    body: PveGuestBatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="业务不存在")
    if not user_can_access_pve(current_user, db, manage=True):
        raise HTTPException(status_code=403, detail="无权管理 PVE 平台资源")
    # 逐项校验虚拟机级 ACL，避免“有 pve:manage 就能挂任意 guest”。
    allowed_guests = get_user_pve_guest_keys(current_user, db)
    if allowed_guests is not None:
        denied = [
            f"{item.connection_id}:{item.guest_type}:{item.vmid}"
            for item in body.items
            if (item.connection_id, item.guest_type, item.vmid) not in allowed_guests
        ]
        if denied:
            raise HTTPException(
                status_code=403,
                detail=f"无权关联以下虚拟机: {'、'.join(denied)}",
            )

    conn_ids = sorted({item.connection_id for item in body.items})
    conns = {
        conn.id: conn
        for conn in db.query(PveConnection).filter(PveConnection.id.in_(conn_ids)).all()
    }
    if set(conns) != set(conn_ids):
        raise HTTPException(status_code=400, detail="包含不存在的 PVE 连接")
    disabled = [
        conns[conn_id].name for conn_id in conn_ids if not conns[conn_id].enabled
    ]
    if disabled:
        raise HTTPException(
            status_code=400, detail=f"PVE 连接已停用: {'、'.join(disabled)}"
        )

    # 连接可达时核对 guest 是否真实存在，避免把已删除的虚机长期挂在业务上;
    # 连接不可达时无法核对，放行并沿用前端传入的名称快照。
    ensure_fresh(db, conn_ids)
    missing: list[str] = []
    for conn_id in conn_ids:
        if not (get_connection_state(conn_id) or {}).get("reachable"):
            continue
        available = get_guests_by_connection(conn_id)
        for item in body.items:
            if item.connection_id != conn_id:
                continue
            snap = available.get(item.vmid)
            if not snap or snap.get("guest_type") != item.guest_type:
                missing.append(f"{item.guest_type}/{item.vmid}")
    if missing:
        raise HTTPException(
            status_code=400, detail=f"PVE 平台上不存在这些虚拟机: {'、'.join(missing)}"
        )

    existing = {
        (row.connection_id, row.guest_type, row.vmid): row
        for row in db.query(BusinessPveGuest)
        .filter(BusinessPveGuest.business_id == business_id)
        .all()
    }
    for item in body.items:
        name = _guest_display_name(item)
        row = existing.get((item.connection_id, item.guest_type, item.vmid))
        if row:
            if name and row.guest_name != name:
                row.guest_name = name
            continue
        db.add(
            BusinessPveGuest(
                business_id=business_id,
                connection_id=item.connection_id,
                guest_type=item.guest_type,
                vmid=item.vmid,
                guest_name=name,
            )
        )
    db.commit()
    return _business_detail_payload(db, business, current_user)


@router.delete(
    "/api/businesses/{business_id}/guests/{link_id}", response_model=BusinessDetail
)
def unlink_guest(
    business_id: int,
    link_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="业务不存在")
    if not user_can_access_pve(current_user, db, manage=True):
        raise HTTPException(status_code=403, detail="无权管理 PVE 平台资源")
    row = (
        db.query(BusinessPveGuest)
        .filter(
            BusinessPveGuest.id == link_id,
            BusinessPveGuest.business_id == business_id,
        )
        .first()
    )
    if row:
        if not user_can_access_pve(
            current_user,
            db,
            manage=True,
            guest=(row.connection_id, row.guest_type, row.vmid),
        ):
            raise HTTPException(status_code=403, detail="无权管理该虚拟机")
        db.delete(row)
        db.commit()
    return _business_detail_payload(db, business, current_user)


@router.post(
    "/api/businesses/{business_id}/interfaces",
    response_model=BusinessDetail,
    status_code=201,
)
def create_and_link_interface(
    business_id: int,
    body: InterfaceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="业务不存在")
    interface = ServiceInterface(**_interface_values(body))
    db.add(interface)
    db.flush()
    db.add(BusinessInterface(business_id=business_id, interface_id=interface.id))
    db.commit()
    # 立即补一次首探(同 create_interface 的理由)。fire-and-forget:返回的
    # 详情里可能仍是「尚未完成首次探测」,新结果由前端 30s 详情轮询带出。
    trigger_immediate_probe([interface.id])
    return _business_detail_payload(db, business, current_user)


@router.delete(
    "/api/businesses/{business_id}/interfaces/{interface_id}",
    response_model=BusinessDetail,
)
def unlink_interface(
    business_id: int,
    interface_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:manage")),
):
    business = db.query(Business).filter(Business.id == business_id).first()
    if not business:
        raise HTTPException(status_code=404, detail="业务不存在")
    row = (
        db.query(BusinessInterface)
        .filter(
            BusinessInterface.business_id == business_id,
            BusinessInterface.interface_id == interface_id,
        )
        .first()
    )
    if row:
        db.delete(row)
        db.commit()
    return _business_detail_payload(db, business, current_user)
