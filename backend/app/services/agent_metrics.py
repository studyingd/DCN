"""平台自身指标趋势摘要(从 agent.py 拆出,Phase-1 2026-10-31)。

设备=DeviceMetricSample,虚机=pve_guest_status 内存历史环;零成本区分
业务增长与突发泄漏。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.device_metric_sample import DeviceMetricSample
from app.models.pve_guest_binding import PveGuestBinding
from app.services.agent_types import AgentTarget, session_retry
from app.services.pve_guest_status import get_guest_metric_history


def _metrics_summary(db: Session, device: Device | AgentTarget, hours: int) -> str:
    """近 N 小时指标的紧凑摘要(给 LLM 的上下文,不是完整时序)。

    物理设备读 ``DeviceMetricSample``;虚拟机(AgentTarget,id=None)没有 ORM
    指标记录,改用 PVE 快照环内存历史(经运维接入绑定按 IP 反查宿主身份)。
    """
    device_id = getattr(device, "id", None)
    if device_id is not None:
        return _device_metrics_summary(db, device_id, hours)
    if not device.ip_address:
        return json.dumps(
            {"note": "目标无 IP,无法关联平台采集数据"}, ensure_ascii=False
        )

    def _load_binding():
        return (
            db.query(PveGuestBinding)
            .filter(
                PveGuestBinding.ip_address == device.ip_address.strip(),
                PveGuestBinding.enabled == 1,
            )
            .first()
        )

    # 长会话自愈:连接被服务端抬掉时回滚换新连接重试
    binding = session_retry(db, _load_binding)
    if binding is None:
        return json.dumps(
            {"note": "PVE 虚拟机尚未接入平台指标采集,请使用虚拟机内诊断项取证"},
            ensure_ascii=False,
        )
    return _guest_metrics_summary(binding.connection_id, binding.vmid, hours)


def _device_metrics_summary(db: Session, device_id: int, hours: int) -> str:
    """物理设备:DeviceMetricSample 的窗口统计。"""
    hours = min(max(hours, 1), 168)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    def _load_rows():
        return (
            db.query(DeviceMetricSample)
            .filter(
                DeviceMetricSample.device_id == device_id,
                DeviceMetricSample.ts >= since,
            )
            .order_by(DeviceMetricSample.ts)
            .all()
        )

    rows = session_retry(db, _load_rows)
    if not rows:
        return json.dumps({"note": "该时间范围内无采集数据"}, ensure_ascii=False)

    latest_ts = rows[-1].ts
    if latest_ts.tzinfo is None:
        latest_ts = latest_ts.replace(tzinfo=timezone.utc)
    age_seconds = max(0, int((datetime.now(timezone.utc) - latest_ts).total_seconds()))

    def _stat(field: str) -> dict | None:
        vals = [getattr(r, field) for r in rows if getattr(r, field) is not None]
        if not vals:
            return None
        return {
            "latest": round(vals[-1], 1),
            "avg": round(sum(vals) / len(vals), 1),
            "max": round(max(vals), 1),
        }

    summary = {
        "window_hours": hours,
        "samples": len(rows),
        "latest_sample_at": latest_ts.isoformat(),
        "age_seconds": age_seconds,
        "data_status": "stale" if age_seconds > 180 else "fresh",
        "cpu_pct": _stat("cpu_pct"),
        "mem_pct": _stat("mem_pct"),
        "disk_max_pct": _stat("disk_max_pct"),
        "load1": _stat("load1"),
        "net_rx_bps": _stat("net_rx_bps"),
        "net_tx_bps": _stat("net_tx_bps"),
        "disk_read_bps": _stat("disk_read_bps"),
        "disk_write_bps": _stat("disk_write_bps"),
    }
    return json.dumps(summary, ensure_ascii=False)


def _guest_metrics_summary(connection_id: int, vmid: int, hours: int) -> str:
    """虚拟机:PVE 快照环历史的窗口统计(cpu/mem 与告警评估同口径)。

    磁盘使用率快照里没有(告警评估也不产出),不编造字段。
    """
    samples = get_guest_metric_history(connection_id, vmid)
    if not samples:
        return json.dumps(
            {"note": "PVE 快照历史尚为空(采集刚启动或本实例非 leader)"},
            ensure_ascii=False,
        )
    hours = min(max(hours, 1), 168)
    cutoff = time.time() - hours * 3600
    rows = [s for s in samples if float(s.get("ts") or 0) >= cutoff]
    if not rows:
        return json.dumps({"note": "该时间范围内无采集数据"}, ensure_ascii=False)

    latest = rows[-1]
    latest_ts = float(latest.get("ts") or 0)

    cpu_vals = [round(float(s.get("cpu") or 0) * 100.0, 1) for s in rows]

    def _pct(field: str, total_field: str) -> dict | None:
        vals = [
            round(float(s.get(field) or 0) / float(s.get(total_field) or 0) * 100.0, 1)
            for s in rows
            if s.get(total_field)
        ]
        if not vals:
            return None
        return {
            "latest": vals[-1],
            "avg": round(sum(vals) / len(vals), 1),
            "max": round(max(vals), 1),
        }

    summary = {
        "window_hours": hours,
        "samples": len(rows),
        "latest_sample_at": datetime.fromtimestamp(
            latest_ts, tz=timezone.utc
        ).isoformat(),
        "age_seconds": max(0, int(time.time() - latest_ts)),
        "data_status": "stale" if time.time() - latest_ts > 180 else "fresh",
        "status": latest.get("status"),
        "cpu_pct": (
            {
                "latest": cpu_vals[-1],
                "avg": round(sum(cpu_vals) / len(cpu_vals), 1),
                "max": round(max(cpu_vals), 1),
            }
            if cpu_vals
            else None
        ),
        "mem_pct": _pct("mem", "maxmem"),
        "mem_bytes": latest.get("mem"),
        "maxmem_bytes": latest.get("maxmem"),
        # 宿主记账口径说明:面板/PVE 侧内存是「分配给虚拟机的量」,与机内
        # 任务管理器的实际使用口径不同;未启用 balloon 时按全量配置记账。
        "mem_basis": (
            "pve_host_accounted(分配口径,区别于机内实际使用;未启用 balloon 时"
            "按配置全量记账)"
        ),
    }
    return json.dumps(summary, ensure_ascii=False)
