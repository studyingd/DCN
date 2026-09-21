"""Docker 容器采集器——周期探测普通服务器及 PVE Guest 的全部容器。

Linux 走 SSH、Windows 走 WinRM,各发一条组合命令(docker info/ps/stats),
解析后整批刷新 device_containers + device_docker_status；PVE Guest 通过
``pve_guest_binding_id`` 归属，普通设备仍通过 ``device_id`` 归属。
没装 Docker 的服务器标记 nodocker(available=0),不算错误。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import (
    CONTAINERS_CONCURRENCY,
    CONTAINERS_INTERVAL,
    CONTAINERS_TIMEOUT,
)
from app.database import SessionLocal
from app.models.device import OPS_TARGET_TYPES, Device, is_windows_os
from app.models.device_container import DeviceContainer, DeviceDockerStatus
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.services.containers_parser import parse_docker_output
from app.services.crypto import decrypt

logger = logging.getLogger(__name__)

PVE_ID_FACTOR = 1_000_000


@dataclass
class ContainerTarget:
    """统一描述普通设备或 PVE 页面绑定的 guest。"""

    target_id: int
    kind: str
    entity_id: int
    name: str
    ip_address: str | None
    os_system: str | None
    username: str | None
    password: str | None
    ssh_key: str | None
    ssh_port: int
    winrm_port: int
    ssh_host_key: str | None
    device: Device | None = None
    binding: PveGuestBinding | None = None

    @property
    def is_windows(self) -> bool:
        return is_windows_os(self.os_system)


def pve_target_id(connection_id: int, vmid: int) -> int:
    """Stable negative ID shared with automation targets."""
    return -(connection_id * PVE_ID_FACTOR + vmid)


def decode_pve_target_id(target_id: int) -> tuple[int, int] | None:
    if target_id >= 0:
        return None
    connection_id, vmid = divmod(abs(target_id), PVE_ID_FACTOR)
    return (connection_id, vmid) if connection_id and vmid else None


# Linux:先判断 docker 是否存在,再取版本/容器清单/资源占用(stats 限时 10s)。
# stderr 错误经 mktemp 临时文件带回:固定路径会让并发探测(周期采集 × 操作后
# 重采/自愈验证)互踩同一台主机上的同一文件,错误文案错乱。
# 注意:操作后重采也必须跑全量(含 stats)——持久化是整批 delete+insert,
# 跳过 stats 会把整台主机的容器指标写成 NULL,直到下个 60s 周期才恢复
# (实测 docker stats --no-stream ≈2s,10s 只是超时上限,跳过它毫无意义)。
def _linux_probe_cmd() -> str:
    parts = [
        "DCN_ERR=$(mktemp /tmp/dcn-docker-err.XXXXXX);",
        "if command -v docker >/dev/null 2>&1; then",
        "echo '@@info'; if docker info --format '{{.ServerVersion}}' 2>$DCN_ERR; then echo '__ok=1'; else echo '__ok=0'; sed 's/^/__err=/' $DCN_ERR; fi;",
        "echo '@@ps'; if docker ps -a --format "
        "'{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.State}}|{{.Ports}}' 2>$DCN_ERR; then echo '__ok=1'; else echo '__ok=0'; sed 's/^/__err=/' $DCN_ERR; fi;",
        "echo '@@stats'; if timeout 10 docker stats --no-stream --format "
        "'{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' 2>$DCN_ERR; then echo '__ok=1'; else echo '__ok=0'; sed 's/^/__err=/' $DCN_ERR; fi;",
    ]
    parts.extend(["else echo '@@nodocker'; fi;", "rm -f $DCN_ERR"])
    return " ".join(parts)


LINUX_CMD = _linux_probe_cmd()


# Windows:同样逻辑,经 WinRM 执行 PowerShell。错误文本直接捕获在 $v 内存里
# (无临时文件,也就没有 Linux 那个并发互踩问题)。
def _windows_probe_ps() -> str:
    parts = [
        "if (Get-Command docker -ErrorAction SilentlyContinue) { ",
        "Write-Output '@@info'; try { $v = docker version --format '{{.Server.Version}}' 2>&1; if ($LASTEXITCODE -eq 0) { $v; '__ok=1' } else { '__ok=0'; '__err=' + ($v -join ' ') } } catch { '__ok=0'; '__err=' + $_.Exception.Message }; ",
        "Write-Output '@@ps'; try { $v = docker ps -a --format ",
        "'{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.State}}|{{.Ports}}' 2>&1; if ($LASTEXITCODE -eq 0) { $v; '__ok=1' } else { '__ok=0'; '__err=' + ($v -join ' ') } } catch { '__ok=0'; '__err=' + $_.Exception.Message }; ",
        "Write-Output '@@stats'; try { $v = docker stats --no-stream --format ",
        "'{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' 2>&1; if ($LASTEXITCODE -eq 0) { $v; '__ok=1' } else { '__ok=0'; '__err=' + ($v -join ' ') } } catch { '__ok=0'; '__err=' + $_.Exception.Message } ",
        "} else { Write-Output '@@nodocker' }",
    ]
    return "".join(parts)


WINDOWS_PS = _windows_probe_ps()


def _load_target(db, target_id: int) -> tuple[ContainerTarget | None, str | None]:
    """Load a target in the worker's own DB session.

    Positive IDs are ordinary managed devices (server/cloud_server/host).
    Negative IDs use the same
    ``connection_id + VMID`` encoding as automation and resolve to an enabled
    PVE guest binding.
    """
    if target_id > 0:
        device = (
            db.query(Device)
            .filter(Device.id == target_id, Device.type.in_(OPS_TARGET_TYPES))
            .first()
        )
        if device is None:
            return None, "设备不存在或不属于可纳管类型"
        password = (
            decrypt(device.remote_password_enc) if device.remote_password_enc else ""
        )
        ssh_key = (
            decrypt(device.remote_ssh_key_enc) if device.remote_ssh_key_enc else ""
        )
        return (
            ContainerTarget(
                target_id=target_id,
                kind="device",
                entity_id=device.id,
                name=device.name,
                ip_address=device.ip_address,
                os_system=device.os_system,
                username=device.remote_username,
                password=password,
                ssh_key=ssh_key,
                ssh_port=device.ssh_port or 22,
                winrm_port=device.winrm_port or 5985,
                ssh_host_key=device.ssh_host_key,
                device=device,
            ),
            None,
        )

    decoded = decode_pve_target_id(target_id)
    if not decoded:
        return None, "PVE 虚拟机目标无效"
    connection_id, vmid = decoded
    binding = (
        db.query(PveGuestBinding)
        .filter_by(connection_id=connection_id, vmid=vmid, enabled=1)
        .first()
    )
    if binding is None:
        return None, "PVE 虚拟机未配置或已停用运维接入"
    conn = db.query(PveConnection).filter_by(id=connection_id, enabled=1).first()
    if conn is None:
        return None, "PVE 平台不存在或已停用"
    password = decrypt(binding.password_enc) if binding.password_enc else ""
    ssh_key = decrypt(binding.ssh_key_enc) if binding.ssh_key_enc else ""
    return (
        ContainerTarget(
            target_id=target_id,
            kind="pve_guest",
            entity_id=binding.id,
            name=f"[{conn.name}] {binding.guest_type.upper()} {vmid}",
            ip_address=binding.ip_address,
            os_system=binding.os_system,
            username=binding.username,
            password=password,
            ssh_key=ssh_key,
            ssh_port=binding.ssh_port or 22,
            winrm_port=binding.winrm_port or 5985,
            ssh_host_key=binding.ssh_host_key,
            binding=binding,
        ),
        None,
    )


def load_container_target(db, target_id: int) -> ContainerTarget | None:
    """Public router helper; returns only enabled, valid targets."""
    target, _error = _load_target(db, target_id)
    return target


def _exec_probe(db, target: ContainerTarget) -> tuple[str, str | None]:
    if not target.ip_address or not target.username:
        raise RuntimeError("未绑定有效 IP 或用户名")
    if target.is_windows and not target.password:
        raise RuntimeError("Windows 容器采集需要 WinRM 密码")
    if not target.password and not target.ssh_key:
        raise RuntimeError("未绑定有效密码或 SSH 私钥")

    captured_key: str | None = None
    if target.is_windows:
        from app.services.winrm import run_powershell

        code, out, err = run_powershell(
            target.ip_address,
            target.username,
            target.password or "",
            _windows_probe_ps(),
            port=target.winrm_port,
            timeout=CONTAINERS_TIMEOUT,
        )
    else:
        from app.services.ssh import exec_ssh_command, open_ssh_client

        client, captured_key = open_ssh_client(
            target.ip_address,
            target.ssh_port,
            target.username,
            target.password or None,
            timeout=min(CONTAINERS_TIMEOUT, 15),
            pinned_key_b64=target.ssh_host_key or None,
            private_key=target.ssh_key or None,
            allow_tofu=not bool(target.ssh_host_key),
        )
        try:
            code, out, err = exec_ssh_command(
                client, _linux_probe_cmd(), timeout=CONTAINERS_TIMEOUT
            )
        finally:
            client.close()
    if captured_key and not target.ssh_host_key:
        if target.device is not None:
            target.device.ssh_host_key = captured_key
        if target.binding is not None:
            target.binding.ssh_host_key = captured_key
        db.commit()
    if code != 0 and not out:
        raise RuntimeError(f"探测失败(exit={code}): {err[:150]}")
    return out, captured_key


# 串行化写库锁:多线程并发对 device_containers 做 delete+insert 会触发 InnoDB
# 死锁(1213),且未回滚的连接泄漏会耗尽连接池、挂死整个后端。写库串行化可根除。
_persist_lock = threading.Lock()


def _persist_batch(items: list[tuple[ContainerTarget, dict, str | None]]) -> None:
    now = datetime.now(timezone.utc)
    with _persist_lock:
        db = SessionLocal()
        try:
            for target, parsed, error in items:
                if target.entity_id <= 0:
                    # The target may have been deleted/disabled after the
                    # worker queried it; there is no valid owner row to save.
                    continue
                owner_filter = (
                    {"device_id": target.entity_id}
                    if target.kind == "device"
                    else {"pve_guest_binding_id": target.entity_id}
                )
                st = db.query(DeviceDockerStatus).filter_by(**owner_filter).first()
                if st is None:
                    st = DeviceDockerStatus(**owner_filter)
                    db.add(st)
                st.available = 1 if parsed.get("available") else 0
                st.version = parsed.get("version")
                st.container_count = len(parsed.get("containers", []))
                st.last_error = error
                st.command_status_json = json.dumps(
                    parsed.get("commands", {}), ensure_ascii=False
                )
                st.updated_at = now
                db.query(DeviceContainer).filter_by(**owner_filter).delete()
                db.bulk_insert_mappings(
                    DeviceContainer,
                    [
                        {
                            **owner_filter,
                            "container_id": c["container_id"],
                            "name": c["name"],
                            "image": c["image"],
                            "state": c["state"],
                            "status": c["status"],
                            "ports": c["ports"],
                            "cpu_pct": c["cpu_pct"],
                            "mem_used_mb": c["mem_used_mb"],
                            "mem_limit_mb": c["mem_limit_mb"],
                            "mem_pct": c["mem_pct"],
                            "updated_at": now,
                        }
                        for c in parsed.get("containers", [])
                    ],
                )
            db.commit()
        except Exception:
            # 死锁等写库异常必须回滚,否则连接带着悬空事务回池,污染连接池
            db.rollback()
            raise
        finally:
            db.close()


def _collect_probe(target_id: int) -> tuple[ContainerTarget, dict, str | None]:
    db = SessionLocal()
    try:
        target, target_error = _load_target(db, target_id)
        if target is None:
            # Keep an ephemeral target so the batch can record a useful error
            # without losing the target ID returned by the scheduler.
            target = ContainerTarget(
                target_id=target_id,
                kind="device" if target_id > 0 else "pve_guest",
                entity_id=0,
                name=str(target_id),
                ip_address=None,
                os_system=None,
                username=None,
                password=None,
                ssh_key=None,
                ssh_port=22,
                winrm_port=5985,
                ssh_host_key=None,
            )
            return (
                target,
                {"available": False, "version": None, "containers": []},
                target_error or "目标不存在",
            )
        try:
            raw, _captured_key = _exec_probe(db, target)
            parsed = parse_docker_output(raw)
            errors = [
                f"{name}: {item.get('error')}"
                for name, item in parsed.get("commands", {}).items()
                if not item.get("success") and item.get("error")
            ]
            return target, parsed, "; ".join(errors)[:500] or None
        except Exception as exc:
            logger.info("Docker probe failed for %s: %s", target.name, exc)
            return (
                target,
                {"available": False, "version": None, "containers": []},
                str(exc)[:200],
            )
    finally:
        db.close()


def _collect_one(target_id: int) -> None:
    """兼容控制接口的单设备采集并持久化(全量含 stats,见 _linux_probe_cmd 注释)。"""
    _persist_batch([_collect_probe(target_id)])


# 操作后重采的去重闸门:同一目标已有在采探测时不再起新的,
# 防连点操作对同一台主机并发开多条 SSH 探测(周期采集不走这里)。
_collect_one_inflight: set[int] = set()
_collect_one_lock = threading.Lock()


def _spawn_collect_one(target_id: int) -> None:
    """后台线程跑单机全量采集(操作后刷新/绑定保存触发),同目标去重。"""
    with _collect_one_lock:
        if target_id in _collect_one_inflight:
            return
        _collect_one_inflight.add(target_id)

    def _run() -> None:
        try:
            _collect_one(target_id)
        finally:
            with _collect_one_lock:
                _collect_one_inflight.discard(target_id)

    threading.Thread(target=_run, daemon=True).start()


def _fetch_targets() -> list[int]:
    """在执行器线程内自建会话查询在线目标——会话生命周期不跨越 await 取消边界。"""
    db = SessionLocal()
    try:
        rows = (
            db.query(Device.id)
            .filter(Device.type.in_(OPS_TARGET_TYPES), Device.status == "online")
            .all()
        )
        targets = [r[0] for r in rows]
        bindings = (
            db.query(PveGuestBinding.connection_id, PveGuestBinding.vmid)
            .join(PveConnection, PveConnection.id == PveGuestBinding.connection_id)
            .filter(PveGuestBinding.enabled == 1, PveConnection.enabled == 1)
            .all()
        )
        targets.extend(pve_target_id(conn_id, vmid) for conn_id, vmid in bindings)
        return targets
    finally:
        db.close()


async def _collect_cycle() -> None:
    loop = asyncio.get_running_loop()
    # 会话完全在执行器线程内开/关;即便此 await 被取消,线程也会自行完成 close,
    # 不会把半开的会话留在事件循环侧导致 IllegalStateChangeError。
    targets = await loop.run_in_executor(None, _fetch_targets)

    if not targets:
        return

    def _run_pool() -> list[tuple[ContainerTarget, dict, str | None]]:
        results: list[tuple[ContainerTarget, dict, str | None]] = []
        with ThreadPoolExecutor(max_workers=CONTAINERS_CONCURRENCY) as pool:
            futures = [pool.submit(_collect_probe, did) for did in targets]
            for f in as_completed(futures):
                try:
                    results.append(f.result())
                except Exception:
                    logger.exception("Container collect worker failed")
        return results

    results = await loop.run_in_executor(None, _run_pool)
    if results:
        await loop.run_in_executor(None, _persist_batch, results)
        from app.services.alerts import evaluate_container_alerts

        await loop.run_in_executor(None, evaluate_container_alerts)


async def run_containers_loop():
    logger.info(
        "Docker container collector started (interval=%ds, concurrency=%d)",
        CONTAINERS_INTERVAL,
        CONTAINERS_CONCURRENCY,
    )
    while True:
        try:
            await _collect_cycle()
        except asyncio.CancelledError:
            raise  # 正常关闭:让 CancelledError 上抛,交由 lifespan 取消
        except Exception:
            logger.exception("Container collection cycle failed")
        await asyncio.sleep(CONTAINERS_INTERVAL)


def _exec_target_command(
    target: ContainerTarget,
    command: str,
    *,
    timeout: int = CONTAINERS_TIMEOUT,
) -> tuple[int, str, str]:
    if not target.ip_address or not target.username:
        raise RuntimeError("未绑定有效 IP 或用户名")
    if target.is_windows and not target.password:
        raise RuntimeError("Windows 容器操作需要 WinRM 密码")
    if target.is_windows:
        from app.services.winrm import run_powershell

        return run_powershell(
            target.ip_address,
            target.username,
            target.password or "",
            command,
            port=target.winrm_port,
            timeout=timeout,
        )
    from app.services.ssh import exec_ssh_command, open_ssh_client

    client, captured_key = open_ssh_client(
        target.ip_address,
        target.ssh_port,
        target.username,
        target.password or None,
        timeout=min(timeout, 15),
        pinned_key_b64=target.ssh_host_key or None,
        private_key=target.ssh_key or None,
        allow_tofu=not bool(target.ssh_host_key),
    )
    try:
        result = exec_ssh_command(client, command, timeout=timeout)
    finally:
        client.close()
    if captured_key and not target.ssh_host_key:
        db = SessionLocal()
        try:
            if target.kind == "device":
                device = db.query(Device).filter_by(id=target.entity_id).first()
                if device:
                    device.ssh_host_key = captured_key
            else:
                binding = (
                    db.query(PveGuestBinding).filter_by(id=target.entity_id).first()
                )
                if binding:
                    binding.ssh_host_key = captured_key
            db.commit()
        finally:
            db.close()
    return result


def run_container_action(
    target: ContainerTarget, action: str, name: str
) -> tuple[int, str, str]:
    """对指定容器执行生命周期动作(start/stop/restart)。

    action 与 name 均已在路由层白名单校验,命令拼接安全。
    """
    command = f"docker {action} {name}"
    return _exec_target_command(target, command)


def remove_container(
    target: ContainerTarget, name: str, *, keep_volumes: bool
) -> tuple[int, str, str]:
    """删除容器。``keep_volumes`` 语义:

    - True  → ``docker rm``:保留全部卷。匿名卷失去属主后仍在磁盘上,
      但已无名字可寻,基本等于孤儿数据——"保留"的实用价值主要是心里预期对齐。
    - False → ``docker rm -v``:连匿名卷一起删。**具名卷与 bind mount
      无论如何都不会被 -v 碰到**(docker 只删它自己起的匿名卷)。

    单次 exec 完成 stop+rm(两次独立往返要各握一次 SSH 手手,慢一倍);
    `;` 保证 stop 失败不阻塞 rm——容器可能本来就停了,stop 对已停容器
    报错属噪音,rm 的结果才是用户要的。stop 的输出全部丢弃。
    """
    flag = "" if keep_volumes else " -v"
    command = f"docker stop {name} >/dev/null 2>&1; docker rm{flag} {name}"
    code, out, err = _exec_target_command(target, command)
    if code == 0:
        note = "容器已删除" + ("(匿名卷已保留)" if keep_volumes else "(匿名卷已删除)")
        return 0, note, ""
    return (
        code,
        (out or ""),
        (err or f"docker rm{flag} {name} 执行失败"),
    )


# ── 日志专用 SSH 连接复用缓存(Linux) ──
# 每次取日志若都新建 SSH 连接,握手要 1~3 秒;日志弹窗还有 5s 自动刷新,反复重连体验很差。
# 这里按 device_id 缓存一条 paramiko 连接(TTL 内且 transport 存活就复用),
# 让"查看日志/自动刷新"近乎秒开。失败时丢弃缓存连接并新建重试一次。
_LOG_SSH_TTL = 50.0
_LOG_CONNECT_TIMEOUT = 8
_LOG_BANNER_TIMEOUT = 10
# 缓存的 SSH 连接数上限。TTL 只在「同一设备再次被查看」时才惰性检查,不设上限
# 的话每台被看过一次的设备都会永久占着一条 sshd 会话(耗 MaxSessions/fd)。
_LOG_MAX_CLIENTS = 32
_log_clients: dict[int, tuple[object, float]] = {}
_log_clients_guard = threading.Lock()
_log_device_locks: dict[int, threading.Lock] = {}


def _evict_log_clients_locked(now: float) -> None:
    """清掉过期 + 超出上限的连接;调用方必须已持有 ``_log_clients_guard``。"""
    for did, entry in list(_log_clients.items()):
        if (now - entry[1]) >= _LOG_SSH_TTL or not _alive(entry[0]):
            _log_clients.pop(did, None)
            try:
                entry[0].close()
            except Exception:
                pass
    while len(_log_clients) >= _LOG_MAX_CLIENTS:
        oldest_did = min(_log_clients, key=lambda k: _log_clients[k][1])
        entry = _log_clients.pop(oldest_did, None)
        if entry is None:
            break
        try:
            entry[0].close()
        except Exception:
            pass


def _log_device_lock(device_id: int) -> threading.Lock:
    with _log_clients_guard:
        lock = _log_device_locks.get(device_id)
        if lock is None:
            lock = threading.Lock()
            _log_device_locks[device_id] = lock
        return lock


def _open_log_ssh(target: ContainerTarget):
    from app.services.ssh import open_ssh_client

    client, captured_key = open_ssh_client(
        target.ip_address or "",
        target.ssh_port,
        target.username or "",
        target.password or None,
        timeout=_LOG_CONNECT_TIMEOUT,
        banner_timeout=_LOG_BANNER_TIMEOUT,
        pinned_key_b64=target.ssh_host_key or None,
        private_key=target.ssh_key or None,
        allow_tofu=not bool(target.ssh_host_key),
    )
    if captured_key and not target.ssh_host_key:
        db = SessionLocal()
        try:
            if target.kind == "device":
                entity = db.query(Device).filter_by(id=target.entity_id).first()
            else:
                entity = (
                    db.query(PveGuestBinding).filter_by(id=target.entity_id).first()
                )
            if entity:
                entity.ssh_host_key = captured_key
                db.commit()
        finally:
            db.close()
    return client


def _alive(client) -> bool:
    try:
        transport = client.get_transport()
        return transport is not None and transport.is_active()
    except Exception:
        return False


def _fetch_logs_linux_cached(
    target: ContainerTarget, command: str
) -> tuple[int, str, str]:
    from app.services.ssh import exec_ssh_command

    lock = _log_device_lock(target.target_id)
    with lock:
        now = time.monotonic()
        client = None
        with _log_clients_guard:
            entry = _log_clients.get(target.target_id)
            if entry and (now - entry[1]) < _LOG_SSH_TTL and _alive(entry[0]):
                client = entry[0]
                _log_clients[target.target_id] = (client, now)
        if client is None:
            client = _open_log_ssh(target)
            with _log_clients_guard:
                _evict_log_clients_locked(now)
                old = _log_clients.get(target.target_id)
                if old is not None:
                    try:
                        old[0].close()
                    except Exception:
                        pass
                _log_clients[target.target_id] = (client, now)
        try:
            return exec_ssh_command(client, command, timeout=15)
        except Exception:
            # 缓存连接可能已失效:丢弃并新建重试一次
            try:
                client.close()
            except Exception:
                pass
            with _log_clients_guard:
                _log_clients.pop(target.target_id, None)
            client = _open_log_ssh(target)
            with _log_clients_guard:
                _evict_log_clients_locked(time.monotonic())
                _log_clients[target.target_id] = (client, time.monotonic())
            return exec_ssh_command(client, command, timeout=15)


def fetch_container_logs(
    target: ContainerTarget, name: str, tail: int
) -> tuple[int, str, str]:
    """抓取容器最近 tail 行日志(合并 stderr)。name/tail 已在路由层校验。

    Linux 复用 SSH 连接提速;Windows 走 WinRM(无状态,逐次请求)。
    """
    tail = max(1, min(int(tail), 5000))
    command = f"docker logs --tail {tail} {name} 2>&1"
    if target.is_windows:
        return _exec_target_command(target, command, timeout=15)
    return _fetch_logs_linux_cached(target, command)
