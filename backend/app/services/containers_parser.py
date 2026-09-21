"""Docker 探测输出解析器——纯函数,可单测。

采集命令输出按 @@section 分节、| 分隔(见 containers_collector),
解析为结构化容器列表。不碰 SSH/WinRM/DB。
"""

from __future__ import annotations

import re

_SIZE_RE = re.compile(r"^([\d.]+)\s*([KMGT]i?B|B)?$", re.IGNORECASE)
_SIZE_TO_MB = {
    "B": 1 / (1024 * 1024),
    "KIB": 1 / 1024,
    "KB": 1 / 1024,
    "MIB": 1.0,
    "MB": 1.0,
    "GIB": 1024.0,
    "GB": 1024.0,
    "TIB": 1024.0 * 1024,
    "TB": 1024.0 * 1024,
}


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


def _parse_pct(s: str) -> float | None:
    try:
        return round(float(s.strip().rstrip("%")), 2)
    except (ValueError, TypeError):
        return None


def _parse_size_mb(token: str) -> int | None:
    m = _SIZE_RE.match(token.strip())
    if not m:
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "B").upper()
    return int(num * _SIZE_TO_MB.get(unit, 1.0))


def _parse_mem_usage(s: str) -> tuple[int | None, int | None]:
    """'1.2GiB / 16GiB' → (used_mb, limit_mb)。"""
    parts = s.split("/")
    if len(parts) != 2:
        return None, None
    return _parse_size_mb(parts[0]), _parse_size_mb(parts[1])


def parse_docker_output(text: str) -> dict:
    """返回 {available, version, containers:[...], nodocker}。"""
    result = {
        "available": False,
        "version": None,
        "containers": [],
        "nodocker": False,
        "commands": {
            "info": {"success": False, "error": "未执行"},
            "ps": {"success": False, "error": "未执行"},
            "stats": {"success": False, "error": "未执行"},
        },
    }
    if not text or not text.strip():
        return result

    sections = _sections(text)

    if "nodocker" in sections:
        result["nodocker"] = True
        for name in result["commands"]:
            result["commands"][name] = {"success": False, "error": "未安装 Docker"}
        return result

    # 采集命令会在每个分节首行写入 __ok/__err 元数据；兼容旧格式时，
    # 有内容的分节仍可解析，但不会把“部分输出”误报成完全成功。
    for name in ("info", "ps", "stats"):
        lines = sections.get(name, [])
        ok = next((line for line in lines if line.startswith("__ok=")), None)
        err = next((line for line in lines if line.startswith("__err=")), None)
        if ok is not None:
            result["commands"][name] = {
                "success": ok[5:].strip() == "1",
                "error": (err[6:].strip() if err else None),
            }
        elif lines:
            result["commands"][name] = {"success": True, "error": None}

    # 兼容迁移前落库/测试的旧输出格式（@@ver 而非 @@info，且无状态元数据）。
    if result["commands"]["info"]["error"] == "未执行" and sections.get("ver"):
        result["commands"]["info"] = {"success": True, "error": None}
    if result["commands"]["ps"]["error"] == "未执行" and sections.get("ps"):
        result["commands"]["ps"] = {"success": True, "error": None}
    if result["commands"]["stats"]["error"] == "未执行" and "stats" not in sections:
        result["commands"]["stats"] = {"success": True, "error": None}

    ver = sections.get("ver", []) or [
        line for line in sections.get("info", []) if not line.startswith("__")
    ]
    if ver and ver[0].strip():
        result["version"] = ver[0].strip()

    # stats 按容器名索引: name -> {cpu_pct, mem_used_mb, mem_limit_mb, mem_pct}
    stats_by_name: dict[str, dict] = {}
    for line in sections.get("stats", []):
        parts = line.split("|")
        if len(parts) < 4:
            continue
        name = parts[0].strip()
        used_mb, limit_mb = _parse_mem_usage(parts[2])
        stats_by_name[name] = {
            "cpu_pct": _parse_pct(parts[1]),
            "mem_used_mb": used_mb,
            "mem_limit_mb": limit_mb,
            "mem_pct": _parse_pct(parts[3]),
        }

    containers = []
    for line in sections.get("ps", []):
        parts = line.split("|", 5)
        if len(parts) < 5:
            continue
        cid, name, image, status, state = (p.strip() for p in parts[:5])
        ports = parts[5].strip() if len(parts) > 5 else ""
        st = stats_by_name.get(name, {})
        containers.append(
            {
                "container_id": cid,
                "name": name,
                "image": image or None,
                "status": status or None,
                "state": state or None,
                "ports": ports or None,
                "cpu_pct": st.get("cpu_pct"),
                "mem_used_mb": st.get("mem_used_mb"),
                "mem_limit_mb": st.get("mem_limit_mb"),
                "mem_pct": st.get("mem_pct"),
            }
        )

    # info 成功即可证明 Docker daemon 可用；ps/stats 的权限或超时单独呈现，
    # 不应把“daemon 正常但 stats 失败”误报成未安装 Docker。
    result["available"] = bool(result["commands"]["info"]["success"])
    result["containers"] = containers
    return result
