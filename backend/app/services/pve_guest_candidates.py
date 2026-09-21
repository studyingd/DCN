"""PVE 虚拟机候选项收集（业务关联 / 角色授权共用）。

数据取自内存快照，仅在快照明显过期时补采，避免弹窗打开时同步调用 PVE API。
``allowed_guests=None`` 表示不做角色 ACL 过滤——角色编辑器需要看到全部 guest
才能授权，因此由调用方决定是否传入白名单。
"""

from sqlalchemy.orm import Session

from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.services.pve_guest_status import (
    ensure_fresh,
    get_connection_state,
    get_guests_by_connection,
)

GuestKey = tuple[int, str, int]


def collect_pve_guest_candidates(
    db: Session, allowed_guests: set[GuestKey] | None = None
) -> list[dict]:
    conns = (
        db.query(PveConnection)
        .filter(PveConnection.enabled == 1)
        .order_by(PveConnection.id)
        .all()
    )
    if not conns:
        return []
    ensure_fresh(db, [conn.id for conn in conns])

    bindings: dict[GuestKey, PveGuestBinding] = {}
    for binding in (
        db.query(PveGuestBinding)
        .filter(PveGuestBinding.connection_id.in_([conn.id for conn in conns]))
        .all()
    ):
        bindings[(binding.connection_id, binding.guest_type, binding.vmid)] = binding

    result: list[dict] = []
    for conn in conns:
        reachable = 1 if (get_connection_state(conn.id) or {}).get("reachable") else 0
        guests = get_guests_by_connection(conn.id)
        for vmid in sorted(guests):
            snap = guests[vmid]
            key: GuestKey = (conn.id, snap["guest_type"], vmid)
            if allowed_guests is not None and key not in allowed_guests:
                continue
            binding = bindings.get(key)
            result.append(
                {
                    "connection_id": conn.id,
                    "connection_name": conn.name,
                    "guest_type": snap["guest_type"],
                    "vmid": vmid,
                    "name": snap.get("name") or f"VM {vmid}",
                    "node": snap.get("node"),
                    "status": str(snap.get("status") or "unknown"),
                    "ip_address": binding.ip_address if binding else None,
                    "os_system": binding.os_system if binding else None,
                    "cpu": snap.get("cpu"),
                    "mem": snap.get("mem") or None,
                    "maxmem": snap.get("maxmem") or None,
                    "reachable": reachable,
                }
            )
    return result
