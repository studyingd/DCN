"""PVE(Proxmox VE)平台管理 API。

连接配置由 settings:manage 维护(API Token 加密存储);
查看类端点 device:view;虚机电源/快照/控制台 device:remote;新建/删除虚机 device:manage。
"""

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.business import BusinessPveGuest
from app.models.device_container import DeviceContainer, DeviceDockerStatus
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.models.user import User
from app.schemas.device import OSDetectRequest, OSDetectResponse
from app.schemas.pve import (
    PveConnectionCreate,
    PveConnectionResponse,
    PveConnectionUpdate,
    PveGuestBindingResponse,
    PveGuestBindingUpdate,
)
from app.services.auth import get_current_user
from app.services.crypto import decrypt, encrypt
from app.services.os_detect import detect_os
from app.services.permissions import (
    get_user_pve_guest_keys,
    require_pve_connection_permission,
    require_pve_guest_permission,
    require_pve_permission,
    user_can_access_pve,
)
from app.services.pve import (
    PveError,
    build_client,
    guest_agent_summary,
    is_guest_template,
    parse_guest_volumes,
)
from app.services.pve_guest_metrics import collect_guest_filesystems
from app.services.pve_guest_rates import compute_rates
from app.services.pve_guest_status import record_guest_ip
from app.services.winrm import probe_winrm_service
from app.services.winrm_setup import (
    build_winrm_setup_script,
    is_ipv4,
    select_ipv4_target,
)
from app.utils import apply_update
from app.validators import validate_dns_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pve", tags=["pve"])


# ── helpers ──


def _get_conn(db: Session, conn_id: int) -> PveConnection:
    conn = db.query(PveConnection).filter(PveConnection.id == conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="PVE 连接不存在")
    if not conn.enabled:
        raise HTTPException(status_code=400, detail="该 PVE 连接已停用")
    return conn


def _client(db: Session, conn_id: int):
    return build_client(_get_conn(db, conn_id))


def _validate_gtype(gtype: str) -> str:
    # LXC 支持已彻底移除(2026-09-17 用户拍板):创建/观测/操作全链路只认 qemu。
    if gtype != "qemu":
        raise HTTPException(
            status_code=400, detail="平台仅支持 qemu 虚拟机(LXC 已移除)"
        )
    return gtype


def _validate_node_name(node: str) -> str:
    """节点名只允许 PVE 主机名安全字符集,防任意串注入 PVE 请求路径。"""
    candidate = str(node or "").strip()
    if not candidate or not all(ch.isalnum() or ch in "._-" for ch in candidate):
        raise HTTPException(status_code=400, detail="非法的节点名")
    return candidate


def _validate_upid(upid: str) -> str:
    """UPID 形如 ``UPID:node:ptype:id:user:time:name``,校验字符集 URL 安全。

    upid 会拼进 PVE 请求路径,这里限制成字母数字与 ``_.@:!+-:``(不含 ``/ ? # %``
    空格等保留字符),防止任意字符串注入。"""
    value = str(upid or "").strip()
    if not value or len(value) > 128 or not value.startswith("UPID:"):
        raise HTTPException(status_code=422, detail="非法的任务 ID(UPID)")
    if not re.fullmatch(r"UPID:[A-Za-z0-9_.@:!+\-:]+", value):
        raise HTTPException(status_code=422, detail="非法的任务 ID(UPID)")
    return value


_SNAP_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _validate_snapname(name: str) -> str:
    """快照名仅允许字母/数字/下划线(PVE 自身口径,连字符也不行);
    ``current`` 是 PVE 保留的"当前状态"标识,不能用作快照名。"""
    value = str(name or "").strip()
    if not value:
        raise HTTPException(status_code=422, detail="快照名不能为空")
    if len(value) > 64:
        raise HTTPException(status_code=422, detail="快照名过长(最多 64 字符)")
    if not _SNAP_NAME_RE.fullmatch(value):
        raise HTTPException(
            status_code=422,
            detail="快照名仅支持字母、数字、下划线(不支持连字符/中文/空格)",
        )
    if value.lower() == "current":
        raise HTTPException(
            status_code=422, detail="current 是 PVE 保留名,不能用作快照名"
        )
    return value


def _infer_guest_os(config: dict) -> str:
    """Infer a guest OS family from PVE's ostype without Guest Agent."""
    ostype = str(config.get("ostype") or "").lower()
    # PVE uses win10/win11 as well as legacy wxp/w2k* identifiers.
    if ostype.startswith(("win", "wxp", "w2k")):
        return "windows"
    # Install media names are a useful fallback when the PVE OS Type was left
    # at the generic Linux default (`l26`). This covers common Windows ISO and
    # VirtIO media names without requiring Guest Agent access.
    media = " ".join(
        str(value).lower()
        for key, value in config.items()
        if key.startswith(("ide", "sata", "scsi"))
    )
    if any(
        marker in media
        for marker in ("windows", "win10", "win11", "winserver", "virtio-win")
    ):
        return "windows"
    # QEMU's default `l26` is frequently left unchanged for Windows VMs, so it
    # is not reliable evidence.
    if ostype in {
        "debian",
        "ubuntu",
        "centos",
        "fedora",
        "opensuse",
        "archlinux",
        "alpine",
    }:
        return "linux"
    return "unknown"


def _require_binding_manage(
    user: User,
    db: Session,
    *,
    guest: tuple[int, str, int] | None = None,
) -> None:
    """运维接入配置属于 pve:manage，并叠加虚拟机级 ACL。

    ``settings:manage`` 曾在这里兼任 PVE 管理后门；迁移 0034 已给持有该键的角色
    补授 ``pve:manage``，因此此处不再重复兼容。
    """
    if not user_can_access_pve(user, db, manage=True, guest=guest):
        raise HTTPException(status_code=403, detail="需要 PVE 管理权限")


def _find_binding(
    db: Session, conn_id: int, gtype: str, vmid: int
) -> PveGuestBinding | None:
    return (
        db.query(PveGuestBinding)
        .filter_by(connection_id=conn_id, guest_type=gtype, vmid=vmid)
        .first()
    )


def _purge_guest_local_records(
    db: Session, conn_id: int, gtype: str, vmid: int
) -> dict:
    """虚机在 PVE 侧被销毁后，清掉平台里指向它的引用。

    PVE guest 不是 ``devices`` 行，平台侧的身份是 ``(connection_id, guest_type, vmid)``。
    留着这些记录会让容器采集、业务监控继续去连一台已经不存在的虚机，表现为
    "永远离线"的假告警和被自愈流程反复拉起。

    ``container_actions`` 是审计流水，外键为 SET NULL，这里刻意保留。
    """
    removed = {"bindings": 0, "containers": 0, "docker_status": 0, "business_links": 0}
    binding = _find_binding(db, conn_id, gtype, vmid)
    if binding is not None:
        # 显式删子表，不依赖数据库级 CASCADE（不同部署的建库方式不一致）。
        removed["containers"] = (
            db.query(DeviceContainer)
            .filter_by(pve_guest_binding_id=binding.id)
            .delete(synchronize_session=False)
        )
        removed["docker_status"] = (
            db.query(DeviceDockerStatus)
            .filter_by(pve_guest_binding_id=binding.id)
            .delete(synchronize_session=False)
        )
        db.delete(binding)
        removed["bindings"] = 1
    biz_query = db.query(BusinessPveGuest).filter_by(
        connection_id=conn_id, guest_type=gtype, vmid=vmid
    )
    removed["business_links"] = biz_query.delete(synchronize_session=False)
    db.commit()
    return removed


def _binding_view(
    binding: PveGuestBinding | None,
    conn_id: int,
    gtype: str,
    vmid: int,
    inferred_os: str = "unknown",
    qga: dict | None = None,
) -> dict:
    # QGA facts win over stale PVE metadata and previously saved binding values
    # whenever the agent is reachable; fallback values are retained otherwise.
    qga = qga or {}
    qga_available = bool(qga.get("qga_available"))
    qga_os = qga.get("qga_os_system")
    # Guest Agent is the authoritative source while it is reachable.  The
    # PVE ``ostype=l26`` value and a previously saved binding are only fallbacks
    # for guests where QGA is disabled/unavailable.
    resolved_os = (
        qga_os
        if qga_available and qga_os in {"linux", "windows"}
        else (
            inferred_os
            if inferred_os in {"linux", "windows"}
            else (binding.os_system if binding else inferred_os)
        )
    )
    # 排除法兜底(与设备侧 utils/osType.ts 同口径):Windows 必须被 QGA/ostype/
    # 探测显式确证，确证不了的一律按 Linux 对待。os_assumed=True 表示该 linux
    # 是兜底假设而非确证，前端会据此自动跑一次端口探测来纠正藏着的 Windows。
    os_assumed = resolved_os not in {"linux", "windows"}
    if os_assumed:
        resolved_os = "linux"
    qga_ip = qga.get("qga_ip_address")
    resolved_ip = (
        qga_ip
        if qga_available and qga_ip
        else (binding.ip_address if binding and binding.ip_address else qga_ip)
    )
    # 运行期问到的实时地址记入「最近已知 IP」缓存:QGA-only 虚机(没保存过
    # 绑定行)停机后,告警卡片的地址列靠它兜底,不至于退回 '-'。
    if qga_available and qga_ip:
        record_guest_ip(conn_id, vmid, qga_ip)
    winrm_port = binding.winrm_port if binding else 5985
    winrm_available = bool(
        resolved_os == "windows"
        and resolved_ip
        and (
            (binding and binding.last_test_status == "success")
            or probe_winrm_service(resolved_ip, winrm_port)
        )
    )
    return {
        "id": binding.id if binding else None,
        "connection_id": conn_id,
        "guest_type": gtype,
        "vmid": vmid,
        "ip_address": resolved_ip,
        "os_system": resolved_os,
        "os_assumed": os_assumed,
        # 持久化的精确 OS 名;QGA 实时 pretty-name 在 qga_os_name,前端优先用它。
        "os_name": binding.os_name if binding else None,
        "ssh_port": binding.ssh_port if binding else 22,
        "winrm_port": winrm_port,
        "username": binding.username if binding else None,
        "has_password": bool(binding and binding.password_enc),
        "has_ssh_key": bool(binding and binding.ssh_key_enc),
        "enabled": bool(binding.enabled) if binding else False,
        "configured": bool(
            binding
            and binding.enabled
            and resolved_ip
            and binding.username
            and (binding.password_enc or binding.ssh_key_enc)
        ),
        "last_test_status": binding.last_test_status if binding else None,
        "last_test_error": binding.last_test_error if binding else None,
        "last_test_at": binding.last_test_at.isoformat()
        if binding and binding.last_test_at
        else None,
        "winrm_available": winrm_available,
        **qga,
    }


def _guest_agent_view(
    client,
    node: str,
    gtype: str,
    vmid: int,
    config: dict | None = None,
    *,
    binding: PveGuestBinding | None = None,
    fallback_filesystems: bool = False,
    inferred_os: str | None = None,
) -> dict:
    try:
        qga = guest_agent_summary(client, node, gtype, vmid, config)
    except Exception:
        qga = {
            "qga_enabled": False,
            "qga_available": False,
            "qga_ip_address": None,
            "qga_os_system": None,
            "qga_os_name": None,
            "qga_disks": [],
        }
    if not fallback_filesystems:
        return qga

    active_binding = binding if binding and binding.enabled else None
    password = (
        decrypt(active_binding.password_enc)
        if active_binding and active_binding.password_enc
        else ""
    )
    ssh_key = (
        decrypt(active_binding.ssh_key_enc)
        if active_binding and active_binding.ssh_key_enc
        else ""
    )
    # A live QGA address/OS wins when QGA supplied one; the persisted binding is
    # the fallback for disabled/unavailable agents.
    result = collect_guest_filesystems(
        qga,
        ip_address=(qga.get("qga_ip_address") if qga.get("qga_available") else None)
        or (active_binding.ip_address if active_binding else None),
        os_system=(qga.get("qga_os_system") if qga.get("qga_available") else None)
        or (active_binding.os_system if active_binding else None)
        or inferred_os,
        username=active_binding.username if active_binding else None,
        password=password,
        ssh_key=ssh_key,
        ssh_port=active_binding.ssh_port if active_binding else 22,
        winrm_port=active_binding.winrm_port if active_binding else 5985,
        ssh_host_key=active_binding.ssh_host_key if active_binding else None,
    )
    captured_key = result.pop("_ssh_host_key", None)
    if captured_key and active_binding and not active_binding.ssh_host_key:
        active_binding.ssh_host_key = captured_key
    return result


def _wrap(
    fn,
    *args,
    user: User | None = None,
    db: Session | None = None,
    **kwargs,
):
    """把 PveError 转成 502。

    传入 ``user``/``db`` 时（view 用户可达的读路径都应传）：非 pve:manage
    用户拿到的 detail 换成通用文案，原始详情只进日志——PveError 文本里
    带着上游 DNS/主机名等内部细节（越权实测："hostname lookup 'pve'
    failed..." 直接泄露给了单虚机授权账号）。
    """
    try:
        return fn(*args, **kwargs)
    except PveError as exc:
        detail = str(exc)
        if (
            user is not None
            and db is not None
            and not user_can_access_pve(user, db, manage=True)
        ):
            logger.warning(
                "PVE 502 detail sanitized for user %s: %s",
                user.username,
                detail[:300],
            )
            detail = "PVE 平台接口调用失败，请联系管理员"
        raise HTTPException(status_code=502, detail=detail) from exc


# ══════════════════════════════════════════════════════════════════
# PVE 连接管理(settings:manage)
# ══════════════════════════════════════════════════════════════════


@router.get("/connections", response_model=list[PveConnectionResponse])
def list_connections(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_pve_permission()),
):
    """连接清单。pve:manage 看全部；pve:view 只见有授权虚机的连接，
    且 host/port/token_id 脱敏(越权实测：清单曾把全部连接的基础设施
    地址与 token 身份泄露给单虚机授权账号)。"""
    rows = db.query(PveConnection).order_by(PveConnection.id).all()
    is_manage = user_can_access_pve(current_user, db, manage=True)
    if is_manage:
        return [PveConnectionResponse.model_validate(c) for c in rows]
    allowed = get_user_pve_guest_keys(current_user, db)
    if allowed is None:  # device_scope='all'：不受限但不持 pve:manage
        visible = rows
    else:
        conn_ids = {key[0] for key in allowed}
        visible = [c for c in rows if c.id in conn_ids]
    return [
        PveConnectionResponse.model_validate(c).model_copy(
            update={"host": "", "port": 0, "token_id": ""}
        )
        for c in visible
    ]


@router.post("/connections", response_model=PveConnectionResponse, status_code=201)
def create_connection(
    body: PveConnectionCreate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_pve_permission(manage=True)),
):
    conn = PveConnection(
        name=body.name,
        host=body.host,
        port=body.port,
        token_id=body.token_id,
        token_secret_enc=encrypt(body.token_secret),
        verify_ssl=body.verify_ssl,
        enabled=body.enabled,
        description=body.description,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return PveConnectionResponse.model_validate(conn)


@router.put("/connections/{conn_id}", response_model=PveConnectionResponse)
def update_connection(
    conn_id: int,
    body: PveConnectionUpdate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_pve_permission(manage=True)),
):
    conn = db.query(PveConnection).filter(PveConnection.id == conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="PVE 连接不存在")
    data = body.model_dump(exclude_unset=True)
    if "token_secret" in data:
        secret = data.pop("token_secret")
        if secret:
            conn.token_secret_enc = encrypt(secret)
    apply_update(
        conn,
        data,
        ["name", "host", "port", "token_id", "verify_ssl", "enabled", "description"],
    )
    db.commit()
    db.refresh(conn)
    return PveConnectionResponse.model_validate(conn)


@router.delete("/connections/{conn_id}")
def delete_connection(
    conn_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_pve_permission(manage=True)),
):
    conn = db.query(PveConnection).filter(PveConnection.id == conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="PVE 连接不存在")
    db.delete(conn)
    db.commit()
    return {"message": "连接已删除"}


@router.post("/connections/{conn_id}/test")
def test_connection(
    conn_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_pve_permission(manage=True)),
):
    """测试连接:调 /version 验证连通性与 Token 有效性。"""
    client = build_client(
        db.query(PveConnection).filter(PveConnection.id == conn_id).first()
        or _raise404()
    )
    data = _wrap(client.version)
    return {"ok": True, "version": (data or {}).get("version")}


def _raise404():
    raise HTTPException(status_code=404, detail="PVE 连接不存在")


# ══════════════════════════════════════════════════════════════════
# 资源总览(device:view)
# ══════════════════════════════════════════════════════════════════


def _guest_type_of(raw: dict) -> str:
    """cluster/resources 用 id 前缀区分 guest 类型(如 "qemu/101"、"lxc/200")。

    LXC 已彻底移除:非 qemu 的记录(如 lxc)原样返回类型值,由调用方过滤丢弃。"""
    raw_id = str(raw.get("id") or "")
    gtype = raw_id.split("/", 1)[0] if "/" in raw_id else str(raw.get("type") or "")
    return gtype if gtype in ("qemu", "lxc") else "qemu"


def _filter_guests_by_acl(
    db: Session, current_user: User, conn_id: int, guests: list
) -> list:
    """按角色虚拟机白名单过滤总览里的 guest；``device_scope='all'`` 时原样返回。"""
    allowed = get_user_pve_guest_keys(current_user, db)
    if allowed is None:
        return guests
    scoped = {(g, v) for c, g, v in allowed if c == conn_id}
    return [
        raw
        for raw in guests
        if isinstance(raw, dict) and (_guest_type_of(raw), raw.get("vmid")) in scoped
    ]


@router.get("/connections/{conn_id}/overview")
def overview(
    conn_id: int,
    force: bool = Query(
        False, description="跳过 cluster/resources 缓存,强制拉取实时数据"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_pve_connection_permission()),
):
    """节点 + 全部虚机/容器聚合总览（按角色虚拟机白名单过滤）。

    前端列表轮询默认带 ``force=true``：cluster/resources 的 12s 缓存会让前端
    10s 轮询一半时间拿到同一份数据,4 个 IO 计数器完全不变,前端速率计算
    会全部归 0 或停在「采样中」。实时性 > PVE API 压力(每次仍只是一次请求)。
    """
    client = _client(db, conn_id)
    nodes = _wrap(client.list_nodes, user=current_user, db=db)
    # cluster/resources 一次返回所有 guest，并包含 diskread/diskwrite/netin/netout；
    # 避免按节点、按虚机逐个请求造成 PVE API 压力。
    guests = _wrap(
        lambda: client.list_guest_resources(force=force),
        user=current_user,
        db=db,
    )
    # 差分基线在后端进程内,由后台采集循环持续保温:正常情况下打开页面
    # 第一次请求就能拿到速率,不再有「采样中/0B/s」空窗期。
    compute_rates(conn_id, guests)
    # LXC 已移除:列表只呈现 qemu 虚拟机(cluster/resources 里混入的 lxc 行丢弃)。
    qemu_guests = [g for g in guests if _guest_type_of(g) == "qemu"]
    return {
        "nodes": nodes,
        "guests": _filter_guests_by_acl(db, current_user, conn_id, qemu_guests),
    }


@router.get("/connections/{conn_id}/nodes")
def list_nodes(
    conn_id: int,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_permission(manage=True)),
):
    return _wrap(_client(db, conn_id).list_nodes)


@router.get("/connections/{conn_id}/nodes/{node}/storage")
def list_storage(
    conn_id: int,
    node: str,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_permission(manage=True)),
):
    return _wrap(_client(db, conn_id).list_storage, node)


@router.get("/connections/{conn_id}/nodes/{node}/isos")
def list_isos(
    conn_id: int,
    node: str,
    storage: str = "local",
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_permission(manage=True)),
):
    return _wrap(_client(db, conn_id).list_iso_images, node, storage)


@router.get("/connections/{conn_id}/nextid")
def next_vmid(
    conn_id: int,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_permission(manage=True)),
):
    return {"vmid": _wrap(_client(db, conn_id).next_vmid)}


@router.get("/connections/{conn_id}/task-status")
def pve_task_status(
    conn_id: int,
    node: str = Query(..., description="任务所在节点"),
    upid: str = Query(..., max_length=200, description="PVE 异步任务 ID(UPID)"),
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_permission(manage=True)),
):
    """查询 PVE 异步任务(创建/克隆/销毁/快照)的执行状态。

    create/clone 等操作提交后返回 UPID,PVE 侧任务可能失败(存储满/名称冲突/
    锁定),前端轮询本端点到终态,把 exitstatus 展示给用户。"""
    node = _validate_node_name(node)
    upid = _validate_upid(upid)
    data = _wrap(_client(db, conn_id).task_status, node, upid)
    stopped = str(data.get("status") or "") == "stopped"
    exitstatus = data.get("exitstatus")
    return {
        "status": "stopped" if stopped else "running",
        # PVE 在终态时必填 exitstatus("OK" 或错误文本);缺失时保守按成功,
        # 避免显示无意义的报错
        "exitstatus": exitstatus,
        "ok": stopped and str(exitstatus or "OK") == "OK",
        "done": stopped,
    }


# ══════════════════════════════════════════════════════════════════
# 虚机/容器操作
# ══════════════════════════════════════════════════════════════════


@router.get("/connections/{conn_id}/guests/{gtype}/{vmid}")
def guest_detail(
    conn_id: int,
    gtype: str,
    vmid: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_pve_guest_permission()),
):
    _validate_gtype(gtype)
    client = _client(db, conn_id)
    node = _guest_node(client, vmid, user=current_user, db=db)
    config = _wrap(client.guest_config, node, gtype, vmid, user=current_user, db=db)
    binding = _find_binding(db, conn_id, gtype, vmid)
    previous_host_key = binding.ssh_host_key if binding else None
    inferred_os = _infer_guest_os(config)
    qga = _guest_agent_view(
        client,
        node,
        gtype,
        vmid,
        config,
        binding=binding,
        fallback_filesystems=True,
        inferred_os=inferred_os,
    )
    # Persist the first-contact SSH host key captured by the fallback path.
    # This keeps the same TOFU protection used by ordinary Device metrics.
    if binding and binding.ssh_host_key != previous_host_key:
        db.commit()
    return {
        "status": _wrap(client.guest_status, node, gtype, vmid),
        "config": config,
        "guest_agent": qga,
    }


# 正在后台补探 os_name 的 binding id,防止并发 GET 重复起线程。
# add/discard 都在锁内:线程池并发请求同一 binding 时,无锁 set 的
# 「检查后写入」窗口会漏过重复起线程。
_os_name_backfill_inflight: set[int] = set()
_os_name_backfill_lock = threading.Lock()


def _backfill_os_name(binding_id: int) -> None:
    """后台线程:用已存凭据补探精确 OS 名并落库(QGA 不可用时的兜底链路)。

    Windows 经 WinRM Caption、Linux 经 SSH /etc/os-release。幂等:os_name
    已有值或凭据缺失时直接返回。
    """
    from app.database import SessionLocal
    from app.services.os_detect import (
        probe_linux_os_via_ssh_sync,
        probe_windows_os_via_winrm_sync,
    )

    db = SessionLocal()
    try:
        b = db.get(PveGuestBinding, binding_id)
        if not b or b.os_name or not b.ip_address or not b.username:
            return
        password = decrypt(b.password_enc) if b.password_enc else None
        if not password and not b.ssh_key_enc:
            return
        precise = None
        if b.os_system == "windows":
            if password:
                precise = probe_windows_os_via_winrm_sync(
                    b.ip_address, b.username, password, b.winrm_port or 5985
                )
        else:
            precise = probe_linux_os_via_ssh_sync(
                b.ip_address,
                b.username,
                password or "",
                port=b.ssh_port or 22,
                pinned_key=b.ssh_host_key,
                private_key=decrypt(b.ssh_key_enc) if b.ssh_key_enc else None,
            )
        if precise:
            b.os_name = precise
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
        with _os_name_backfill_lock:
            _os_name_backfill_inflight.discard(binding_id)


def _maybe_backfill_os_name(binding: PveGuestBinding | None, view: dict) -> None:
    """有凭据且 os_name 为空时触发后台补探——存量 binding(旧代码时期保存,
    从未触发过凭据探测) 的精确版本号全靠这条链路补。"""
    if (
        binding is None
        or not binding.enabled
        or binding.os_name
        or not (binding.password_enc or binding.ssh_key_enc)
        or not view.get("ip_address")
    ):
        return
    with _os_name_backfill_lock:
        if binding.id in _os_name_backfill_inflight:
            return
        _os_name_backfill_inflight.add(binding.id)
    threading.Thread(target=_backfill_os_name, args=(binding.id,), daemon=True).start()


@router.get(
    "/connections/{conn_id}/guests/{gtype}/{vmid}/binding",
    response_model=PveGuestBindingResponse,
)
def guest_binding(
    conn_id: int,
    gtype: str,
    vmid: int,
    node: str | None = None,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_guest_permission()),
):
    """Return persisted SSH/WinRM access metadata; OS is inferred from PVE config when unset."""
    _validate_gtype(gtype)
    client = _client(db, conn_id)
    guest_node = _guest_node(client, vmid, node)
    config = _wrap(client.guest_config, guest_node, gtype, vmid)
    qga = _guest_agent_view(client, guest_node, gtype, vmid, config)
    binding = _find_binding(db, conn_id, gtype, vmid)
    view = _binding_view(
        binding,
        conn_id,
        gtype,
        vmid,
        _infer_guest_os(config),
        qga,
    )
    _maybe_backfill_os_name(binding, view)
    return view


@router.post(
    "/connections/{conn_id}/guests/{gtype}/{vmid}/binding/detect-os",
    response_model=OSDetectResponse,
)
async def detect_guest_binding_os(
    conn_id: int,
    gtype: str,
    vmid: int,
    body: OSDetectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Detect the guest OS from its reachable services, independent of PVE ostype."""
    _validate_gtype(gtype)
    _require_binding_manage(current_user, db, guest=(conn_id, gtype, vmid))
    client = _client(db, conn_id)
    _guest_node(client, vmid)  # ensure the guest still exists
    binding = _find_binding(db, conn_id, gtype, vmid)
    username = body.username or (binding.username if binding else None)
    password = body.password
    if not password and binding and binding.password_enc:
        password = decrypt(binding.password_enc)
    # 只配了私钥的绑定:回退到已存私钥,否则精确识别永远轮空
    ssh_key = None
    if binding and binding.ssh_key_enc:
        ssh_key = decrypt(binding.ssh_key_enc)
    result = await detect_os(
        body.ip_address,
        username=username,
        password=password,
        winrm_port=body.winrm_port,
        ssh_key=ssh_key,
    )
    # 凭据探测拿到精确名时顺带落库(迁移 0038):QGA 不可用的虚拟机,下次打开
    # 页面直接显示精确名,不必再手动点一次「检测」。banner 级版本串
    # (如 'Linux (OpenSSH) 7.4')不写,避免把 SSH 软件版本误当 OS 版本。
    precise = (result.os_version or "").strip()
    if (
        binding
        and precise
        and "(openssh)" not in precise.lower()
        and "(dropbear)" not in precise.lower()
        and binding.os_name != precise
    ):
        binding.os_name = precise
        db.commit()
    return OSDetectResponse(**result.to_dict())


@router.get(
    "/connections/{conn_id}/guests/{gtype}/{vmid}/winrm-setup-script",
    response_class=PlainTextResponse,
)
def download_guest_winrm_setup_script(
    conn_id: int,
    gtype: str,
    vmid: int,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_guest_permission(manage=True)),
):
    """Generate a dynamic WinRM bootstrap script for a Windows guest."""
    _validate_gtype(gtype)
    binding = _find_binding(db, conn_id, gtype, vmid)
    if not binding:
        raise HTTPException(status_code=400, detail="请先在运维接入中配置虚拟机凭据")
    client = _client(db, conn_id)
    node = _guest_node(client, vmid)
    config = _wrap(client.guest_config, node, gtype, vmid)
    qga = _guest_agent_view(client, node, gtype, vmid, config)
    qga_ip = qga.get("qga_ip_address") if qga.get("qga_available") else None
    # WinRM 脚本仅支持 IPv4，而 QGA 在虚拟机没有 IPv4 时会回报一个全局 IPv6。
    # 这里按「QGA 优先、手填兜底」在两个候选里选 IPv4，而不是只要 QGA 有值就用它。
    target_ip = select_ipv4_target(qga_ip, binding.ip_address)
    if not target_ip:
        candidate = qga_ip or binding.ip_address
        if candidate:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"虚拟机当前地址 {candidate} 不是 IPv4，而 WinRM 启用脚本仅支持 IPv4；"
                    "请在运维接入中为该虚拟机填写一个 IPv4 地址"
                ),
            )
        raise HTTPException(
            status_code=400,
            detail="请先启动 QEMU Guest Agent 或在运维接入中配置虚拟机 IP",
        )
    try:
        script, source_ip = build_winrm_setup_script(
            target_ip, binding.winrm_port or 5985
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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


@router.put(
    "/connections/{conn_id}/guests/{gtype}/{vmid}/binding",
    response_model=PveGuestBindingResponse,
)
def update_guest_binding(
    conn_id: int,
    gtype: str,
    vmid: int,
    body: PveGuestBindingUpdate,
    node: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _validate_gtype(gtype)
    _require_binding_manage(current_user, db, guest=(conn_id, gtype, vmid))
    # Ensure the VM still exists on the selected PVE platform.
    client = _client(db, conn_id)
    guest_node = _guest_node(client, vmid, node)
    config = _wrap(client.guest_config, guest_node, gtype, vmid)
    binding = _find_binding(db, conn_id, gtype, vmid)
    if binding is None:
        binding = PveGuestBinding(connection_id=conn_id, guest_type=gtype, vmid=vmid)
        db.add(binding)
    qga = _guest_agent_view(client, guest_node, gtype, vmid, config)
    # When QGA is available its current address is authoritative, even if an
    # older manual address is still present in the submitted form.
    qga_ip = qga.get("qga_ip_address") if qga.get("qga_available") else None
    form_ip = (body.ip_address or "").strip()
    effective_ip = (qga_ip or form_ip).strip()
    # 例外：QGA 在虚拟机没有 IPv4 时会回报一个全局 IPv6。Windows 的管理通道
    # WinRM 只支持 IPv4，这时必须让表单里手填的 IPv4 胜出，否则用户改什么都
    # 会被 QGA 的 IPv6 覆盖回去，WinRM 永远配不起来。
    if not is_ipv4(effective_ip) and is_ipv4(form_ip):
        effective_ip = form_ip
    if not effective_ip:
        raise HTTPException(
            status_code=400,
            detail="未获取到虚拟机 IP，请先启动 QEMU Guest Agent 或手动填写 IP",
        )
    binding.ip_address = effective_ip
    inferred_os = _infer_guest_os(config)
    # The form value may come from live port detection or explicit correction;
    # do not overwrite it with a stale/misconfigured PVE `ostype=l26`.
    binding.os_system = (
        qga.get("qga_os_system")
        if qga.get("qga_available") and qga.get("qga_os_system") in {"linux", "windows"}
        else (
            body.os_system
            if body.os_system in {"linux", "windows"}
            else (
                binding.os_system
                if binding.os_system in {"linux", "windows"}
                else (inferred_os if inferred_os in {"linux", "windows"} else None)
            )
        )
    )
    # None = 未确证(Windows 需要 QGA/ostype/探测显式确证),各消费方按排除法
    # 将 None 视为 Linux;不要再把字面量 "unknown" 写库。
    binding.ssh_port = body.ssh_port
    binding.winrm_port = body.winrm_port
    binding.username = body.username.strip()
    # When editing an existing binding, omitted secret fields intentionally
    # retain the encrypted values already stored for that guest. Linux guests
    # may authenticate with a private key only; Windows/WinRM still requires a
    # password because the WinRM transport does not consume SSH keys.
    effective_password = (
        body.password
        if body.password is not None
        else (decrypt(binding.password_enc) if binding.password_enc else "")
    )
    effective_ssh_key = (
        body.ssh_key
        if body.ssh_key is not None
        else (decrypt(binding.ssh_key_enc) if binding.ssh_key_enc else "")
    )
    if binding.os_system == "windows":
        if not (effective_password or "").strip():
            raise HTTPException(
                status_code=400, detail="Windows 虚拟机必须配置 WinRM 登录密码"
            )
    elif not ((effective_password or "").strip() or (effective_ssh_key or "").strip()):
        raise HTTPException(
            status_code=400, detail="Linux 虚拟机必须配置登录密码或 SSH 私钥"
        )
    if body.password is not None:
        binding.password_enc = encrypt(body.password) if body.password else None
    if body.ssh_key is not None:
        binding.ssh_key_enc = encrypt(body.ssh_key) if body.ssh_key else None
    binding.enabled = 1 if body.enabled else 0
    binding.last_test_status = None
    binding.last_test_error = None
    db.commit()
    db.refresh(binding)
    # Start a Docker probe immediately after saving a valid guest binding so
    # the container-management page does not wait for the next 60s cycle.
    try:
        from app.services.containers_collector import (
            _spawn_collect_one,
            pve_target_id,
        )

        if binding.enabled:
            _spawn_collect_one(pve_target_id(conn_id, vmid))
    except Exception:
        # Binding persistence must succeed even if the optional collector
        # cannot be started in this worker.
        pass
    return _binding_view(binding, conn_id, gtype, vmid, inferred_os, qga)


@router.post("/connections/{conn_id}/guests/{gtype}/{vmid}/binding/test")
def test_guest_binding(
    conn_id: int,
    gtype: str,
    vmid: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _validate_gtype(gtype)
    _require_binding_manage(current_user, db, guest=(conn_id, gtype, vmid))
    binding = _find_binding(db, conn_id, gtype, vmid)
    if (
        not binding
        or not binding.enabled
        or not binding.ip_address
        or not binding.username
    ):
        raise HTTPException(
            status_code=400, detail="请先配置虚拟机 IP、用户名并启用运维接入"
        )
    password = decrypt(binding.password_enc) if binding.password_enc else ""
    ssh_key = decrypt(binding.ssh_key_enc) if binding.ssh_key_enc else ""
    username = binding.username
    if not password and not ssh_key:
        raise HTTPException(status_code=400, detail="绑定凭据没有可用的密码或 SSH 私钥")
    try:
        if binding.os_system == "windows":
            from app.services.winrm import run_powershell

            code, out, err = run_powershell(
                binding.ip_address,
                username,
                password,
                "Write-Output 'DCN_OK'",
                port=binding.winrm_port,
                timeout=15,
            )
        else:
            from app.services.ssh import exec_ssh_command, open_ssh_client

            client, key = open_ssh_client(
                binding.ip_address,
                binding.ssh_port,
                username,
                password or None,
                timeout=10,
                pinned_key_b64=binding.ssh_host_key or None,
                private_key=ssh_key or None,
                allow_tofu=not bool(binding.ssh_host_key),
            )
            try:
                code, out, err = exec_ssh_command(client, "printf DCN_OK", timeout=10)
            finally:
                client.close()
            if not binding.ssh_host_key:
                binding.ssh_host_key = key
        ok = code == 0 and "DCN_OK" in (out or "")
        binding.last_test_status = "success" if ok else "failed"
        binding.last_test_error = (
            None if ok else (err or out or f"远程命令退出码 {code}")[:500]
        )
    except Exception as exc:
        ok = False
        binding.last_test_status = "failed"
        binding.last_test_error = str(exc)[:500]
    from datetime import datetime, timezone

    binding.last_test_at = datetime.now(timezone.utc)
    db.commit()
    if not ok:
        raise HTTPException(
            status_code=502, detail=binding.last_test_error or "连接测试失败"
        )
    return {"ok": True, "message": "虚拟机运维连接测试成功"}


@router.get("/connections/{conn_id}/guests/{gtype}/{vmid}/history")
def guest_history(
    conn_id: int,
    gtype: str,
    vmid: int,
    range: str = "1h",
    node: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_pve_guest_permission()),
):
    """虚机/容器历史资源数据，使用 PVE RRD，不需要 Guest Agent。"""
    _validate_gtype(gtype)
    timeframe_map = {
        "1h": "hour",
        "24h": "day",
        "7d": "week",
        "30d": "month",
        "1y": "year",
    }
    timeframe = timeframe_map.get(range)
    if not timeframe:
        raise HTTPException(status_code=400, detail="range 必须是 1h/24h/7d/30d/1y")
    client = _client(db, conn_id)
    guest_node = _guest_node(client, vmid, node, user=current_user, db=db)
    return {
        "range": range,
        "timeframe": timeframe,
        "items": _wrap(
            client.guest_rrddata,
            guest_node,
            gtype,
            vmid,
            timeframe,
            user=current_user,
            db=db,
        ),
    }


@router.get("/connections/{conn_id}/guests/rrd-latest")
def guests_rrd_latest(
    conn_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_pve_permission()),
):
    """全部运行中 guest 的最新一条 RRD 速率采样(批量,用于列表页首轮预填)。

    列表页实时速率靠前端对 cluster/resources 累计计数器做差值,首次轮询没有
    基线,会空转一个轮询周期(~10s 显示「采样中」)。RRD 最新采样本身就是
    速率,且由 PVE 按自身节奏生成,不受 cluster/resources 12s 缓存影响。
    服务端并行扇出到 PVE,一台 guest 失败不影响其余。
    """
    client = _client(db, conn_id)
    guests = _filter_guests_by_acl(
        db,
        current_user,
        conn_id,
        [
            g
            for g in _wrap(client.list_guest_resources, user=current_user, db=db)
            if _guest_type_of(g) == "qemu"
            and not is_guest_template(g)
            and g.get("status") == "running"
        ],
    )

    def _latest(guest: dict):
        gtype = _guest_type_of(guest)
        vmid = int(guest.get("vmid"))
        key = f"{gtype}/{vmid}"
        try:
            points = client.guest_rrddata(str(guest["node"]), gtype, vmid, "hour")
        except Exception:
            return key, None
        valid = [p for p in points or [] if isinstance(p, dict) and p.get("time")]
        return key, (valid[-1] if valid else None)

    items: dict[str, dict] = {}
    if guests:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for key, point in pool.map(_latest, guests):
                if point is not None:
                    items[key] = point
    return {"items": items}


def _guest_node(
    client,
    vmid: int,
    known_node: str | None = None,
    *,
    user: User | None = None,
    db: Session | None = None,
) -> str:
    """解析 vmid 所在节点；创建虚机等无 vmid 场景回退到首个节点。

    ``user``/``db`` 透传给内部的 ``_wrap``：view 用户可达的读路径
    （detail/history）要传，PveError 详情才会脱敏。
    """
    # 列表页已经从 /cluster/resources 得到节点，详情请求复用该值，
    # 避免 binding/history/snapshot 每次重复拉取全平台资源列表。
    if known_node:
        candidate = str(known_node).strip()
        if candidate and all(ch.isalnum() or ch in "._-" for ch in candidate):
            return candidate
    if vmid:
        guests = _wrap(client.list_guest_resources, user=user, db=db)
        guest = next(
            (item for item in guests if int(item.get("vmid", -1)) == vmid), None
        )
        if guest and guest.get("node"):
            return str(guest["node"])
    nodes = _wrap(client.list_nodes)
    if not nodes:
        raise HTTPException(status_code=502, detail="PVE 无可用节点")
    return nodes[0]["node"]


class PowerRequest(BaseModel):
    action: str = Field(description="start/stop/shutdown/reboot/reset/suspend/resume")


@router.post("/connections/{conn_id}/guests/{gtype}/{vmid}/power")
def guest_power(
    conn_id: int,
    gtype: str,
    vmid: int,
    body: PowerRequest,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_guest_permission(manage=True)),
):
    _validate_gtype(gtype)
    client = _client(db, conn_id)
    node = _guest_node(client, vmid)
    _wrap(client.power, node, gtype, vmid, body.action)
    return {"success": True, "message": f"已对 {gtype}/{vmid} 执行 {body.action}"}


class SnapshotCreate(BaseModel):
    snapname: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)


@router.get("/connections/{conn_id}/guests/{gtype}/{vmid}/snapshots")
def list_snapshots(
    conn_id: int,
    gtype: str,
    vmid: int,
    node: str | None = None,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_guest_permission()),
):
    _validate_gtype(gtype)
    client = _client(db, conn_id)
    return _wrap(client.list_snapshots, _guest_node(client, vmid, node), gtype, vmid)


@router.post("/connections/{conn_id}/guests/{gtype}/{vmid}/snapshots")
def create_snapshot(
    conn_id: int,
    gtype: str,
    vmid: int,
    body: SnapshotCreate,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_guest_permission(manage=True)),
):
    _validate_gtype(gtype)
    # PVE 对快照名有自己的字符集限制(字母/数字/下划线),非法名只回原始 400;
    # 提前拦下并给中文说明,顺带拦住保留名 current。
    _validate_snapname(body.snapname)
    client = _client(db, conn_id)
    return _wrap(
        client.create_snapshot,
        _guest_node(client, vmid),
        gtype,
        vmid,
        body.snapname,
        body.description,
    )


@router.delete("/connections/{conn_id}/guests/{gtype}/{vmid}/snapshots/{snapname}")
def delete_snapshot(
    conn_id: int,
    gtype: str,
    vmid: int,
    snapname: str,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_guest_permission(manage=True)),
):
    _validate_gtype(gtype)
    client = _client(db, conn_id)
    return _wrap(
        client.delete_snapshot, _guest_node(client, vmid), gtype, vmid, snapname
    )


@router.post(
    "/connections/{conn_id}/guests/{gtype}/{vmid}/snapshots/{snapname}/rollback"
)
def rollback_snapshot(
    conn_id: int,
    gtype: str,
    vmid: int,
    snapname: str,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_guest_permission(manage=True)),
):
    _validate_gtype(gtype)
    client = _client(db, conn_id)
    return _wrap(
        client.rollback_snapshot, _guest_node(client, vmid), gtype, vmid, snapname
    )


class GuestCreateRequest(BaseModel):
    vmid: int | None = None
    name: str = Field(min_length=1, max_length=63)
    cores: int = Field(2, ge=1, le=128)
    memory_mb: int = Field(2048, ge=512, le=524288)
    disk_gb: int = Field(32, ge=1, le=4096)
    storage: str = "local-lvm"
    iso: str | None = None  # 如 local:iso/xxx.iso
    # 默认开启 QEMU Guest Agent:IP/OS/文件系统识别、磁盘告警采集、地址兑底
    # 整条自动化链路都依赖它;guest 里还需装 qemu-guest-agent 包。
    agent: bool = True


@router.post("/connections/{conn_id}/guests/{gtype}")
def create_guest(
    conn_id: int,
    gtype: str,
    body: GuestCreateRequest,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_permission(manage=True)),
):
    _validate_gtype(gtype)
    try:
        guest_name = validate_dns_name(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not guest_name:
        raise HTTPException(status_code=422, detail="名称不能为空")
    client = _client(db, conn_id)
    node = _guest_node(client, 0)
    vmid = body.vmid or _wrap(client.next_vmid)

    params = {
        "vmid": vmid,
        "name": guest_name,
        "cores": body.cores,
        "memory": body.memory_mb,
        "scsi0": f"{body.storage}:{body.disk_gb}",
        "net0": "virtio,bridge=vmbr0",
        "scsihw": "virtio-scsi-pci",
    }
    if body.agent:
        params["agent"] = 1
    if body.iso:
        params["ide2"] = f"{body.iso},media=cdrom"
    result = _wrap(client.create_qemu, node, params)
    return {
        "success": True,
        "vmid": vmid,
        "message": f"已提交创建 {gtype}/{vmid}",
        "task": result,
        # 前端轮询任务终态需要 node(task-status 端点参数)
        "node": node,
    }


class CloneRequest(BaseModel):
    newid: int | None = None  # 留空自动分配
    name: str | None = Field(None, max_length=63)
    full: bool = True  # 完整克隆(独立于模板)
    description: str = Field(default="", max_length=500)


@router.post("/connections/{conn_id}/guests/{gtype}/{vmid}/clone")
def clone_guest(
    conn_id: int,
    gtype: str,
    vmid: int,
    body: CloneRequest,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_guest_permission(manage=True)),
):
    """克隆虚机/容器(模板克隆或整机克隆)。"""
    _validate_gtype(gtype)
    try:
        clone_name = validate_dns_name(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    client = _client(db, conn_id)
    node = _guest_node(client, vmid)
    newid = body.newid or _wrap(client.next_vmid)
    result = _wrap(
        client.clone_guest,
        node,
        gtype,
        vmid,
        newid,
        clone_name,
        body.full,
        body.description,
    )
    return {
        "success": True,
        "newid": newid,
        "message": f"已开始克隆 {gtype}/{vmid} → {newid}",
        "task": result,
        # 前端轮询任务终态需要 node(task-status 端点参数)
        "node": node,
    }


# 强制停机后等待虚机真正 stopped 的上限(秒)。超过就报错，不硬删运行中的虚机。
_DELETE_STOP_WAIT_SECONDS = 60
# 销毁任务提交后等待 PVE 终态的上限(秒)。destroy 是异步任务,失败(磁盘锁/HA
# 引用等)时虚机仍在,必须等终态再清平台记录,否则绑定/容器/业务关联全丢。
_DELETE_TASK_WAIT_SECONDS = 60


class GuestConfigUpdate(BaseModel):
    """调整虚机规格;未提供的字段保持不变。磁盘只允许扩容。"""

    cores: int | None = Field(default=None, ge=1, le=128)
    memory_mb: int | None = Field(default=None, ge=512, le=524288)
    # 磁盘目标总量(GB);只能大于当前容量(数据安全,PVE resize 亦不允许缩)
    disk_gb: int | None = Field(default=None, ge=1, le=4096)


def _disk_volume_of(config: dict) -> tuple[str, int] | None:
    """配置里第一个 QEMU 磁盘卷的 (键, 容量字节);无磁盘返回 None。"""
    for volume in parse_guest_volumes(config):
        if volume["kind"] == "disk" and volume.get("size_bytes"):
            return volume["key"], int(volume["size_bytes"])
    return None


@router.put("/connections/{conn_id}/guests/{gtype}/{vmid}/config")
def update_guest_config(
    conn_id: int,
    gtype: str,
    vmid: int,
    body: GuestConfigUpdate,
    node: str | None = None,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_guest_permission(manage=True)),
):
    """调整虚机 CPU/内存/磁盘容量(仅关机状态,2026-09-17 用户定调)。

    * 虚机必须处于 stopped:运行中一律 409,避免热插拔/重启生效的口径分歧;
    * cores/memory 提交到 PVE config,启动虚机后生效;
    * 磁盘只允许扩容(PVE resize 不允许缩),扩的是第一个磁盘卷(scsi0 类键);
      容量扩大后**还需在客户机内扩展文件系统**才能真正可用;
    * 提交后返回最新 config/status 供前端刷新。
    """
    _validate_gtype(gtype)
    if body.cores is None and body.memory_mb is None and body.disk_gb is None:
        raise HTTPException(status_code=422, detail="至少调整一项(cores/memory/disk)")
    client = _client(db, conn_id)
    resolved_node = _guest_node(client, vmid, node)
    # 仅关机可调:API 直调也拦,与前端按钮口径一致
    status = _wrap(client.guest_status, resolved_node, gtype, vmid)
    if str(status.get("status") or "") != "stopped":
        raise HTTPException(
            status_code=409,
            detail=f"虚机 {gtype}/{vmid} 正在运行，请先关机再调整配置",
        )
    config = _wrap(client.guest_config, resolved_node, gtype, vmid)

    disk_key: str | None = None
    if body.disk_gb is not None:
        current = _disk_volume_of(config)
        if current is None:
            raise HTTPException(status_code=422, detail="未找到虚机磁盘卷,无法调整容量")
        disk_key, current_bytes = current
        target_bytes = body.disk_gb * 1024**3
        if target_bytes <= current_bytes:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"磁盘只允许扩容:当前 {current_bytes / 1024**3:.0f}GB,"
                    f"目标 {body.disk_gb}GB 不大于当前容量"
                ),
            )
        # 用增量(+NG)而不是绝对值:PVE/我们各自取整(G=Gib)存在误差时,
        # 绝对值可能意外变成"缩小"请求而被拒。
        delta_gb = (target_bytes - current_bytes + 1024**3 - 1) // 1024**3
        _wrap(
            client.resize_guest_disk,
            resolved_node,
            gtype,
            vmid,
            disk_key,
            f"+{delta_gb}G",
        )

    params: dict[str, Any] = {}
    if body.cores is not None:
        params["cores"] = body.cores
    if body.memory_mb is not None:
        params["memory"] = body.memory_mb
    if params:
        _wrap(client.update_guest_config, resolved_node, gtype, vmid, params)

    # resize/config 已各自失效资源缓存,重取最新配置回传(status 未变,复用)
    config = _wrap(client.guest_config, resolved_node, gtype, vmid)
    disk = _disk_volume_of(config)
    return {
        "success": True,
        "message": "配置已更新,启动虚机后生效(磁盘扩容后请在客户机内扩展文件系统)",
        "config": config,
        "disk_bytes": disk[1] if disk else None,
        "status": status,
    }


@router.get("/connections/{conn_id}/guests/{gtype}/{vmid}/volumes")
def guest_volumes(
    conn_id: int,
    gtype: str,
    vmid: int,
    node: str | None = None,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_guest_permission()),
):
    """删除前预览:该虚机引用了哪些磁盘卷，销毁时各自会不会被清掉。"""
    _validate_gtype(gtype)
    client = _client(db, conn_id)
    resolved_node = _guest_node(client, vmid, node)
    config = _wrap(client.guest_config, resolved_node, gtype, vmid)
    status = _wrap(client.guest_status, resolved_node, gtype, vmid)
    binding = _find_binding(db, conn_id, gtype, vmid)
    business_links = (
        db.query(BusinessPveGuest)
        .filter_by(connection_id=conn_id, guest_type=gtype, vmid=vmid)
        .count()
    )
    return {
        "node": resolved_node,
        "status": str(status.get("status") or ""),
        "running": str(status.get("status") or "") == "running",
        "template": bool(config.get("template")),
        "volumes": parse_guest_volumes(config),
        "purged_local": {
            "bindings": 1 if binding is not None else 0,
            "containers": (
                db.query(DeviceContainer)
                .filter_by(pve_guest_binding_id=binding.id)
                .count()
                if binding is not None
                else 0
            ),
            "business_links": business_links,
        },
    }


@router.delete("/connections/{conn_id}/guests/{gtype}/{vmid}")
def delete_guest(
    conn_id: int,
    gtype: str,
    vmid: int,
    purge: bool = Query(
        True, description="连同虚机拥有的卷、快照和备份/HA 引用一起删除"
    ),
    destroy_unreferenced_disks: bool = Query(
        True, description="同时销毁 unused* 残留卷，避免存储上留下孤儿磁盘"
    ),
    force: bool = Query(False, description="虚机运行中时先强制停机再删除"),
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_guest_permission(manage=True)),
):
    """销毁虚机，并清理 PVE 侧磁盘与平台侧引用。

    PVE 的 ``destroy`` 默认只删配置;``purge=1`` 才会销毁虚机拥有的卷，
    ``destroy-unreferenced-disks=1`` 才会连 ``unused*`` 残留卷一起清掉。
    共享盘(被其他虚机引用)PVE 不会删，属于期望行为。
    删除成功后同步清理 ``pve_guest_bindings``(连带容器快照)与 ``business_pve_guests``。

    destroy 是异步任务,这里等它到终态才清平台侧引用:任务失败时虚机还在,
    先清本地会让监控/绑定口径全丢(表现为"永远离线"假告警);超时未完时
    同样保留记录,用户重试删除——若届时虚机已从 PVE 消失,走下面的"已不存在"
    分支自动收敛,不会产生半删状态。
    """
    _validate_gtype(gtype)
    client = _client(db, conn_id)

    # 存在性判定兼 node 解析:虚机不在 cluster/resources 里 = PVE 侧已销毁
    # (销毁任务曾成功/有人直接在 PVE 界面删掉)。此时直接清平台引用即可,
    # 也顺带省掉 _guest_node 的第二次资源拉取。
    guests = _wrap(client.list_guest_resources)
    target = next((item for item in guests if int(item.get("vmid", -1)) == vmid), None)
    if target is None:
        removed = _purge_guest_local_records(db, conn_id, gtype, vmid)
        return {
            "success": True,
            "message": f"{gtype}/{vmid} 在 PVE 侧已不存在，已清理平台侧引用",
            "task": None,
            "node": None,
            "stopped_first": False,
            "volumes": [],
            "purged_local": removed,
        }
    node = str(target["node"])

    # 删除前先取一次配置，既用于运行状态判断，也用于回执"清掉了哪些盘"。
    config = _wrap(client.guest_config, node, gtype, vmid)
    volumes = parse_guest_volumes(config)
    status = _wrap(client.guest_status, node, gtype, vmid)

    stopped = False
    if str(status.get("status") or "") == "running":
        if not force:
            raise HTTPException(
                status_code=409,
                detail=f"虚机 {gtype}/{vmid} 正在运行，请先关机，或选择「强制删除」自动停机后销毁",
            )
        _wrap(client.power, node, gtype, vmid, "stop")
        stopped = _wrap(
            client.wait_guest_stopped, node, gtype, vmid, _DELETE_STOP_WAIT_SECONDS
        )
        if not stopped:
            raise HTTPException(
                status_code=409,
                detail=f"强制停机超时({_DELETE_STOP_WAIT_SECONDS}s)，虚机仍在运行，已取消删除",
            )

    task = _wrap(
        client.delete_guest, node, gtype, vmid, purge, destroy_unreferenced_disks
    )
    # 等销毁任务到终态(PVE destroy 异步执行,典型几秒)。失败/超时都不清本地:
    # 失败时虚机还在,记录保留可重试;超时重试走上面的"已不存在"分支收敛。
    if task:
        final = _wrap(
            client.wait_task_done,
            node,
            str(task),
            _DELETE_TASK_WAIT_SECONDS,
        )
        if final is None:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"PVE 销毁任务仍在进行(已等待 {_DELETE_TASK_WAIT_SECONDS}s)，"
                    f"平台侧记录已保留；请稍后重试删除以清理引用"
                ),
            )
        exitstatus = str(final.get("exitstatus") or "")
        if exitstatus and exitstatus != "OK":
            raise HTTPException(
                status_code=502,
                detail=(
                    f"PVE 销毁任务失败：{exitstatus}"
                    f"(平台侧记录已保留，排除问题后可重试删除)"
                ),
            )
    removed = _purge_guest_local_records(db, conn_id, gtype, vmid)

    disks = [v["volid"] for v in volumes if v["kind"] == "disk"]
    orphans = [v["volid"] for v in volumes if v["kind"] == "unused"]
    # ISO 安装介质不是虚机私有数据，PVE 销毁虚机时不会删。
    preserved = [v for v in volumes if v["kind"] in ("cdrom", "bind")]
    parts = [f"已销毁 {gtype}/{vmid}"]
    if not purge:
        # 未开 purge 时卷会保留在存储上，必须如实告知，否则用户以为已经清理干净。
        parts.append(f"未开启 purge，{len(disks)} 个磁盘卷仍保留在存储上")
    else:
        parts.append(f"清理 {len(disks)} 个磁盘卷" if disks else "无磁盘卷")
        if orphans:
            parts.append(
                f"清理 {len(orphans)} 个 unused 残留卷"
                if destroy_unreferenced_disks
                else f"保留 {len(orphans)} 个 unused 残留卷"
            )
    if preserved:
        parts.append(f"{len(preserved)} 个 ISO/绑定挂载按 PVE 规则保留")
    message = "，".join(parts)

    return {
        "success": True,
        "message": message,
        "task": task,
        "node": node,
        "stopped_first": stopped,
        "volumes": volumes,
        "purged_local": removed,
    }


# ── 控制台票据(device:remote;真正的 WS 代理见 routers/pve_console.py) ──


@router.post("/connections/{conn_id}/guests/{gtype}/{vmid}/console")
def console_ticket(
    conn_id: int,
    gtype: str,
    vmid: int,
    db: Session = Depends(get_db),
    _u: User = Depends(require_pve_guest_permission(manage=True)),
):
    """申请 VNC 代理票据,返回前端 noVNC 所需的连接参数。"""
    _validate_gtype(gtype)
    client = _client(db, conn_id)
    node = _guest_node(client, vmid)
    data = _wrap(client.vncproxy, node, gtype, vmid)
    return {
        "node": node,
        "port": data.get("port"),
        "ticket": data.get("ticket"),
        "cert": data.get("cert"),
    }
