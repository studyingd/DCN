"""Metrics output parsers — pure functions, unit-testable.

采集命令的原始输出 → 结构化指标 dict。解析器不碰 SSH/WinRM/DB,
输入是字符串,输出是统一契约:

    {
      "ok": bool,
      "cpu_pct": float | None,
      "mem_pct": float | None,
      "mem_used_mb": int | None,
      "mem_total_mb": int | None,
      "disk_max_pct": float | None,
      "disks": [{"mount", "size_bytes", "used_bytes", "pct"}],
      "load1"/"load5"/"load15": float | None,   # 仅 Linux
      "uptime_sec": int | None,
      "net_rx_bytes"/"net_tx_bytes": int | None,  # Linux 累计计数器(采集器跨周期求差)
      "net_rx_bps"/"net_tx_bps": float | None,    # Windows 直读速率
      "diskio_read_bytes"/"diskio_write_bytes": int | None,  # Linux 累计计数器
      "disk_read_bps"/"disk_write_bps": float | None,        # Windows 直读速率
      "os_caption": str | None,                   # Windows 精确版本名(OS 自愈回写用)
    }
"""

from __future__ import annotations

import re

# Linux 端忽略的伪文件系统(df 输出里无监控价值的)
_PSEUDO_FS_PREFIXES = (
    "tmpfs",
    "devtmpfs",
    "overlay",
    "squashfs",
    "none",
    "udev",
    "efivarfs",
    "/dev/loop",
)
_PSEUDO_MOUNT_PREFIXES = ("/run", "/dev", "/sys", "/snap")


# Linux 整盘设备名(跳过分区与 loop/ram/dm,避免重复计数)
_WHOLE_DISK_RE = re.compile(
    r"^(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+|mmcblk\d+)$"
)


def _empty() -> dict:
    return {
        "ok": False,
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
        "net_rx_bytes": None,
        "net_tx_bytes": None,
        "net_rx_bps": None,
        "net_tx_bps": None,
        "diskio_read_bytes": None,
        "diskio_write_bytes": None,
        "disk_read_bps": None,
        "disk_write_bps": None,
        "os_caption": None,
    }


def _to_float(s: str) -> float | None:
    try:
        v = float(s)
    except (ValueError, TypeError):
        return None
    if v != v:  # NaN
        return None
    return v


def _to_int(s: str) -> int | None:
    f = _to_float(s)
    return int(f) if f is not None else None


# ══════════════════════════════════════════════════════════════════════
# Linux:@@section 标记的分节输出(见 metrics_collector._LINUX_CMD)
# ══════════════════════════════════════════════════════════════════════


def _sections(text: str) -> dict[str, list[str]]:
    secs: dict[str, list[str]] = {}
    cur: str | None = None
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("@@"):
            cur = line[2:].strip()
            secs[cur] = []
        elif cur is not None and line.strip():
            secs[cur].append(line)
    return secs


def _cpu_ticks(line: str) -> tuple[int, int] | None:
    """'cpu  u n s idle iowait irq softirq steal ...' → (idle_all, total)。"""
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    try:
        v = [int(x) for x in parts[1:9]]
    except ValueError:
        return None
    while len(v) < 8:
        v.append(0)
    # guest/guest_nice 已计入 user/nice,不累加以免重复
    idle_all = v[3] + v[4]
    total = sum(v)
    return idle_all, total


def _parse_linux_cpu(lines1: list[str], lines2: list[str]) -> float | None:
    """会话内两次采样 /proc/stat 求差——自包含,不依赖历史基线。"""
    if not lines1 or not lines2:
        return None
    a = _cpu_ticks(lines1[0])
    b = _cpu_ticks(lines2[0])
    if not a or not b:
        return None
    d_idle = b[0] - a[0]
    d_total = b[1] - a[1]
    if d_total <= 0:
        return None
    pct = (1 - d_idle / d_total) * 100
    return round(max(0.0, min(100.0, pct)), 1)


def _parse_linux_mem(lines: list[str]) -> tuple[float | None, int | None, int | None]:
    total_kb = avail_kb = None
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == "MemTotal:":
            total_kb = _to_int(parts[1])
        elif parts[0] == "MemAvailable:":
            avail_kb = _to_int(parts[1])
    if not total_kb:
        return None, None, None
    total_mb = total_kb // 1024
    used_mb = max(0, total_mb - (avail_kb or 0) // 1024)
    pct = round(used_mb / total_mb * 100, 1) if total_mb else None
    return pct, used_mb, total_mb


def _parse_linux_disks(lines: list[str]) -> list[dict]:
    """df -B1 -P 输出:Filesystem 1B-blocks Used Available Capacity Mounted on"""
    disks: list[dict] = []
    for line in lines:
        if line.startswith("Filesystem"):
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        fs, size_s, used_s, _avail_s, _cap, mount = parts
        if not size_s.isdigit():
            continue
        size = int(size_s)
        used = int(used_s) if used_s.isdigit() else 0
        if size <= 0:
            continue
        low = fs.lower()
        if low.startswith(_PSEUDO_FS_PREFIXES):
            continue
        if mount.startswith(_PSEUDO_MOUNT_PREFIXES):
            continue
        disks.append(
            {
                "mount": mount,
                "size_bytes": size,
                "used_bytes": used,
                "pct": round(used / size * 100, 1),
            }
        )
    return disks


def _parse_linux_net(lines: list[str]) -> tuple[int | None, int | None]:
    """/proc/net/dev → 物理网卡累计 (rx_bytes, tx_bytes)。"""
    rx = tx = 0
    seen = False
    for line in lines:
        if "|" in line or ":" not in line:  # 跳过两行表头
            continue
        name, rest = line.split(":", 1)
        if name.strip() == "lo":
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        r = _to_int(fields[0])
        t = _to_int(fields[8])
        if r is None or t is None:
            continue
        rx += r
        tx += t
        seen = True
    return (rx, tx) if seen else (None, None)


def _parse_linux_diskio(lines: list[str]) -> tuple[int | None, int | None]:
    """/proc/diskstats → 整盘累计 (read_bytes, write_bytes)。

    只统计整盘设备(sdX/vdX/nvmeXnY/mmcblkN 等),分区(loop/sda1/nvme0n1p1)
    与 loop/ram/dm 全部跳过,避免重复计数。扇区按 512 字节换算。
    """
    read = written = 0
    seen = False
    for line in lines:
        parts = line.split()
        if len(parts) < 10:
            continue
        if not _WHOLE_DISK_RE.match(parts[2]):
            continue
        # parts[3]=reads_completed ... parts[5]=sectors_read, parts[9]=sectors_written
        sr = _to_int(parts[5])
        sw = _to_int(parts[9])
        if sr is None or sw is None:
            continue
        read += sr * 512
        written += sw * 512
        seen = True
    return (read, written) if seen else (None, None)


def parse_linux_metrics(text: str) -> dict:
    result = _empty()
    if not text or not text.strip():
        return result

    secs = _sections(text)

    result["cpu_pct"] = _parse_linux_cpu(secs.get("cpu1", []), secs.get("cpu2", []))

    mem_pct, used_mb, total_mb = _parse_linux_mem(secs.get("mem", []))
    result["mem_pct"] = mem_pct
    result["mem_used_mb"] = used_mb
    result["mem_total_mb"] = total_mb

    disks = _parse_linux_disks(secs.get("disk", []))
    result["disks"] = disks
    if disks:
        result["disk_max_pct"] = max(d["pct"] for d in disks)

    load = secs.get("load", [])
    if load:
        parts = load[0].split()
        if len(parts) >= 3:
            result["load1"] = _to_float(parts[0])
            result["load5"] = _to_float(parts[1])
            result["load15"] = _to_float(parts[2])

    uptime = secs.get("uptime", [])
    if uptime:
        parts = uptime[0].split()
        if parts:
            result["uptime_sec"] = _to_int(parts[0])

    rx, tx = _parse_linux_net(secs.get("net", []))
    result["net_rx_bytes"] = rx
    result["net_tx_bytes"] = tx

    io_r, io_w = _parse_linux_diskio(secs.get("diskio", []))
    result["diskio_read_bytes"] = io_r
    result["diskio_write_bytes"] = io_w

    # PRETTY_NAME="Rocky Linux 9.4 (Blue Onyx)" → Rocky Linux 9.4 (Blue Onyx)
    # 供采集器自愈回写 device.os_system(与 Windows 的 Caption 等价)。
    osrel = secs.get("osrel", [])
    if osrel:
        m = re.match(r'^\s*PRETTY_NAME="?([^"\n]+)"?\s*$', osrel[0])
        if m:
            result["os_caption"] = m.group(1).strip() or None

    result["ok"] = any(
        v is not None
        for v in (result["cpu_pct"], result["mem_pct"], result["disk_max_pct"])
    )
    return result


# ══════════════════════════════════════════════════════════════════════
# Windows:key=value 行输出(见 metrics_collector._WINDOWS_PS)
# ══════════════════════════════════════════════════════════════════════


def parse_windows_metrics(text: str) -> dict:
    result = _empty()
    if not text or not text.strip():
        return result

    mem_total_mb: int | None = None
    mem_free_mb: int | None = None
    mem_available_mb: int | None = None
    disks: list[dict] = []
    rx_bps = 0.0
    tx_bps = 0.0
    net_seen = False
    io_read = 0.0
    io_write = 0.0
    io_seen = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().lower()
        val = val.strip()

        if key == "cpu_pct":
            result["cpu_pct"] = _to_float(val)
        elif key == "mem_total_mb":
            mem_total_mb = _to_int(val)
        elif key == "mem_free_mb":
            mem_free_mb = _to_int(val)
        elif key == "mem_available_mb":
            mem_available_mb = _to_int(val)
        elif key == "uptime_sec":
            result["uptime_sec"] = _to_int(val)
        elif key == "caption":
            result["os_caption"] = val or None
        elif key == "disk":
            parts = val.split(",")
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                size = int(parts[1])
                used = int(parts[2])
                if size > 0:
                    disks.append(
                        {
                            "mount": parts[0],
                            "size_bytes": size,
                            "used_bytes": used,
                            "pct": round(used / size * 100, 1),
                        }
                    )
        elif key == "netif":
            parts = val.split(",")
            if len(parts) >= 3:
                r = _to_float(parts[1])
                t = _to_float(parts[2])
                if r is not None and t is not None:
                    rx_bps += r
                    tx_bps += t
                    net_seen = True
        elif key == "diskio":
            parts = val.split(",")
            # _Total 是各盘之和,采集端已过滤;这里再挡一层防重复计数
            if len(parts) >= 4 and parts[0].strip().lower() != "_total":
                r = _to_float(parts[1])
                w = _to_float(parts[2])
                if r is not None and w is not None:
                    io_read += r
                    io_write += w
                    io_seen = True

    if mem_total_mb:
        # AvailableMBytes includes reclaimable standby/file-cache pages and
        # matches Task Manager's available-memory semantics. Fall back to the
        # legacy free-pages value when older agents do not emit it.
        available_mb = (
            mem_available_mb if mem_available_mb is not None else (mem_free_mb or 0)
        )
        used_mb = max(0, mem_total_mb - available_mb)
        result["mem_total_mb"] = mem_total_mb
        result["mem_used_mb"] = used_mb
        result["mem_pct"] = round(used_mb / mem_total_mb * 100, 1)

    result["disks"] = disks
    if disks:
        result["disk_max_pct"] = max(d["pct"] for d in disks)
    if net_seen:
        result["net_rx_bps"] = rx_bps
        result["net_tx_bps"] = tx_bps
    if io_seen:
        result["disk_read_bps"] = io_read
        result["disk_write_bps"] = io_write

    result["ok"] = any(
        v is not None
        for v in (result["cpu_pct"], result["mem_pct"], result["disk_max_pct"])
    )
    return result
