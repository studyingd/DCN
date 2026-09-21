"""Server metrics collector — periodic CPU/mem/disk/load/network sampling.

每 METRICS_INTERVAL 秒对全部在线纳管设备(server/cloud_server/host)采集一轮:
  * Linux  → SSH 一条组合命令(/proc + df,会话内双采样 CPU,自包含);
  * Windows → WinRM 一条 PowerShell(CIM 煮熟计数器,瞬时直读);
  * 解析结果更新内存最新值缓存(get_latest_metrics,列表页零 DB 读取),
    并写入 device_metric_samples 历史表(详情页曲线);
  * Windows 设备顺带用 Win32_OperatingSystem.Caption 回写精确 os_system;
  * 每天清理一次 METRICS_RETENTION_DAYS 之前的历史行。

连接与会话生命周期:
  * Linux 采集走独立 SshPool 复用 transport(仅池 miss 时握手,常规轮次
    只开一条 exec channel);host key 首次 TOFU 在池工厂里用短会话持久化,
    已 pin 的设备连接时强制比对,不碰 DB。
  * 设备字段/凭据用短会话读出即关,采集期间(网络 IO 最长 METRICS_TIMEOUT)
    不持有 DB 会话——此前会话全程挂着,并发 METRICS_CONCURRENCY 轮会把
    连接池占满;OS 自愈回写在采集成功后另起短会话。

paramiko / pywinrm 均为阻塞调用,采集在线程池中执行(同 scheduler.py 模式)。
"""

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from app.config import (
    METRICS_CONCURRENCY,
    METRICS_INTERVAL,
    METRICS_RETENTION_DAYS,
    METRICS_SSH_POOL_SIZE,
    METRICS_TIMEOUT,
)
from app.database import SessionLocal
from app.models.device import OPS_TARGET_TYPES, Device
from app.models.device_metric_sample import DeviceMetricSample
from app.services.alerts import (
    close_orphaned_alert_events,
    evaluate_alerts,
    evaluate_host_status_alerts,
)
from app.services.device_credentials import resolve_credentials
from app.services.metrics_parser import parse_linux_metrics, parse_windows_metrics
from app.services.ssh_pool import SshPool

logger = logging.getLogger(__name__)

# SSH transport 复用池(独立于文件管理器的 ssh_pool 单例):
# 采集是稳态周期流量(每 METRICS_INTERVAL 一轮),池 TTL 盖过「周期+采集耗时」
# 即可常温复用;文件管理是用户触发的突发流量且上限 32,混用会互相 LRU 驱逐。
# 池 miss 时才握手(实测单次握手+认证 ~164ms),常规轮次只开一条 exec channel。
_ssh_pool = SshPool(
    idle_ttl=max(180.0, METRICS_INTERVAL * 2.0),
    max_size=METRICS_SSH_POOL_SIZE,
)

# 每台设备最新一行样本(限 _FALLBACK_MAX_AGE_SECONDS 内),DB 回退读取的数据源。
_LATEST_SAMPLE_QUERY = (
    select(DeviceMetricSample)
    .where(
        DeviceMetricSample.id.in_(
            select(func.max(DeviceMetricSample.id)).group_by(
                DeviceMetricSample.device_id
            )
        )
    )
    .order_by(DeviceMetricSample.device_id)
)

# ── 采集命令(输出契约见 metrics_parser 模块 docstring)──

# Linux:@@section 分节;CPU 在会话内 sleep 1 双采样求差,不依赖历史基线。
_LINUX_CMD = (
    "echo '@@cpu1'; grep '^cpu ' /proc/stat; sleep 1; "
    "echo '@@cpu2'; grep '^cpu ' /proc/stat; "
    "echo '@@mem'; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; "
    "echo '@@disk'; (df -B1 -P 2>/dev/null || df -P); "
    "echo '@@diskio'; cat /proc/diskstats; "
    "echo '@@load'; cat /proc/loadavg; "
    "echo '@@uptime'; cat /proc/uptime; "
    "echo '@@net'; cat /proc/net/dev; "
    # 读 PRETTY_NAME 用于 OS 自愈回写(对齐 Windows 的 Caption 行为)。
    # 取 .DEFAULT 兼容 systemd,缺失时回退到 /etc/os-release 本身。
    "echo '@@osrel'; grep -E '^PRETTY_NAME=' /etc/os-release 2>/dev/null"
)

# Windows:key=value 行;PerfFormattedData 是煮熟计数器,单次直读无需采样等待。
_WINDOWS_PS = r"""
$os = Get-CimInstance Win32_OperatingSystem
$mem = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
$cpu = Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'"
Write-Output "cpu_pct=$($cpu.PercentProcessorTime)"
Write-Output "mem_total_mb=$([math]::Round($os.TotalVisibleMemorySize/1KB))"
Write-Output "mem_free_mb=$([math]::Round($os.FreePhysicalMemory/1KB))"
Write-Output "mem_available_mb=$([math]::Round($mem.AvailableMBytes))"
Write-Output "uptime_sec=$([int]((Get-Date) - $os.LastBootUpTime).TotalSeconds)"
Write-Output "caption=$($os.Caption)"
Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
  Write-Output "disk=$($_.DeviceID),$($_.Size),$($_.Size-$_.FreeSpace)"
}
Get-CimInstance Win32_PerfFormattedData_Tcpip_NetworkInterface | ForEach-Object {
  $n = $_.Name -replace ',',' '
  Write-Output "netif=$n,$($_.BytesReceivedPersec),$($_.BytesSentPersec)"
}
Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk |
  Where-Object {$_.Name -ne '_Total'} | ForEach-Object {
    $n = $_.Name -replace ',',' '
    Write-Output "diskio=$n,$($_.DiskReadBytesPersec),$($_.DiskWriteBytesPersec),$($_.PercentDiskTime)"
  }
""".strip()

# ── 运行状态 ──

_latest: dict[int, dict] = {}
# Linux 网卡计数器跨周期求差的基线:device_id -> (monotonic_ts, rx_bytes, tx_bytes)
_prev_net: dict[int, tuple[float, int, int]] = {}
# Linux 磁盘 IO 计数器基线:device_id -> (monotonic_ts, read_bytes, write_bytes)
_prev_io: dict[int, tuple[float, int, int]] = {}
_last_retention_day: str | None = None

# ── 读取侧的 DB 回退 ──
# 多副本部署中只有 leader 实例跑采集;非 leader 进程的 _latest 永远是空的,
# 指标页会显示"无数据",尽管数据库里样本一直在写(README 明说支持这种拓扑)。
# 读取路径上,当 _latest 里没有该设备(或条目早于 _FALLBACK_MAX_AGE_SECONDS,
# 即本进程刚重启、采集循环还没跑完第一轮)时,回退查 device_metric_samples
# 每台设备最新一行,结果做 _FALLBACK_TTL_SECONDS 的短 TTL 缓存,防止列表页
# 10s 轮询直接打到数据库。leader 进程 _latest 永远是新鲜的,不会走这条路径。
_FALLBACK_TTL_SECONDS = 10.0
_FALLBACK_MAX_AGE_SECONDS = METRICS_INTERVAL * 2
_fallback_cache: dict[int, dict | None] = {}
_fallback_loaded_at = 0.0


def _fallback_from_db() -> dict[int, dict]:
    """从 device_metric_samples 读每台设备最新一行,映射成 _latest 条目形状。

    纳管设备里「绑了凭据但最近无新鲜样本」的行也补一条 available=False 的
    合成条目:非 leader 进程(或采集刚停摆)时,列表页的剔除规则靠 available
    标志工作,缺条目会退化成「还没采集过,给一次机会」——失败设备以含糊的
    「不可用」挂在列表里,而不是带明确原因或被剔除。合成 error 文案说明
    "最近无采集数据",把「WinRM 不可达/采集失败」与「真没采过」区分开。
    """
    db = SessionLocal()
    try:
        rows = db.execute(_LATEST_SAMPLE_QUERY).scalars().all()
        managed = (
            db.query(
                Device.id,
                Device.type,
                Device.os_system,
                Device.remote_username,
                Device.remote_password_enc,
                Device.remote_ssh_key_enc,
            )
            .filter(Device.type.in_(OPS_TARGET_TYPES))
            .all()
        )
    except Exception:
        logger.exception("metrics latest fallback query failed")
        return {}
    finally:
        db.close()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_FALLBACK_MAX_AGE_SECONDS)
    out: dict[int, dict] = {}
    for row in rows:
        if row.ts is None or row.ts < cutoff:
            continue  # 最新样本已超龄(采集停摆),不算"有数据"
        did = int(row.device_id)
        out[did] = {
            "device_id": did,
            "available": True,
            "error": None,
            "fetched_at": row.ts.isoformat().replace("+00:00", "Z"),
            "source": None,
            "cpu_pct": row.cpu_pct,
            "mem_pct": row.mem_pct,
            "mem_used_mb": row.mem_used_mb,
            "mem_total_mb": row.mem_total_mb,
            "disk_max_pct": row.disk_max_pct,
            "disks": json.loads(row.disks_json or "[]"),
            "load1": row.load1,
            "load5": row.load5,
            "load15": row.load15,
            "uptime_sec": row.uptime_sec,
            "net_rx_bps": row.net_rx_bps,
            "net_tx_bps": row.net_tx_bps,
            "disk_read_bps": row.disk_read_bps,
            "disk_write_bps": row.disk_write_bps,
        }
    # 有凭据但没有新鲜样本 → available=False 合成条目(与 leader 进程里
    # _unavailable(...) 的形状对齐,_summary 原样透出 error 文案)。
    for (
        did,
        _type,
        os_system,
        username,
        password_enc,
        ssh_key_enc,
    ) in managed:
        if did in out:
            continue
        if not username or not (password_enc or ssh_key_enc):
            continue  # 未绑凭据的设备由列表页自己的规则处理
        if "windows" in (os_system or "").lower() and not password_enc:
            continue  # Windows 缺密码同上,不是"采集失败"
        out[did] = _unavailable(did, "最近无采集数据(采集实例未运行或目标不可达)")
    return out


def get_latest_metrics() -> dict[int, dict]:
    merged = {did: dict(m) for did, m in _latest.items()}
    if not merged or _needs_fallback(merged):
        merged.update({k: v for k, v in _load_fallback().items() if k not in merged})
    return merged


def get_device_metrics(device_id: int) -> dict | None:
    m = _latest.get(device_id)
    if m is not None and _fresh(m):
        return dict(m)
    fallback = _load_fallback().get(device_id)
    if fallback is not None:
        return dict(fallback)
    return dict(m) if m else None


def _fresh(entry: dict) -> bool:
    fetched = entry.get("fetched_at")
    if not fetched:
        return False
    try:
        ts = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
    except ValueError:
        return True  # 解析不了就别回退,宁可显示缓存值
    return datetime.now(timezone.utc) - ts <= timedelta(
        seconds=_FALLBACK_MAX_AGE_SECONDS
    )


def _needs_fallback(merged: dict[int, dict]) -> bool:
    return any(not _fresh(m) for m in merged.values())


def _load_fallback() -> dict[int, dict]:
    global _fallback_loaded_at
    now = time.monotonic()
    if now - _fallback_loaded_at > _FALLBACK_TTL_SECONDS:
        _fallback_cache.clear()
        _fallback_loaded_at = now
        _fallback_cache.update(_fallback_from_db())
    return _fallback_cache


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _unavailable(device_id: int, error: str) -> dict:
    return {
        "device_id": device_id,
        "available": False,
        "error": error,
        "fetched_at": None,
        "source": None,
        "cpu_pct": None,
        "mem_pct": None,
        "mem_used_mb": None,
        "mem_total_mb": None,
        "disk_max_pct": None,
        "disks": [],
        "load1": None,
        "load5": None,
        "load15": None,
        "uptime_sec": None,
        "net_rx_bps": None,
        "net_tx_bps": None,
        "disk_read_bps": None,
        "disk_write_bps": None,
    }


def _fetch_targets() -> list[tuple[int, str]]:
    """全部纳管设备的 (id, status)——离线设备也要在缓存里占位。

    在执行器线程内自建会话查询,会话生命周期不跨越 await 取消边界。
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(Device.id, Device.status)
            .filter(Device.type.in_(OPS_TARGET_TYPES))
            .all()
        )
        return [(r[0], r[1] or "offline") for r in rows]
    finally:
        db.close()


def _resolve_creds(device: Device) -> tuple[str, str, str] | None:
    # 只读 device 上已加密的凭据字段并解密，不需要 DB 会话（对比 _collect_linux
    # 需要 db 才能建 SSH 通道）。
    if not device.remote_username:
        return None
    username, password, ssh_key = resolve_credentials(device)
    if not username or (not password and not ssh_key):
        return None
    if device.is_windows and not password:
        return None
    return username, password, ssh_key


def _collect_linux(device: Device, username: str, password: str, ssh_key: str) -> str:
    """Linux 指标采集:SSH transport 经 _ssh_pool 复用。

    ``device`` 可来自已关闭的会话(字段已物化,``connect_device`` 在 db=None
    时不会碰数据库);pinned host key 存在时工厂仅做「连接+比对」(不匹配即
    HostKeyMismatchError),首次 TOFU 的 host key 持久化在工厂里用独立短会话
    完成——会话只在握手期间持有,exec 阶段不占 DB 连接。
    """
    from app.services.ssh import connect_device, exec_ssh_command

    key = _ssh_pool.key(
        device.ip_address, device.ssh_port or 22, username, password, ssh_key or None
    )

    def _factory():
        if device.ssh_host_key:
            # 已 pin:无需会话,连接时强制比对 host key
            client, _key = connect_device(
                device,
                username,
                password,
                db=None,
                timeout=min(METRICS_TIMEOUT, 15),
                private_key=ssh_key or None,
            )
            return client
        # 首次 TOFU:短会话内连接并持久化 host key(connect_device 内部 commit)
        db = SessionLocal()
        try:
            dev = db.query(Device).filter(Device.id == device.id).first()
            if dev is None:
                raise RuntimeError("设备不存在")
            client, _key = connect_device(
                dev,
                username,
                password,
                db=db,
                timeout=min(METRICS_TIMEOUT, 15),
                private_key=ssh_key or None,
            )
            return client
        finally:
            db.close()

    client, _created = _ssh_pool.acquire(key, _factory)
    _ssh_pool.mark_in_use(key)
    try:
        code, out, err = exec_ssh_command(client, _LINUX_CMD, timeout=METRICS_TIMEOUT)
        _ssh_pool.touch(key)
    except Exception:
        # 传输层异常:transport 可能已僵死,作废让下一轮重建
        _ssh_pool.discard(key)
        raise
    finally:
        _ssh_pool.release(key)
    if code != 0 and not out:
        raise RuntimeError(f"SSH 执行失败(exit={code}): {err[:150]}")
    return out


def _collect_windows(device: Device, username: str, password: str) -> str:
    from app.services.winrm import run_on_device

    code, out, err = run_on_device(
        device, username, password, _WINDOWS_PS, timeout=METRICS_TIMEOUT
    )
    if code != 0 and not out:
        raise RuntimeError(f"WinRM 执行失败(exit={code}): {err[:150]}")
    return out


def _counter_rates(
    prev: dict[int, tuple[float, int, int]],
    device_id: int,
    a_bytes: int | None,
    b_bytes: int | None,
) -> tuple[float | None, float | None]:
    """累计计数器跨周期求差(网卡/磁盘 IO 共用)。

    首轮无基线返回 (None, None);计数器回退(设备重启)重置基线。
    """
    if a_bytes is None or b_bytes is None:
        return None, None
    now = time.monotonic()
    baseline = prev.get(device_id)
    prev[device_id] = (now, a_bytes, b_bytes)
    if not baseline:
        return None, None
    dt = now - baseline[0]
    if dt <= 0:
        return None, None
    d_a = a_bytes - baseline[1]
    d_b = b_bytes - baseline[2]
    return (d_a / dt if d_a >= 0 else None), (d_b / dt if d_b >= 0 else None)


def _selfheal_os(device_id: int, caption: str, name: str) -> None:
    """采集成功后回写精确 OS 名(独立短会话;失败只记日志,不丢指标条目)。"""
    db = SessionLocal()
    try:
        dev = db.query(Device).filter(Device.id == device_id).first()
        if dev is not None and dev.os_system != caption:
            dev.os_system = caption
            db.commit()
            logger.info("Device %s os_system updated: %s", name, caption)
    except Exception:
        db.rollback()
        logger.exception("os_system self-heal failed")
    finally:
        db.close()


def _collect_one(device_id: int) -> dict:
    """采集单台设备(worker 线程内执行)。

    会话生命周期:设备字段/凭据用短会话读出即关,采集期间(SSH/WinRM 网络
    IO,最长 METRICS_TIMEOUT)不持有任何 DB 会话;OS 自愈回写在采集成功后
    另起短会话(_selfheal_os)。
    """
    db = SessionLocal()
    try:
        device = db.query(Device).filter(Device.id == device_id).first()
        if device is None:
            return _unavailable(device_id, "设备不存在")
        if not device.ip_address:
            return _unavailable(device_id, "未配置 IP 地址")

        creds = _resolve_creds(device)
        if creds is None:
            return _unavailable(device_id, "未绑定有效凭据")
        username, password, ssh_key = creds
        is_win = device.is_windows
    finally:
        db.close()

    try:
        if is_win:
            raw = _collect_windows(device, username, password)
            parsed = parse_windows_metrics(raw)
        else:
            raw = _collect_linux(device, username, password, ssh_key)
            parsed = parse_linux_metrics(raw)
    except Exception as exc:
        logger.info("Metrics collect failed for %s: %s", device.name, exc)
        return _unavailable(device_id, str(exc)[:200])

    if not parsed.get("ok"):
        return _unavailable(device_id, "指标解析失败")

    # Windows 计数器直读速率;Linux 累计计数器跨周期求差
    if is_win:
        rx_bps = parsed.get("net_rx_bps")
        tx_bps = parsed.get("net_tx_bps")
        io_read_bps = parsed.get("disk_read_bps")
        io_write_bps = parsed.get("disk_write_bps")
    else:
        rx_bps, tx_bps = _counter_rates(
            _prev_net,
            device_id,
            parsed.get("net_rx_bytes"),
            parsed.get("net_tx_bytes"),
        )
        io_read_bps, io_write_bps = _counter_rates(
            _prev_io,
            device_id,
            parsed.get("diskio_read_bytes"),
            parsed.get("diskio_write_bytes"),
        )

    # OS 自愈:精确版本名(Windows Caption / Linux PRETTY_NAME)在采集成功时回写。
    # 覆盖空值、笼统家族名("windows"/"linux")、粗识别串("Windows (...)"/
    # "Linux (OpenSSH) ...");手写描述则尊重,不覆盖。
    caption = parsed.get("os_caption")
    if caption:
        cur_os = (device.os_system or "").strip().lower()
        if is_win:
            generic = cur_os in ("", "windows")
            coarse = cur_os.startswith(("windows (", "microsoft windows [version"))
        else:
            generic = cur_os in ("", "linux")
            coarse = cur_os.startswith(("linux (openssh", "linux (dropbear", "linux ("))
        if generic or coarse:
            _selfheal_os(device_id, caption, device.name)

    entry = {
        "device_id": device_id,
        "available": True,
        "error": None,
        "fetched_at": _iso_now(),
        "source": "winrm" if is_win else "ssh",
        "cpu_pct": parsed["cpu_pct"],
        "mem_pct": parsed["mem_pct"],
        "mem_used_mb": parsed["mem_used_mb"],
        "mem_total_mb": parsed["mem_total_mb"],
        "disk_max_pct": parsed["disk_max_pct"],
        "disks": parsed["disks"],
        "load1": parsed["load1"],
        "load5": parsed["load5"],
        "load15": parsed["load15"],
        "uptime_sec": parsed["uptime_sec"],
        "net_rx_bps": rx_bps,
        "net_tx_bps": tx_bps,
        "disk_read_bps": io_read_bps,
        "disk_write_bps": io_write_bps,
    }

    entry["_sample"] = {
        "device_id": device_id,
        "ts": datetime.now(timezone.utc),
        "cpu_pct": entry["cpu_pct"],
        "mem_pct": entry["mem_pct"],
        "mem_used_mb": entry["mem_used_mb"],
        "mem_total_mb": entry["mem_total_mb"],
        "disk_max_pct": entry["disk_max_pct"],
        "disks_json": json.dumps(entry["disks"], ensure_ascii=False),
        "load1": entry["load1"],
        "load5": entry["load5"],
        "load15": entry["load15"],
        "uptime_sec": entry["uptime_sec"],
        "net_rx_bps": rx_bps,
        "net_tx_bps": tx_bps,
        "disk_read_bps": io_read_bps,
        "disk_write_bps": io_write_bps,
    }
    return entry


# 保留清理每批删除行数:ts 无独立索引(只有 (device_id,ts) 复合索引),一次性
# 大 DELETE 是全表扫+长事务+大 undo;分批短事务把锁范围与回滚段压力摊开。
_RETENTION_BATCH_ROWS = 5000


def _cleanup_retention() -> None:
    """每天一次,删除 METRICS_RETENTION_DAYS 之前的历史行(分批提交)。"""
    global _last_retention_day
    today = datetime.now(timezone.utc).date().isoformat()
    if _last_retention_day == today:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=METRICS_RETENTION_DAYS)
    total = 0
    db = SessionLocal()
    try:
        while True:
            # 先取本批主键再按主键删:id 与时间正相关(自增),老行集中在低
            # id 段,扫描即命中;DELETE IN (ids) 走主键,批间锁范围小。
            ids = (
                db.execute(
                    select(DeviceMetricSample.id)
                    .where(DeviceMetricSample.ts < cutoff)
                    .limit(_RETENTION_BATCH_ROWS)
                )
                .scalars()
                .all()
            )
            if not ids:
                break
            result = db.execute(
                delete(DeviceMetricSample).where(DeviceMetricSample.id.in_(ids))
            )
            db.commit()
            deleted = result.rowcount or 0
            total += deleted
            # 本批已不满:没有更多旧行了;rowcount=0(异常并发场景)则止损退出,
            # 防止同一批 id 反复选中变成死循环。
            if deleted == 0 or len(ids) < _RETENTION_BATCH_ROWS:
                break
        _last_retention_day = today
        if total:
            logger.info("Metrics retention: deleted %d old samples", total)
    except Exception:
        logger.exception("Metrics retention cleanup failed")
        db.rollback()
    finally:
        db.close()


async def _collect_cycle_unlocked() -> None:
    global _latest
    loop = asyncio.get_running_loop()

    # 会话完全在执行器线程内开/关,避免取消时留下半开会话(IllegalStateChangeError)
    targets = await loop.run_in_executor(None, _fetch_targets)

    entries: dict[int, dict] = {}
    online_ids: list[int] = []
    for device_id, status in targets:
        if status == "online":
            online_ids.append(device_id)
        else:
            entries[device_id] = _unavailable(device_id, "设备离线")

    if online_ids:

        def _run_pool() -> dict[int, dict]:
            out: dict[int, dict] = {}
            with ThreadPoolExecutor(max_workers=METRICS_CONCURRENCY) as pool:
                futures = {pool.submit(_collect_one, did): did for did in online_ids}
                for future in as_completed(futures):
                    device_id = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        logger.exception("Metrics worker failed for %s", device_id)
                        result = _unavailable(device_id, str(exc)[:200])
                    out[device_id] = result
            return out

        entries.update(await loop.run_in_executor(None, _run_pool))

    _latest = entries
    # 已删设备的计数器基线清理(微量,但常年运行会无界增长):
    # 本轮目标集合之外的键全部丢弃
    alive = {device_id for device_id, _ in targets}
    for prev in (_prev_net, _prev_io):
        for did in [k for k in prev if k not in alive]:
            prev.pop(did, None)
    # 样本时间戳供告警回溯:sustain 从指标真实越限时刻起算
    sampled_now = datetime.now(timezone.utc)
    for entry in entries.values():
        entry.setdefault("sampled_at", sampled_now)
    samples = [
        entry.pop("_sample") for entry in entries.values() if entry.get("_sample")
    ]
    if samples:

        def _persist_samples() -> None:
            db = SessionLocal()
            try:
                db.bulk_insert_mappings(DeviceMetricSample, samples)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Metrics sample batch persist failed")
            finally:
                db.close()

        await loop.run_in_executor(None, _persist_samples)
    await loop.run_in_executor(None, _cleanup_retention)
    await loop.run_in_executor(None, evaluate_alerts, entries)
    await loop.run_in_executor(None, evaluate_host_status_alerts)
    # 业务状态告警评估已迁至独立循环(run_business_alert_loop,2026-09-17):
    # METRICS_ENABLED=false 的纯接口监控部署不能失去业务告警。
    # 规则被停用/删除、对象被移出规则范围或整行删掉之后，遗留的告警再也不会被任何
    # 一轮评估访问到。每轮扫一遍(只查 open/pending，量很小)，不必等下一次重启。
    await loop.run_in_executor(None, close_orphaned_alert_events)


_collect_lock = asyncio.Lock()


async def _collect_cycle() -> None:
    """Serialize periodic and manual metric collection cycles."""
    async with _collect_lock:
        await _collect_cycle_unlocked()


async def run_metrics_loop():
    logger.info(
        "Metrics collector started (interval=%ds, concurrency=%d, retention=%dd)",
        METRICS_INTERVAL,
        METRICS_CONCURRENCY,
        METRICS_RETENTION_DAYS,
    )
    while True:
        try:
            await _collect_cycle()
        except asyncio.CancelledError:
            raise  # 正常关闭:让 CancelledError 上抛,交由 lifespan 取消
        except Exception:
            logger.exception("Metrics collection cycle failed")
        await asyncio.sleep(METRICS_INTERVAL)


async def trigger_collection() -> dict[int, dict]:
    """手动触发一轮采集(调试/接口用)。"""
    await _collect_cycle()
    return get_latest_metrics()
