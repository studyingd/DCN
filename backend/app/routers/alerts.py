"""Alert center APIs: rules, active/history events and acknowledgement."""

from datetime import datetime, timedelta, timezone
from typing import Union

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models.alert import AlertEvent, AlertRule
from app.models.business import (
    Business,
    BusinessInterface,
    BusinessPveGuest,
    BusinessServer,
)
from app.models.device import OPS_TARGET_TYPES, Device
from app.models.device_container import DeviceContainer
from app.models.maintenance_window import MaintenanceWindow
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.models.user import User
from app.schemas.alert import (
    AlertEventResponse,
    AlertEventsPageResponse,
    AlertOverviewResponse,
    AlertRuleCreate,
    AlertRuleResponse,
    AlertRuleUpdate,
    MaintenanceWindowCreate,
    MaintenanceWindowResponse,
)
from app.services.alerts import (
    _guest_binding_ip,
    _guest_binding_ips,
    close_events_of_deleted_rule,
    close_events_of_disabled_rule,
    close_events_of_rule_out_of_scope,
    snooze_event,
)
from app.services.permissions import (
    get_user_device_ids,
    get_user_pve_guest_keys,
    require_permission,
    user_can_access_device,
    user_can_access_pve,
    user_can_access_pve_vmid,
)
from app.services.pve import build_client, is_guest_template
from app.services.pve_guest_status import get_guests_by_connection
from app.services.remediation import (
    request_analysis,
    request_remediation,
    summarize,
)

router = APIRouter(prefix="/api/alerts", tags=["alerts"])
_NUMERIC_METRICS = {"cpu_pct", "mem_pct", "disk_max_pct"}
_PVE_ID_FACTOR = 1_000_000


def _pve_resource_id(connection_id: int, vmid: int) -> int:
    """与 automation/containers 共用的合成负数 ID。"""
    return -(connection_id * _PVE_ID_FACTOR + vmid)


def _decode_resource_id(resource_id: str | None) -> tuple[int, int] | None:
    try:
        value = int(resource_id or "")
    except (TypeError, ValueError):
        return None
    if value >= 0:
        return None
    conn_id, vmid = divmod(abs(value), _PVE_ID_FACTOR)
    return (conn_id, vmid) if conn_id and vmid else None


def _rule_view(row: AlertRule, active_count: int = 0, closed_count: int = 0) -> dict:
    return {
        **{
            key: getattr(row, key)
            for key in (
                "id",
                "name",
                "metric",
                "operator",
                "threshold",
                "severity",
                "target_device_ids",
                "target_container_ids",
                "target_business_ids",
                "cooldown_seconds",
                "sustain_seconds",
                "enabled",
                "created_by",
                "created_at",
                "updated_at",
            )
        },
        "active_event_count": active_count,
        "closed_event_count": closed_count,
    }


def _event_view(
    row: AlertEvent,
    device: Device | None,
    rule: AlertRule | None,
    *,
    include_analysis_text: bool = False,
    guest_ip: str | None = None,
) -> dict:
    name = row.resource_name or (device.name if device else "未知资源")
    record = row.remediation_json or {}
    return {
        "id": row.id,
        "rule_id": row.rule_id,
        # 规则删除后事件历史保留(rule_id 置空)，用规则名快照兜底。
        "rule_name": rule.name if rule else (row.rule_name or "已删除的告警规则"),
        "device_id": row.device_id,
        "device_name": name,
        # 虚拟机事件没有本地设备行,地址用运维接入 IP(QGA 优先回填过的)
        "device_ip": device.ip_address if device else guest_ip,
        "metric": row.metric,
        "value": row.value,
        "threshold": row.threshold,
        "severity": row.severity,
        "status": row.status,
        "message": row.message,
        "first_triggered_at": row.first_triggered_at,
        "last_seen_at": row.last_seen_at,
        "resolved_at": row.resolved_at,
        "occurrence_count": row.occurrence_count,
        "notification_status": row.notification_status,
        "last_notified_at": row.last_notified_at,
        "resource_type": row.resource_type or "device",
        "resource_id": row.resource_id,
        "resource_name": row.resource_name,
        "remediation_state": row.remediation_state or "",
        "remediation_detail": row.remediation_detail,
        "remediation_target": record.get("target"),
        "remediation_attempts": int(record.get("attempts") or 0),
        # 已知晓(snooze):恢复前不再重复提醒
        "snoozed": bool(row.snoozed),
        "snoozed_at": row.snoozed_at,
        "snoozed_by_name": row.snoozed_by_name,
        "analysis_state": row.analysis_state or "",
        # 列表只带一句话摘要，全文走详情接口，避免 30s 轮询载荷过大
        "analysis_summary": summarize(row.analysis_text, 200)
        if row.analysis_text
        else None,
        "analysis_text": row.analysis_text if include_analysis_text else None,
        "agent_run_id": row.agent_run_id,
    }


def _event_or_404(event_id: int, db: Session, current_user: User) -> AlertEvent:
    """按可见性取告警事件;越权与不存在同样返回 404，避免探测资源。"""
    row = db.query(AlertEvent).filter(AlertEvent.id == event_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="告警事件不存在")
    if row.device_id is not None:
        if not user_can_access_device(current_user, row.device_id, db):
            raise HTTPException(status_code=404, detail="告警事件不存在")
    elif not user_can_access_pve(current_user, db):
        raise HTTPException(status_code=404, detail="告警事件不存在")
    else:
        # PVE 身份可能编码在 resource_id(主机状态)或处置记录 target_id(容器)。
        decoded = _decode_resource_id(row.resource_id) or _decode_resource_id(
            (row.remediation_json or {}).get("target_id")
        )
        if decoded and not user_can_access_pve_vmid(
            current_user, db, decoded[0], decoded[1]
        ):
            raise HTTPException(status_code=404, detail="告警事件不存在")
    return row


def _allowed_query(query, current_user: User, db: Session):
    """按设备 ACL + 虚拟机 ACL 过滤告警事件。

    ``device_id`` 为空的行代表 PVE/业务/容器等动态资源：设备范围受限时它们
    本来一律不可见，现在只要角色获得了对应虚拟机的授权就能看到。
    虚拟机三类事件(指标/主机状态的 resource_id 负数编码、容器事件的处置记录
    target_id)在创建时统一冗余 ``owner_target_id``,这里一次 IN 全收——旧版
    对 remediation_json 做 cast(Text).like 匹配,无法用索引,是全表扫描。
    """
    allowed = get_user_device_ids(current_user, db)
    guest_keys = get_user_pve_guest_keys(current_user, db)
    if allowed is None and guest_keys is None:
        return query
    clauses = []
    if allowed is not None:
        clauses.append(AlertEvent.device_id.in_(allowed))
    if guest_keys is None:
        # 虚拟机不受限：保持原有“非设备类事件随 PVE 权限可见”的行为。
        if allowed is not None and user_can_access_pve(current_user, db):
            clauses.append(AlertEvent.device_id.is_(None))
    elif user_can_access_pve(current_user, db):
        pve_ids = [
            _pve_resource_id(conn_id, vmid) for conn_id, _gtype, vmid in guest_keys
        ]
        if pve_ids:
            # 历史行由迁移 0043 回填 owner_target_id;未回填到位的旧行(极少)
            # 与旧版 LIKE 同样匹配不到,可见性口径不变
            clauses.append(AlertEvent.owner_target_id.in_(pve_ids))
    if not clauses:
        return query.filter(AlertEvent.id.is_(None))
    return query.filter(or_(*clauses))


@router.get("/overview", response_model=AlertOverviewResponse)
def alert_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    # 4 个事件计数(open/critical/24h/failed)合并成一条条件聚合查询,代替旧的
    # 4 条独立 COUNT——前端 30s 轮询一次,每次省 3 次带 ACL 过滤的查询。
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    stats = _allowed_query(
        db.query(
            func.sum(case((AlertEvent.status == "open", 1), else_=0)).label("open"),
            func.sum(
                case(
                    (
                        and_(
                            AlertEvent.status == "open",
                            AlertEvent.severity == "critical",
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("critical"),
            func.sum(case((AlertEvent.first_triggered_at >= since, 1), else_=0)).label(
                "recent"
            ),
            func.sum(
                case(
                    (
                        and_(
                            AlertEvent.notification_status == "failed",
                            AlertEvent.first_triggered_at >= since,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("failed"),
        ),
        current_user,
        db,
    ).first()
    open_events = int(stats.open or 0)
    # 规则删除后事件仍保留(rule_id 置空)，不能再当成"有规则正在告警"。
    active_rules = (
        _allowed_query(db.query(AlertEvent), current_user, db)
        .filter(AlertEvent.status == "open", AlertEvent.rule_id.isnot(None))
        .with_entities(AlertEvent.rule_id)
        .distinct()
        .count()
    )
    return AlertOverviewResponse(
        active_rules=active_rules,
        enabled_rules=db.query(AlertRule).filter(AlertRule.enabled.is_(True)).count(),
        open_events=open_events,
        critical_events=int(stats.critical or 0),
        events_24h=int(stats.recent or 0),
        failed_notifications=int(stats.failed or 0),
    )


@router.get("/containers")
def alert_containers(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """容器候选:普通设备上的 + PVE 虚拟机上的(经 pve_guest_binding 归属)。

    返回 ``target_id``(设备为正 id,虚拟机为合成负数 id)作为宿主身份,
    前端按它分组;勾虚拟机等价于勾其全部容器。
    """
    device_query = (
        db.query(DeviceContainer, Device)
        .join(Device, Device.id == DeviceContainer.device_id)
        .order_by(Device.name, DeviceContainer.name)
    )
    allowed = get_user_device_ids(current_user, db)
    if allowed is not None:
        device_query = device_query.filter(Device.id.in_(allowed))
    items = [
        {
            "container_id": c.container_id,
            "name": c.name,
            "device_id": d.id,
            "device_name": d.name,
            "state": c.state,
            "status": c.status,
            "target_id": d.id,
            "host_kind": "device",
        }
        for c, d in device_query.all()
    ]
    # PVE 虚拟机容器:仅列出当前用户获得授权的 guest;名称取内存快照(QGA 实时名),没有则退回 VMID。
    guest_keys = get_user_pve_guest_keys(current_user, db)
    if guest_keys is None or guest_keys:
        guest_rows = (
            db.query(DeviceContainer, PveGuestBinding, PveConnection)
            .join(
                PveGuestBinding,
                PveGuestBinding.id == DeviceContainer.pve_guest_binding_id,
            )
            .join(PveConnection, PveConnection.id == PveGuestBinding.connection_id)
            .order_by(PveConnection.name, PveGuestBinding.vmid, DeviceContainer.name)
            .all()
        )
        snapshot_cache: dict[int, dict[int, dict]] = {}
        for c, binding, conn in guest_rows:
            if (
                guest_keys is not None
                and (
                    conn.id,
                    binding.guest_type,
                    binding.vmid,
                )
                not in guest_keys
            ):
                continue
            if conn.id not in snapshot_cache:
                snapshot_cache[conn.id] = get_guests_by_connection(conn.id)
            guest_name = (snapshot_cache[conn.id].get(binding.vmid) or {}).get("name")
            display = guest_name or f"{binding.guest_type.upper()} VM {binding.vmid}"
            items.append(
                {
                    "container_id": c.container_id,
                    "name": c.name,
                    "device_id": None,
                    "device_name": f"[{conn.name}] {display}",
                    "state": c.state,
                    "status": c.status,
                    "target_id": _pve_resource_id(conn.id, binding.vmid),
                    "host_kind": "pve_guest",
                }
            )
    return items


@router.get("/businesses")
def alert_businesses(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """业务状态规则的目标候选。

    可见性与业务列表(/api/businesses)同口径(2026-09-18 修复):设备范围受限的
    用户,其可见成员(服务器/接口/PVE 虚机)任一存在的业务都可选——旧实现只
    join business_servers,纯接口/虚机业务不进候选,但 admin 全量可见,造成
    "选得到却保存报不存在"的割裂(保存校验同批修复)。
    """
    allowed = get_user_device_ids(current_user, db)
    businesses = db.query(Business).order_by(Business.name).all()
    if allowed is None:
        return [{"business_id": b.id, "name": b.name} for b in businesses]
    allowed_guests = get_user_pve_guest_keys(current_user, db)
    result = []
    for business in businesses:
        # 服务器成员命中用户设备范围
        if (
            db.query(BusinessServer.id)
            .filter(
                BusinessServer.business_id == business.id,
                BusinessServer.device_id.in_(allowed),
            )
            .first()
            is not None
        ):
            result.append({"business_id": business.id, "name": business.name})
            continue
        # 虚机成员命中用户虚拟机范围
        if allowed_guests is not None and any(
            (link.connection_id, link.guest_type, link.vmid) in allowed_guests
            for link in db.query(BusinessPveGuest)
            .filter(BusinessPveGuest.business_id == business.id)
            .all()
        ):
            result.append({"business_id": business.id, "name": business.name})
            continue
        # 纯接口业务无资源 ACL 语义,设备范围受限的用户同样可选
        if (
            db.query(BusinessInterface.id)
            .filter(BusinessInterface.business_id == business.id)
            .first()
            is not None
        ):
            result.append({"business_id": business.id, "name": business.name})
    return result


@router.get("/rules", response_model=list[AlertRuleResponse])
def list_rules(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    query = db.query(AlertRule).order_by(AlertRule.created_at.desc())
    if get_user_device_ids(current_user, db) is not None:
        query = query.filter(AlertRule.created_by == current_user.id)
    rows = query.all()
    counts = dict(
        db.query(AlertEvent.rule_id, func.count(AlertEvent.id))
        .filter(AlertEvent.status == "open")
        .group_by(AlertEvent.rule_id)
        .all()
    )
    return [_rule_view(row, counts.get(row.id, 0)) for row in rows]


def _validate_targets(
    target_ids: list[int] | None, current_user: User, db: Session
) -> None:
    if target_ids is None:
        return
    regular_ids = [item for item in target_ids if item >= 0]
    devices = (
        db.query(Device.id)
        .filter(Device.id.in_(regular_ids), Device.type.in_(OPS_TARGET_TYPES))
        .all()
        if regular_ids
        else []
    )
    valid = {item[0] for item in devices}
    pve_ids = [item for item in target_ids if item < 0]
    valid_pve = set()
    for target in pve_ids:
        value = abs(target)
        conn_id, vmid = divmod(value, 1_000_000)
        conn = (
            db.query(PveConnection)
            .filter(PveConnection.id == conn_id, PveConnection.enabled == 1)
            .first()
        )
        if not conn:
            continue
        # 优先用 30s 刷新的内存快照判存在性(模板机已在快照刷新时剔除,零 PVE
        # API 成本);快照为空(刚启动/还没轮到该平台)才回退实时 API,与旧行为一致。
        guests = get_guests_by_connection(conn.id)
        if guests:
            if vmid in guests:
                valid_pve.add(target)
            continue
        try:
            if any(
                int(g.get("vmid")) == vmid
                for g in build_client(conn).list_guest_resources()
                if not is_guest_template(g)
            ):
                valid_pve.add(target)
        except Exception:
            continue
    if any(
        (
            item >= 0
            and (
                item not in valid or not user_can_access_device(current_user, item, db)
            )
        )
        or (
            item < 0
            and (
                item not in valid_pve
                or not user_can_access_pve(current_user, db)
                or not user_can_access_pve_vmid(
                    current_user,
                    db,
                    divmod(abs(item), _PVE_ID_FACTOR)[0],
                    divmod(abs(item), _PVE_ID_FACTOR)[1],
                )
            )
        )
        for item in target_ids
    ):
        raise HTTPException(
            status_code=403, detail="告警规则包含无权访问或不可纳管的设备"
        )


def _validate_rule_targets(
    body, current_user: User, db: Session, existing: AlertRule | None = None
) -> None:
    metric = (
        body.metric
        if getattr(body, "metric", None) is not None
        else (existing.metric if existing else None)
    )
    _validate_targets(
        body.target_device_ids
        if getattr(body, "target_device_ids", None) is not None
        else (existing.target_device_ids if existing else None),
        current_user,
        db,
    )
    container_ids = (
        body.target_container_ids
        if getattr(body, "target_container_ids", None) is not None
        else (existing.target_container_ids if existing else None)
    )
    if metric and metric.startswith("container_") and container_ids:
        rows = (
            db.query(DeviceContainer, Device, PveGuestBinding)
            .outerjoin(Device, Device.id == DeviceContainer.device_id)
            .outerjoin(
                PveGuestBinding,
                PveGuestBinding.id == DeviceContainer.pve_guest_binding_id,
            )
            .filter(DeviceContainer.container_id.in_(container_ids))
            .all()
        )
        if len({c.container_id for c, _d, _b in rows}) != len(set(container_ids)):
            raise HTTPException(
                status_code=403, detail="告警规则包含无权访问或不存在的容器"
            )
        for _c, device, binding in rows:
            if device is not None:
                if not user_can_access_device(current_user, device.id, db):
                    raise HTTPException(
                        status_code=403, detail="告警规则包含无权访问或不存在的容器"
                    )
            elif binding is not None:
                if not user_can_access_pve_vmid(
                    current_user, db, binding.connection_id, binding.vmid
                ):
                    raise HTTPException(
                        status_code=403, detail="告警规则包含无权访问或不存在的容器"
                    )
            else:
                raise HTTPException(
                    status_code=403, detail="告警规则包含无权访问或不存在的容器"
                )
    business_ids = (
        body.target_business_ids
        if getattr(body, "target_business_ids", None) is not None
        else (existing.target_business_ids if existing else None)
    )
    if metric and metric.startswith("business_") and business_ids:
        # 存在性按 Business 本体判定(2026-09-18 修复):业务的合法成员有
        # 服务器/接口/PVE 虚机三类,只关联接口或虚机、没有服务器的业务
        # 评估器照样监控——旧实现查 business_servers 反查,纯接口/虚机业务
        # 被误报「不存在的业务」(实测业务 cs 只有接口+虚机,保存规则 404)。
        businesses = db.query(Business).filter(Business.id.in_(business_ids)).all()
        if {b.id for b in businesses} != set(business_ids):
            raise HTTPException(status_code=404, detail="告警规则包含不存在的业务")
        # ACL 与业务列表口径一致:按成员判定——服务器走设备 ACL,虚机走
        # 虚拟机 ACL,接口无资源 ACL 概念;成员全部无权(且不是"未受限")才拒。
        for business in businesses:
            device_ids = [
                row[0]
                for row in db.query(BusinessServer.device_id)
                .filter(BusinessServer.business_id == business.id)
                .all()
            ]
            guest_keys = [
                (row[0], row[1], row[2])
                for row in db.query(
                    BusinessPveGuest.connection_id,
                    BusinessPveGuest.guest_type,
                    BusinessPveGuest.vmid,
                )
                .filter(BusinessPveGuest.business_id == business.id)
                .all()
            ]
            has_iface = (
                db.query(BusinessInterface.id)
                .filter(BusinessInterface.business_id == business.id)
                .first()
                is not None
            )
            if has_iface and not device_ids and not guest_keys:
                continue  # 纯接口业务:无资源 ACL 语义,放行
            members_ok = any(
                user_can_access_device(current_user, device_id, db)
                for device_id in device_ids
            ) or any(
                user_can_access_pve_vmid(current_user, db, connection_id, vmid)
                for connection_id, _guest_type, vmid in guest_keys
            )
            if not members_ok:
                raise HTTPException(
                    status_code=403, detail="告警规则包含无权访问的业务"
                )


@router.post("/rules", response_model=AlertRuleResponse)
def create_rule(
    body: AlertRuleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    _validate_rule_targets(body, current_user, db)
    data = body.model_dump()
    if data.get("metric") not in _NUMERIC_METRICS:
        # 状态类规则没有阈值语义:值恒为 0/1,固定 operator=gt、threshold=0,
        # 与评估器 _matches 的比较口径匹配。severity 不再强制 critical——
        # 业务状态类告警配 warning/info 是合理需求,由前端默认 critical 兼顾默认行为。
        data["threshold"] = 0
        data["operator"] = "gt"
    row = AlertRule(**data, created_by=current_user.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _rule_view(row)


@router.put("/rules/{rule_id}", response_model=AlertRuleResponse)
def update_rule(
    rule_id: int,
    body: AlertRuleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    row = db.query(AlertRule).filter(AlertRule.id == rule_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="告警规则不存在")
    was_enabled = bool(row.enabled)
    previous_scope = (
        row.metric,
        tuple(row.target_device_ids or ()),
        tuple(str(v) for v in (row.target_container_ids or ())),
        tuple(row.target_business_ids or ()),
    )
    data = body.model_dump(exclude_unset=True)
    _validate_rule_targets(body, current_user, db, row)
    effective_metric = data.get("metric", row.metric)
    if effective_metric not in _NUMERIC_METRICS:
        # 同 create:状态类只固定阈值语义,severity 交给调用方选择
        data["threshold"] = 0
        data["operator"] = "gt"
    for key, value in data.items():
        setattr(
            row,
            key,
            value.strip() if key == "name" and isinstance(value, str) else value,
        )
    db.commit()
    db.refresh(row)
    closed = 0
    if was_enabled and not row.enabled:
        # 评估器只看 enabled 的规则，停用后它的事件再也不会被刷新或恢复，
        # 必须当场收尾，否则告警中心永远挂着"未恢复"。
        closed = close_events_of_disabled_rule(db, row)
    else:
        current_scope = (
            row.metric,
            tuple(row.target_device_ids or ()),
            tuple(str(v) for v in (row.target_container_ids or ())),
            tuple(row.target_business_ids or ()),
        )
        if current_scope != previous_scope:
            # 收窄监控范围后，被移出去的对象同样再也不会被这条规则评估到，
            # 它遗留的告警必须当场收尾，不能等到下一个采集周期。只收本规则的
            # 越界事件(精准版)，其它规则的孤儿白有周期性 sweep 兑底，
            # 不用在这里全表扫一遍。
            closed = close_events_of_rule_out_of_scope(db, row)
            # 收尾在独立事务里提交，先结束当前快照，否则下面的计数读到的还是旧数据
            db.commit()
    count = (
        db.query(AlertEvent)
        .filter(AlertEvent.rule_id == row.id, AlertEvent.status == "open")
        .count()
    )
    return _rule_view(row, count, closed)


@router.delete("/rules/{rule_id}")
def delete_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("automation:manage")),
):
    """删除告警规则，但保留它产生过的告警历史。

    规则是配置，事件是运维留痕:删掉一条配错的规则不该连带销毁"什么时候告过警、
    AI 归因得出过什么结论"。事件先按停用同样的方式收尾(推 alert.resolved)，
    再由外键 ON DELETE SET NULL 断开关联，规则名以快照形式留在事件上。
    """
    row = db.query(AlertRule).filter(AlertRule.id == rule_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="告警规则不存在")
    closed = close_events_of_deleted_rule(db, row)
    db.delete(row)
    db.commit()
    message = "告警规则已删除，历史告警事件已保留"
    if closed:
        message = f"告警规则已删除，同时关闭了 {closed} 条进行中告警，历史事件已保留"
    return {"message": message, "closed_event_count": closed}


def _event_device(db: Session, row: AlertEvent) -> Device | None:
    """事件关联的设备。

    PVE 虚机与业务类事件没有本地设备行(``device_id`` 为 NULL)。直接
    ``db.get(Device, None)`` 会触发 SQLAlchemy 的 "fully NULL primary key
    identity" 警告，且官方已声明未来版本会改成抛错，所以先判空。
    """
    return db.get(Device, row.device_id) if row.device_id else None


def _devices_by_id(db: Session, device_ids: set) -> dict[int, Device]:
    """一次取回事件涉及的全部设备，避免列表接口逐行查询造成 N+1。"""
    if not device_ids:
        return {}
    return {row.id: row for row in db.query(Device).filter(Device.id.in_(device_ids))}


@router.get(
    "/events",
    response_model=Union[list[AlertEventResponse], AlertEventsPageResponse],
)
def list_events(
    status: str | None = Query(None),
    severity: str | None = Query(None),
    metric: str | None = Query(None, max_length=32),
    q: str | None = Query(None, max_length=100),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    page: int | None = Query(None, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """告警事件列表:旧调用(不带 page)返回纯数组;带 page 返回分页对象。

    筛选:status/severity/metric 精确匹配,since/until 卡首末次触发时间窗口
    (first_triggered_at),q 按规则名/对象名/消息内容模糊搜索。历史事件检索
    (90 天保留)以前只能看最近 100 条,现在前端可翻页+搜索。
    """
    # 权限过滤只用到 alert_events.device_id，不需要 JOIN；规则改用 selectinload
    # 单独批量取，这样 rule_id 为 NULL(规则已删、历史保留)的事件也不会被内连接吞掉。
    query = _allowed_query(db.query(AlertEvent), current_user, db)
    if status:
        query = query.filter(AlertEvent.status == status)
    else:
        query = query.filter(AlertEvent.status.in_(("open", "resolved")))
    if severity:
        query = query.filter(AlertEvent.severity == severity)
    if metric:
        query = query.filter(AlertEvent.metric == metric)
    if since:
        query = query.filter(AlertEvent.first_triggered_at >= since)
    if until:
        query = query.filter(AlertEvent.first_triggered_at <= until)
    if q:
        # 转义 LIKE 通配符,用户输入的 %/_ 只当普通字符
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        query = query.filter(
            or_(
                AlertEvent.rule_name.like(pattern),
                AlertEvent.resource_name.like(pattern),
                AlertEvent.message.like(pattern),
            )
        )
    query = query.options(selectinload(AlertEvent.rule)).order_by(
        AlertEvent.last_seen_at.desc()
    )
    if page is None:
        # 旧调用方:返回数组(长度上限 page_size)
        rows = query.limit(page_size).all()
    else:
        total = query.with_entities(AlertEvent.id).count()
        rows = query.offset((page - 1) * page_size).limit(page_size).all()
    # 以前是逐行 db.get(Device, ...)，limit=500 时最多能发出 500 条额外查询。
    devices = _devices_by_id(db, {row.device_id for row in rows if row.device_id})
    guest_ips = _guest_binding_ips(db, rows)
    items = [
        _event_view(
            row, devices.get(row.device_id), row.rule, guest_ip=guest_ips.get(row.id)
        )
        for row in rows
    ]
    if page is None:
        return items
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/events/{event_id}", response_model=AlertEventResponse)
def get_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """单条告警详情(含 Agent 归因分析全文)。"""
    row = _event_or_404(event_id, db, current_user)
    return _event_view(
        row,
        _event_device(db, row),
        row.rule,
        include_analysis_text=True,
        guest_ip=_guest_binding_ip(db, row),
    )


@router.post("/events/{event_id}/remediate", response_model=AlertEventResponse)
def remediate_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """手动触发一次自动处置:离线虚拟机 / 容器立即尝试拉起。

    校验通过后交后台线程执行，进度话术会持续写回事件 message。
    """
    row = _event_or_404(event_id, db, current_user)
    result = request_remediation(row.id)
    if not result.get("ok"):
        raise HTTPException(
            status_code=409, detail=result.get("error") or "无法自动处置"
        )
    db.refresh(row)
    return _event_view(
        row, _event_device(db, row), row.rule, guest_ip=_guest_binding_ip(db, row)
    )


@router.post("/events/{event_id}/analyze", response_model=AlertEventResponse)
def analyze_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """手动触发一次 Agent 只读归因分析(CPU / 内存 / 磁盘 / 容器状态)。"""
    row = _event_or_404(event_id, db, current_user)
    result = request_analysis(row.id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error") or "无法分析")
    # request_analysis 在另一个会话里已把状态写成 running；先结束当前事务，
    # 否则 MySQL REPEATABLE READ 下 refresh 读到的还是旧快照。
    db.commit()
    db.refresh(row)
    return _event_view(
        row, _event_device(db, row), row.rule, guest_ip=_guest_binding_ip(db, row)
    )


@router.post("/events/{event_id}/ack", response_model=AlertEventResponse)
def ack_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """站内「已知晓」:该事件恢复前冷却到期不再重发告警通知。

    幂等:重复点击保持首次时间;评估/归因/自动处置不受影响,恢复照常推
    alert.resolved。认领人快照取当前登录用户。
    """
    _event_or_404(event_id, db, current_user)
    # snooze_event 在独立会话里提交;先结束当前事务快照再读新状态
    if snooze_event(event_id, by_name=current_user.username) is None:
        raise HTTPException(status_code=404, detail="告警事件不存在")
    db.commit()
    db.expire_all()
    row = db.query(AlertEvent).filter(AlertEvent.id == event_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="告警事件不存在")
    return _event_view(
        row, _event_device(db, row), row.rule, guest_ip=_guest_binding_ip(db, row)
    )


# ── 静默窗口(维护期免轰炸) ──


def _window_view(row: "MaintenanceWindow") -> dict:
    now = datetime.now(timezone.utc)
    start = _as_utc_or(row.start_at, now)
    end = _as_utc_or(row.end_at, now)
    if not row.enabled or now >= end:
        state = "ended"
    elif now >= start:
        state = "active"
    else:
        state = "pending"
    targets = row.target_ids or []
    target_label = "全部对象" if not targets else f"{len(targets)} 个对象"
    return {
        "id": row.id,
        "name": row.name,
        "target_ids": targets,
        "start_at": row.start_at,
        "end_at": row.end_at,
        "enabled": row.enabled,
        "muted_count": row.muted_count,
        "summary_state": row.summary_state or "",
        "state": state,
        "target_label": target_label,
        "created_at": row.created_at,
    }


def _as_utc_or(value: datetime, default: datetime) -> datetime:
    try:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    except Exception:
        return default


@router.get("/maintenance-windows", response_model=list[MaintenanceWindowResponse])
def list_maintenance_windows(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """静默窗口列表(含已结束的历史,用于回看汇总结果)。"""
    rows = (
        db.query(MaintenanceWindow)
        .order_by(MaintenanceWindow.start_at.desc())
        .limit(100)
        .all()
    )
    return [_window_view(row) for row in rows]


@router.post("/maintenance-windows", response_model=MaintenanceWindowResponse)
def create_maintenance_window(
    body: MaintenanceWindowCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """新建静默窗口:窗口内目标对象的告警通知被抑制(事件照常流转),结束时推汇总。"""
    if body.end_at <= body.start_at:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")
    now = datetime.now(timezone.utc)
    start = (
        body.start_at
        if body.start_at.tzinfo
        else body.start_at.replace(tzinfo=timezone.utc)
    )
    end = (
        body.end_at if body.end_at.tzinfo else body.end_at.replace(tzinfo=timezone.utc)
    )
    if end <= now:
        raise HTTPException(status_code=400, detail="结束时间已过去,窗口无意义")
    # 目标校验:设备存在即可(虚机合成负数 id 验证成本高,放过;静默多了无害)
    if body.target_ids:
        _validate_targets([t for t in body.target_ids if t >= 0], current_user, db)
    row = MaintenanceWindow(
        name=body.name,
        target_ids=body.target_ids or None,
        start_at=start,
        end_at=end,
        enabled=True,
        created_by=current_user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _window_view(row)


@router.post(
    "/maintenance-windows/{window_id}/finish", response_model=MaintenanceWindowResponse
)
def finish_maintenance_window(
    window_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """提前结束窗口:把 end_at 拉到当下,通知立即恢复,汇总卡由后台循环(≤30s)推出。"""
    row = db.query(MaintenanceWindow).filter(MaintenanceWindow.id == window_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="静默窗口不存在")
    now = datetime.now(timezone.utc)
    if row.enabled and now < row.end_at:
        # 回溯 1 秒:MySQL DATETIME(fsp=0)会把微秒四舍五入到下一秒,
        # now 直接落库可能出现 end_at > now → 视图仍显示 active(测试实测踩到)
        row.end_at = now - timedelta(seconds=1)
        db.commit()
        db.refresh(row)
    return _window_view(row)


@router.delete("/maintenance-windows/{window_id}")
def delete_maintenance_window(
    window_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("automation:manage")),
):
    """删除窗口(立即恢复通知,不推汇总——删=不想要这条记录)。"""
    row = db.query(MaintenanceWindow).filter(MaintenanceWindow.id == window_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="静默窗口不存在")
    db.delete(row)
    db.commit()
    return {"message": "静默窗口已删除,通知已恢复"}


@router.get("/labels")
def alert_labels(
    _user: User = Depends(require_permission("automation:manage")),
):
    """前端展示用的文案字典(唯一真源在后端,避免两份维护漂移)。"""
    from app.services.alerts import (
        ANALYSIS_STATE_LABELS,
        METRIC_LABELS,
        REMEDIATION_STATE_LABELS,
        SEVERITY_LABELS,
    )

    notify_labels = {
        "pending": "待发送",
        "sent": "已发送",
        "failed": "失败",
        "skipped": "跳过",
    }
    return {
        "metrics": METRIC_LABELS,
        "severities": SEVERITY_LABELS,
        "remediation_states": REMEDIATION_STATE_LABELS,
        "analysis_states": ANALYSIS_STATE_LABELS,
        "notify_status": notify_labels,
    }
