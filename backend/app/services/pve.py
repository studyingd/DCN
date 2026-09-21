"""PVE(Proxmox VE)REST API 客户端。

认证:API Token,请求头 `Authorization: PVEAPIToken=<user@realm!tokenid>=<secret>`。
所有方法返回 PVE 响应的 ``data`` 字段。requests 同步阻塞,调用方(FastAPI def 端点)
会跑在线程池里。

参考:PVE API — https://pve.proxmox.com/pve-docs/api-viewer/
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from app.models.pve_connection import PveConnection
from app.services.crypto import decrypt

logger = logging.getLogger(__name__)

# PVE 电源动作白名单
QEMU_POWER_ACTIONS = {
    "start",
    "stop",
    "shutdown",
    "reboot",
    "reset",
    "suspend",
    "resume",
}

# 虚机配置里的磁盘类键:QEMU 是 <总线><序号>(scsi0/virtio1/ide2…)。
# 这些卷归虚机所有,`destroy --purge` 会连带删掉。
_QEMU_DISK_KEY_RE = re.compile(r"^(ide|sata|scsi|virtio|nvme)\d+$", re.IGNORECASE)
# unused0/unused1… 是已从配置卸载、但仍占着存储的残留卷,只有
# `destroy-unreferenced-disks=1` 才会被清理,否则删完虚机它们会变成孤儿数据。
_UNUSED_DISK_KEY_RE = re.compile(r"^unused\d+$", re.IGNORECASE)
_SIZE_UNITS = {
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
}

_RESOURCE_CACHE_TTL = 12.0
_resource_cache: dict[tuple[str, int, str], tuple[float, list[dict]]] = {}
_resource_cache_lock = threading.Lock()
_resource_fetch_locks: dict[tuple[str, int, str], threading.Lock] = {}

# 节点列表缓存(list_nodes 用;节点集/状态变化低频,TTL 稍长)。
_NODE_CACHE_TTL = 30.0
_node_cache: dict[tuple[str, int, str], tuple[float, list[dict]]] = {}


class PveError(Exception):
    """PVE API 调用失败(连接/认证/HTTP 错误统一封装)。"""


def is_guest_template(guest: dict) -> bool:
    """``/cluster/resources`` 里的模板机(克隆源)会带 ``template=1``。

    模板不运行、没有指标，纳进监控只会产生"永远离线"的假告警，甚至被自愈流程
    当成关机虚机去启动。告警相关链路一律跳过;PVE 管理页要展示模板，仍用原始列表。
    """
    return bool(guest.get("template"))


def parse_disk_size(raw: str) -> int | None:
    """解析 PVE 卷描述里的 ``size=32G``(或裸字节数),返回字节;无法解析返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]{0,2})", text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).lower()
    return int(number * _SIZE_UNITS.get(unit, 1))


def parse_guest_volumes(config: dict) -> list[dict]:
    """列出虚机配置里引用的所有磁盘/光驱,用于删除前确认、删除后回执。

    ``kind`` 决定销毁虚机时数据会不会一起没:
      * ``disk``   —— 虚机拥有的磁盘卷,``purge=1`` 会销毁;
      * ``unused`` —— 已从配置卸载的残留卷,物理数据仍在存储上,必须再开
                      ``destroy-unreferenced-disks=1`` 才会清掉,否则变成孤儿磁盘;
      * ``cdrom``  —— 挂载的 ISO 安装介质,属于共享内容,PVE 不会删。
    """
    volumes: list[dict] = []
    for key, value in (config or {}).items():
        name = str(key)
        owned = bool(_QEMU_DISK_KEY_RE.match(name))
        orphaned = bool(_UNUSED_DISK_KEY_RE.match(name))
        if not (owned or orphaned):
            continue
        text = str(value or "")
        # 形如 "local-lvm:vm-101-disk-0,size=32G" 或 "local:101/vm-101-disk-0.qcow2,size=32G"
        volid = text.split(",", 1)[0].strip()
        options = [part.strip().lower() for part in text.split(",")[1:]]
        size = None
        for part in options:
            if part.startswith("size="):
                size = parse_disk_size(part.split("=", 1)[1])
                break
        if orphaned:
            kind = "unused"
        elif "media=cdrom" in options:
            kind = "cdrom"
        else:
            kind = "disk"
        volumes.append(
            {
                "key": name,
                "volid": volid,
                "size_bytes": size,
                "kind": kind,
            }
        )
    order = {"disk": 0, "unused": 1, "cdrom": 2}
    volumes.sort(key=lambda item: (order.get(item["kind"], 9), item["key"]))
    return volumes


def _format_url_host(host: str) -> str:
    """Normalize a PVE host for an HTTPS URL, including IPv6 literals."""
    normalized = str(host or "").strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    # A colon in a host without brackets denotes an IPv6 literal. Brackets are
    # required in the authority portion so the port is parsed correctly.
    return f"[{normalized}]" if ":" in normalized else normalized


class PveClient:
    def __init__(
        self,
        host: str,
        port: int,
        token_id: str,
        token_secret: str,
        verify_ssl: bool = True,
        timeout: int = 15,
    ):
        self.host = str(host or "").strip().strip("[]")
        self.base = f"https://{_format_url_host(self.host)}:{port}/api2/json"
        self.timeout = timeout
        self._session = requests.Session()
        self.authorization_header = f"PVEAPIToken={token_id}={token_secret}"
        self._session.headers["Authorization"] = self.authorization_header
        self._session.verify = verify_ssl
        if not verify_ssl:
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass

    def _req(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base}{path}"
        kwargs.setdefault("timeout", self.timeout)
        try:
            resp = self._session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise PveError(f"无法连接 PVE ({self.base}): {exc}") from exc
        if resp.status_code == 401:
            raise PveError(
                "PVE 认证失败,请检查 API Token(user@realm!tokenid 与 secret)"
            )
        if resp.status_code == 403:
            raise PveError("PVE 拒绝访问(403),请检查 Token 权限")
        if not resp.ok:
            raise PveError(f"PVE 返回 {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json().get("data")
        except ValueError as exc:
            raise PveError("PVE 响应不是合法 JSON") from exc

    # ── 节点 ──
    def list_nodes(self) -> list[dict]:
        """集群节点列表(30s TTL 进程内缓存)。

        节点集与节点状态变化低频;总览页 10s 轮询每次都打 /nodes 纯属浪费,
        多用户同时查看时尤为明显。TTL 30s 足够覆盖节点上下线可见性。
        """
        key = (self.base, int(self.timeout), self.authorization_header)
        now = time.monotonic()
        with _resource_cache_lock:
            cached = _node_cache.get(key)
            if cached and now - cached[0] < _NODE_CACHE_TTL:
                return [dict(item) for item in cached[1]]
        data = self._req("GET", "/nodes") or []
        with _resource_cache_lock:
            _node_cache[key] = (time.monotonic(), [dict(item) for item in data])
        return data

    def node_status(self, node: str) -> dict:
        return self._req("GET", f"/nodes/{node}/status") or {}

    def version(self) -> dict:
        """PVE 版本信息(连接测试用,验证连通性与 Token 有效性)。"""
        return self._req("GET", "/version") or {}

    def task_status(self, node: str, upid: str) -> dict:
        """查询单个异步任务(UPID)的执行状态。

        返回 ``{status: "running"|"stopped", exitstatus: "OK"|错误文本, ...}``;
        ``exitstatus`` 仅在任务到达终态(stopped)后由 PVE 填写。create/clone/
        destroy/快照等操作都是异步任务,提交后必须查这里才知道真实结果。
        """
        return self._req("GET", f"/nodes/{node}/tasks/{upid}/status") or {}

    def wait_task_done(
        self, node: str, upid: str, timeout: int = 60, interval: float = 2.0
    ) -> dict | None:
        """轮询任务直到终态,返回最后一次 task_status;超时返回 None。

        单轮查询失败(PVE 短暂不可达)吞掉继续试,直到 deadline;全程不可达
        同样返回 None,由调用方决定语义("无法确认结果")。
        """
        deadline = time.monotonic() + max(1, int(timeout))
        data: dict | None = None
        while True:
            try:
                data = self.task_status(node, upid)
            except PveError:
                data = None
            if data and str(data.get("status") or "") == "stopped":
                return data
            if time.monotonic() >= deadline:
                return None
            time.sleep(interval)

    def list_guest_resources(
        self, force: bool = False, *, timeout: int | None = None
    ) -> list[dict]:
        """一次获取集群内全部 QEMU/LXC 实时资源与累计 IO 计数。

        ``timeout`` 按次覆盖请求超时:后台快照循环/按需补采用短超时探活时,
        不能改写共享客户端的默认超时(那是全部用户请求路径共用的状态,
        曾经 ``client.timeout = 6`` 一旦触发就会粘住整个进程)。
        """
        key = (self.base, int(self.timeout), self.authorization_header)
        now = time.monotonic()
        if not force:
            with _resource_cache_lock:
                cached = _resource_cache.get(key)
                if cached and now - cached[0] < _RESOURCE_CACHE_TTL:
                    return [dict(item) for item in cached[1]]
        with _resource_cache_lock:
            fetch_lock = _resource_fetch_locks.setdefault(key, threading.Lock())
        with fetch_lock:
            if not force:
                with _resource_cache_lock:
                    cached = _resource_cache.get(key)
                    if cached and time.monotonic() - cached[0] < _RESOURCE_CACHE_TTL:
                        return [dict(item) for item in cached[1]]
            kwargs: dict[str, Any] = {"params": {"type": "vm"}}
            if timeout is not None:
                kwargs["timeout"] = int(timeout)
            data = self._req("GET", "/cluster/resources", **kwargs) or []
            with _resource_cache_lock:
                _resource_cache[key] = (time.monotonic(), [dict(item) for item in data])
        return data

    def invalidate_resource_cache(self) -> None:
        key = (self.base, int(self.timeout), self.authorization_header)
        with _resource_cache_lock:
            _resource_cache.pop(key, None)

    # ── 虚机/容器列表 ──
    def list_qemu(self, node: str) -> list[dict]:
        return self._req("GET", f"/nodes/{node}/qemu") or []

    def list_storage(self, node: str) -> list[dict]:
        return self._req("GET", f"/nodes/{node}/storage") or []

    def list_iso_images(self, node: str, storage: str) -> list[dict]:
        try:
            return (
                self._req(
                    "GET",
                    f"/nodes/{node}/storage/{storage}/content",
                    params={"content": "iso"},
                )
                or []
            )
        except PveError:
            return []

    # ── 虚机/容器状态 ──
    def guest_status(self, node: str, gtype: str, vmid: int) -> dict:
        return self._req("GET", f"/nodes/{node}/{gtype}/{vmid}/status/current") or {}

    def guest_config(self, node: str, gtype: str, vmid: int) -> dict:
        return self._req("GET", f"/nodes/{node}/{gtype}/{vmid}/config") or {}

    def guest_rrddata(
        self, node: str, gtype: str, vmid: int, timeframe: str = "hour"
    ) -> list[dict]:
        """返回 PVE 原生 RRD 历史采样数据(不依赖 Guest Agent)。"""
        return (
            self._req(
                "GET",
                f"/nodes/{node}/{gtype}/{vmid}/rrddata",
                params={"timeframe": timeframe, "cf": "AVERAGE"},
            )
            or []
        )

    def guest_agent(self, node: str, gtype: str, vmid: int) -> dict:
        """Read customer OS data through QEMU Guest Agent.

        A failed call means the agent is disabled, not installed, or the guest
        is stopped; callers should fall back to PVE host-side statistics.
        三条命令并行执行:agent 已启用但未运行时每条要各自等满 4s 超时,
        串行最坏 12s,并行封顶 4s;QEMU 对同一 VM 的 agent 命令本就在 QMP
        层串行,健康路径下并行只是重叠 HTTP/调度开销,无副作用。
        """
        commands = (
            ("interfaces", "network-get-interfaces"),
            ("osinfo", "get-osinfo"),
            ("fsinfo", "get-fsinfo"),
        )

        def _fetch(pair: tuple[str, str]) -> tuple[str, Any | None, str | None]:
            name, command = pair
            try:
                # A Guest Agent command should fail fast.  Otherwise opening a
                # VM detail can block for 45 seconds when QGA was enabled in
                # PVE but is not running in the customer OS.
                value = self._req(
                    "GET",
                    f"/nodes/{node}/{gtype}/{vmid}/agent/{command}",
                    timeout=min(self.timeout, 4),
                )
                return name, (value if value is not None else {}), None
            except PveError as exc:
                return name, None, str(exc)

        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        successes = 0
        with ThreadPoolExecutor(max_workers=len(commands)) as pool:
            for name, value, error in pool.map(_fetch, commands):
                if error is None:
                    results[name] = value
                    successes += 1
                else:
                    errors[name] = error
        results["available"] = successes > 0
        results["errors"] = errors
        return results

    def guest_agent_interfaces(self, node: str, gtype: str, vmid: int) -> Any:
        """只调 QGA 的 network-get-interfaces 单个命令(取 guest IP 用)。

        失败(agent 未启用/guest 停机)独享拋 PveError,调用方自行降级;超时与
        guest_agent 同口径(4s,免得 agent 已启用但未运行时拖住调用方)。
        """
        return self._req(
            "GET",
            f"/nodes/{node}/{gtype}/{vmid}/agent/network-get-interfaces",
            timeout=min(self.timeout, 4),
        )

    def guest_agent_fsinfo(self, node: str, gtype: str, vmid: int) -> Any:
        """只调 QGA 的 get-fsinfo 单个命令(磁盘采集循环用)。

        超时口径与其它 QGA 命令一致;失败独享拋 PveError,由
        ``services.pve.guest_agent_fsinfo_summary`` 走绑定兑底。
        """
        return self._req(
            "GET",
            f"/nodes/{node}/{gtype}/{vmid}/agent/get-fsinfo",
            timeout=min(self.timeout, 4),
        )

    # ── 电源 ──
    def power(self, node: str, gtype: str, vmid: int, action: str) -> Any:
        if action not in QEMU_POWER_ACTIONS:
            raise PveError(f"{gtype} 不支持操作 {action}")
        result = self._req("POST", f"/nodes/{node}/{gtype}/{vmid}/status/{action}")
        self.invalidate_resource_cache()
        return result

    # ── 快照 ──
    def list_snapshots(self, node: str, gtype: str, vmid: int) -> list[dict]:
        return self._req("GET", f"/nodes/{node}/{gtype}/{vmid}/snapshot") or []

    def create_snapshot(
        self, node: str, gtype: str, vmid: int, snapname: str, description: str = ""
    ) -> Any:
        return self._req(
            "POST",
            f"/nodes/{node}/{gtype}/{vmid}/snapshot",
            data={"snapname": snapname, "description": description},
        )

    def delete_snapshot(self, node: str, gtype: str, vmid: int, snapname: str) -> Any:
        return self._req("DELETE", f"/nodes/{node}/{gtype}/{vmid}/snapshot/{snapname}")

    def rollback_snapshot(self, node: str, gtype: str, vmid: int, snapname: str) -> Any:
        return self._req(
            "POST", f"/nodes/{node}/{gtype}/{vmid}/snapshot/{snapname}/rollback"
        )

    # ── 创建/删除 ──
    def next_vmid(self) -> int:
        data = self._req("GET", "/cluster/nextid")
        return int(data)

    def create_qemu(self, node: str, params: dict) -> Any:
        result = self._req("POST", f"/nodes/{node}/qemu", data=params)
        self.invalidate_resource_cache()
        return result

    def update_guest_config(
        self, node: str, gtype: str, vmid: int, params: dict
    ) -> Any:
        """增量更新虚机配置(cores/memory 等;运行中可提交,部分字段需重启生效)。"""
        result = self._req("PUT", f"/nodes/{node}/{gtype}/{vmid}/config", data=params)
        self.invalidate_resource_cache()
        return result

    def resize_guest_disk(
        self, node: str, gtype: str, vmid: int, disk: str, size: str
    ) -> Any:
        """扩容虚机磁盘(目标总量,如 "40G";PVE 只允许扩大)。"""
        result = self._req(
            "PUT",
            f"/nodes/{node}/{gtype}/{vmid}/resize",
            data={"disk": disk, "size": size},
        )
        self.invalidate_resource_cache()
        return result

    def clone_guest(
        self,
        node: str,
        gtype: str,
        vmid: int,
        newid: int,
        name: str | None = None,
        full: bool = True,
        description: str = "",
    ) -> Any:
        """克隆虚机/容器(常用于从模板克隆)。newid 为目标 VMID。"""
        params: dict[str, Any] = {"newid": newid, "full": 1 if full else 0}
        if name:
            params["name"] = name
        if description:
            params["description"] = description
        result = self._req("POST", f"/nodes/{node}/{gtype}/{vmid}/clone", data=params)
        self.invalidate_resource_cache()
        return result

    def delete_guest(
        self,
        node: str,
        gtype: str,
        vmid: int,
        purge: bool = True,
        destroy_unreferenced_disks: bool = True,
    ) -> Any:
        """销毁虚机/LXC。

        ``purge=1``(qm/pct destroy --purge)删除配置、快照,并销毁虚机**拥有**的卷;
        ``destroy-unreferenced-disks=1`` 额外销毁 ``unused*`` 这类已从配置卸载、却仍
        占着存储的残留卷。两者都开,才能保证删完虚机后存储上不留孤儿磁盘。
        共享给其他虚机的卷 PVE 本身不会删,这是期望行为。
        """
        params: dict[str, Any] = {"purge": 1 if purge else 0}
        if destroy_unreferenced_disks:
            params["destroy-unreferenced-disks"] = 1
        result = self._req("DELETE", f"/nodes/{node}/{gtype}/{vmid}", params=params)
        self.invalidate_resource_cache()
        return result

    def wait_guest_stopped(
        self, node: str, gtype: str, vmid: int, timeout: int = 60, interval: float = 2.0
    ) -> bool:
        """轮询等待虚机进入 stopped;超时返回 False(调用方决定是否继续删除)。"""
        deadline = time.monotonic() + max(1, int(timeout))
        while True:
            try:
                if (
                    str(self.guest_status(node, gtype, vmid).get("status") or "")
                    == "stopped"
                ):
                    return True
            except PveError:
                # 状态查不到(例如已被并发删除)视为已停止,交给后续 destroy 给出确切错误
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    # ── 控制台 ──
    def vncproxy(self, node: str, gtype: str, vmid: int) -> dict:
        """申请 VNC websocket 代理票据,返回 {ticket, port, cert, ...}。"""
        data = self._req(
            "POST", f"/nodes/{node}/{gtype}/{vmid}/vncproxy", data={"websocket": 1}
        )
        return data or {}

    def vncwebsocket_url(
        self, node: str, gtype: str, vmid: int, port: int, vncticket: str
    ) -> str:
        from urllib.parse import quote

        return (
            f"wss://{self.base.split('://', 1)[1]}/nodes/{node}/{gtype}/{vmid}"
            f"/vncwebsocket?port={port}&vncticket={quote(vncticket, safe='')}"
        )


def build_client(conn: PveConnection) -> PveClient:
    """从数据库连接记录构建客户端(解密 token secret)。

    进程内按连接身份缓存复用:每次请求都新建 ``requests.Session`` 会把
    TLS 握手/解密 token 的开销乘到每轮轮询上,连接多用户多时还会线性放大
    打到 PVE 的建连数。缓存键覆盖影响客户端行为的全部字段(含 secret 解密
    后的指纹),连接记录一变自然失效;``PveClient`` 自身无状态(每次 _req
    独立),复用是安全的。
    """
    secret = decrypt(conn.token_secret_enc) if conn.token_secret_enc else ""
    key = (
        int(getattr(conn, "id", 0) or 0),
        str(conn.host or ""),
        int(conn.port or 8006),
        str(conn.token_id or ""),
        str(secret),
        bool(conn.verify_ssl),
    )
    with _client_cache_lock:
        cached = _client_cache.get(key)
        if cached is not None:
            return cached
        client = PveClient(
            host=conn.host,
            port=conn.port or 8006,
            token_id=conn.token_id,
            token_secret=secret,
            verify_ssl=bool(conn.verify_ssl),
        )
        if len(_client_cache) > _CLIENT_CACHE_MAX:
            # 上限防膨胀:正常远小于此(连接数本身有限),异常路径兜底
            _client_cache.clear()
        _client_cache[key] = client
        return client


# (id, host, port, token_id, secret, verify_ssl) -> 复用的 PveClient
_client_cache: dict[tuple, PveClient] = {}
_client_cache_lock = threading.Lock()
_CLIENT_CACHE_MAX = 64


def reset_client_cache() -> None:
    """清空客户端缓存(测试用/连接凭据更新后强制重建)。"""
    with _client_cache_lock:
        _client_cache.clear()


def agent_interfaces_ip(interfaces: Any) -> str | None:
    """从 QGA network-get-interfaces 的返回里挑显示 IP(私网 IPv4 优先)。

    与 ``guest_agent_summary`` 的 qga_ip_address 同一挑选口径,抽成独立函数
    供 IP 刷新循环单独调 interfaces 命令时复用。
    """
    rows = (
        interfaces.get("result", interfaces)
        if isinstance(interfaces, dict)
        else interfaces
    )
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for interface in rows if isinstance(rows, list) else []:
        if not isinstance(interface, dict):
            continue
        for item in interface.get("ip-addresses", []) or []:
            if not isinstance(item, dict):
                continue
            try:
                parsed = ipaddress.ip_address(str(item.get("ip-address") or ""))
            except ValueError:
                continue
            if (
                parsed.is_loopback
                or parsed.is_link_local
                or parsed.is_multicast
                or parsed.is_unspecified
            ):
                continue
            addresses.append(parsed)
    # Prefer the RFC1918 IPv4 normally used by DCN; then any IPv4, then IPv6.
    preferred = next(
        (ip for ip in addresses if ip.version == 4 and ip.is_private), None
    )
    preferred = preferred or next((ip for ip in addresses if ip.version == 4), None)
    preferred = preferred or (addresses[0] if addresses else None)
    return str(preferred) if preferred else None


def _agent_enabled_from_config(config: dict) -> bool:
    """PVE 虚机配置的 ``agent`` 键 → QGA 是否启用(支持 ``enabled=0`` 子选项)。"""
    raw_agent = str(config.get("agent") or "").strip().lower()
    if raw_agent in {"1", "true", "yes", "on"}:
        return True
    if raw_agent in {"", "0", "false", "no", "off", "none"}:
        return False
    options = dict(
        part.strip().split("=", 1) for part in raw_agent.split(",") if "=" in part
    )
    return options.get("enabled", "1") not in {"0", "false", "no", "off"}


def _parse_qga_fsinfo(fsinfo: Any) -> list[dict]:
    """QGA get-fsinfo 原始返回 → 可用文件系统行(滤光驱介质,按挂载点排序)。"""
    rows = fsinfo.get("result", fsinfo) if isinstance(fsinfo, dict) else fsinfo
    disks: list[dict] = []
    for fs in rows if isinstance(rows, list) else []:
        if not isinstance(fs, dict):
            continue
        filesystem_type = str(fs.get("type") or "").strip()
        # Optical media exposed by Windows QGA uses CDFS/UDF; Linux guests
        # commonly report ISO9660. These are mounted installation/driver media,
        # not writable guest disks, so they should not appear as capacity data.
        if filesystem_type.lower() in {"cdfs", "udf", "iso9660"}:
            continue
        try:
            total = int(fs.get("total-bytes"))
        except (TypeError, ValueError):
            continue
        if total <= 0:
            continue
        try:
            used = int(fs["used-bytes"]) if fs.get("used-bytes") is not None else None
        except (TypeError, ValueError):
            used = None
        disks.append(
            {
                "mount": fs.get("mountpoint") or fs.get("name") or "未命名卷",
                "filesystem_type": filesystem_type or None,
                "total_bytes": total,
                "used_bytes": used,
            }
        )
    # PVE/QGA does not guarantee filesystem enumeration order. Keep the UI
    # deterministic and intuitive (Windows C:, D:, E:…; then normal mounts).
    disks.sort(key=lambda item: str(item.get("mount") or "").lower())
    return disks


def guest_agent_summary(
    client: PveClient,
    node: str,
    gtype: str,
    vmid: int,
    config: dict | None = None,
) -> dict:
    """Return normalized QGA facts used by both UI and automation.

    The function deliberately returns a complete false/empty structure when
    QGA is disabled or unavailable, allowing every caller to use the same
    deterministic fallback path.
    """
    result = {
        "qga_enabled": False,
        "qga_available": False,
        "qga_ip_address": None,
        "qga_os_system": None,
        "qga_os_name": None,
        "qga_disks": [],
    }
    if gtype != "qemu":
        return result

    config = config if config is not None else client.guest_config(node, gtype, vmid)
    enabled = _agent_enabled_from_config(config)
    result["qga_enabled"] = enabled
    if not enabled:
        return result

    data = client.guest_agent(node, gtype, vmid)
    result["qga_available"] = bool(data.get("available"))
    if not result["qga_available"]:
        return result

    osinfo = data.get("osinfo") or {}
    os_row = osinfo.get("result", osinfo) if isinstance(osinfo, dict) else {}
    if isinstance(os_row, dict):
        os_name = str(
            os_row.get("pretty-name") or os_row.get("pretty_name") or ""
        ).strip()
        if not os_name:
            base_name = str(os_row.get("name") or os_row.get("id") or "").strip()
            version = str(
                os_row.get("version") or os_row.get("version-id") or ""
            ).strip()
            os_name = " ".join(part for part in (base_name, version) if part)
        result["qga_os_name"] = os_name or None
        os_text = " ".join(
            str(os_row.get(key) or "")
            for key in ("id", "name", "pretty-name", "kernel-release", "version")
        ).lower()
        if "windows" in os_text or "microsoft" in os_text or "mswindows" in os_text:
            result["qga_os_system"] = "windows"
        elif os_text:
            result["qga_os_system"] = "linux"

    result["qga_ip_address"] = agent_interfaces_ip(data.get("interfaces"))
    result["qga_disks"] = _parse_qga_fsinfo(data.get("fsinfo"))
    return result


def guest_agent_fsinfo_summary(
    client: PveClient,
    node: str,
    gtype: str,
    vmid: int,
    config: dict | None = None,
) -> dict:
    """磁盘采集专用的 QGA 摘要:只调 get-fsinfo 一条命令。

    完整的 ``guest_agent_summary`` 会调 interfaces/osinfo/fsinfo 三条 QGA
    命令;fs 采集循环(guest_fs_metrics)只消费磁盘行——interfaces/osinfo
    仅服务于 SSH/WinRM 兑底的来源选择,绑定行同样承担——省 2/3 的 QGA
    调用(50 台 guest 一轮从 150 条收敛到 50 条)。agent 未启用或 fsinfo
    失败/无可用盘时 qga_available=False,调用方走绑定兑底,口径与完整
    摘要一致;顺带不再记录 QGA 地址(专门的 IP 刷新循环负责该缓存)。
    """
    result = {
        "qga_enabled": False,
        "qga_available": False,
        "qga_disks": [],
    }
    if gtype != "qemu":
        return result
    config = config if config is not None else client.guest_config(node, gtype, vmid)
    enabled = _agent_enabled_from_config(config)
    result["qga_enabled"] = enabled
    if not enabled:
        return result
    try:
        raw = client.guest_agent_fsinfo(node, gtype, vmid)
    except PveError:
        return result
    disks = _parse_qga_fsinfo(raw)
    if disks:
        result["qga_available"] = True
        result["qga_disks"] = disks
    return result
