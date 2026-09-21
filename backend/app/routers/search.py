"""顶栏全局搜索端点。

顶栏搜索框的统一入口：一次请求同时搜物理设备(名称/IP)与 PVE 虚拟机
(名称/绑定 IP/最近已知 IP)。设备与虚机分属两套数据域(rooms 树 vs PVE
快照)，历史上顶栏只遍历 rooms 树，虚机永远搜不到——此端点即为其补全。

权限按域各自收口：设备结果要求 ``device:view``，虚机结果要求 ``pve:view``，
互不牵连(纯虚拟化角色照样能搜虚机)。资源 ACL 与列表页同源
(``get_user_device_ids`` / ``get_user_pve_guest_keys``)。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.device import Device
from app.models.rack import Rack
from app.models.room import Room
from app.models.user import User
from app.schemas.search import SearchDeviceItem, SearchGuestItem, SearchResponse
from app.services.auth import get_current_user
from app.services.permissions import (
    get_user_device_ids,
    get_user_pve_guest_keys,
    user_can_access_pve,
    user_has_permission,
)
from app.services.pve_guest_candidates import collect_pve_guest_candidates
from app.services.pve_guest_status import get_last_known_ip

router = APIRouter(prefix="/api/search", tags=["search"])

# 与前端顶栏结果列表展示上限一致(防 1 字符搜索铺满整屏)
_MAX_RESULTS = 20


def _search_devices(db: Session, current_user: User, q: str) -> list[SearchDeviceItem]:
    if not user_has_permission(current_user, "device:view", db):
        return []
    query = (
        db.query(Device, Rack, Room)
        .join(Rack, Device.rack_id == Rack.id)
        .join(Room, Rack.room_id == Room.id)
        .filter(
            or_(
                Device.name.ilike(f"%{q}%"),
                Device.ip_address.ilike(f"%{q}%"),
            )
        )
        .order_by(Device.id)
    )
    allowed = get_user_device_ids(current_user, db)
    if allowed is not None:
        query = query.filter(Device.id.in_(allowed))
    items: list[SearchDeviceItem] = []
    for device, rack, room in query.limit(_MAX_RESULTS):
        items.append(
            SearchDeviceItem(
                id=device.id,
                name=device.name,
                type=device.type,
                status=device.status or "offline",
                ip_address=device.ip_address,
                room_id=room.id,
                room_name=room.name,
                rack_id=rack.id,
                rack_name=rack.name,
            )
        )
    return items


def _search_guests(db: Session, current_user: User, q: str) -> list[SearchGuestItem]:
    if not user_has_permission(current_user, "pve:view", db):
        return []
    if not user_can_access_pve(current_user, db):
        return []
    allowed_keys = get_user_pve_guest_keys(current_user, db)
    items: list[SearchGuestItem] = []
    for candidate in collect_pve_guest_candidates(db, allowed_keys):
        name = str(candidate.get("name") or "")
        binding_ip = str(candidate.get("ip_address") or "")
        last_ip = str(
            get_last_known_ip(candidate["connection_id"], candidate["vmid"]) or ""
        )
        if (
            q not in name.lower()
            and q not in binding_ip.lower()
            and q not in last_ip.lower()
        ):
            continue
        items.append(
            SearchGuestItem(
                connection_id=candidate["connection_id"],
                connection_name=candidate["connection_name"],
                guest_type=candidate["guest_type"],
                vmid=candidate["vmid"],
                name=name,
                node=candidate.get("node"),
                status=str(candidate.get("status") or "unknown"),
                ip_address=binding_ip or None,
                last_known_ip=last_ip or None,
            )
        )
        if len(items) >= _MAX_RESULTS:
            break
    return items


@router.get("", response_model=SearchResponse)
def global_search(
    q: str = Query(
        ..., min_length=1, max_length=64, description="关键词(设备/虚机名称或 IP)"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    keyword = q.strip().lower()
    if not keyword:
        return SearchResponse(devices=[], guests=[])
    return SearchResponse(
        devices=_search_devices(db, current_user, keyword),
        guests=_search_guests(db, current_user, keyword),
    )
