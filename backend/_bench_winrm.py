"""WinRM 采集成本实测(一次性诊断脚本,非产品代码)。

对每台在线 Windows 设备/绑定虚机,分别跑:
  1. trivial   —— 空脚本,测会话固定开销(TCP+NTLM+shell 生命周期+PowerShell 启动);
  2. metrics   —— metrics_collector._WINDOWS_PS(60s 一轮的指标采集脚本);
  3. containers—— 容器探测脚本(60s 一轮)。
脚本尾部附 $PID 进程自报 CPU 秒数与工作集,直接反映目标机承受的 CPU/内存成本。
"""

from __future__ import annotations

import re
import sys
import time

from app.database import SessionLocal
from app.models.device import OPS_TARGET_TYPES, Device
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.services.containers_collector import WINDOWS_PS as CONT_PS
from app.services.crypto import decrypt
from app.services.device_credentials import resolve_credentials
from app.services.metrics_collector import _WINDOWS_PS as MET_PS
from app.services.winrm import run_powershell

SELF = r"""
$p = Get-Process -Id $PID
Write-Output ("self_cpu={0:F3}" -f $p.TotalProcessorTime.TotalSeconds)
Write-Output ("self_ws={0:F1}" -f ($p.WorkingSet64/1MB))
"""

SELF_RE = re.compile(r"self_cpu=([\d.]+)\s*self_ws=([\d.]+)")


def bench(name: str, host: str, port: int, username: str, password: str) -> None:
    probes = [
        ("trivial", "Write-Output ok"),
        ("metrics", MET_PS),
        ("containers", CONT_PS),
    ]
    print(f"\n=== {name}  {host}:{port} ===")
    total_cpu = 0.0
    for label, script in probes:
        runs = 3 if label == "trivial" else 2
        for i in range(runs):
            t0 = time.perf_counter()
            try:
                code, out, err = run_powershell(
                    host, username, password, script + SELF, port=port, timeout=25
                )
            except Exception as exc:
                print(f"  {label}[{i}] FAILED: {exc}")
                continue
            dt = time.perf_counter() - t0
            m = SELF_RE.search(out or "")
            cpu = float(m.group(1)) if m else None
            ws = float(m.group(2)) if m else None
            nodocker = "@@nodocker" in (out or "")
            print(
                f"  {label}[{i}] wall={dt * 1000:7.0f}ms  exit={code}  "
                f"target_cpu={cpu}s  ws={ws}MB  nodocker={nodocker}"
            )
            if label != "trivial" and cpu is not None:
                total_cpu += cpu
    # 每分钟一轮 metrics + 一轮 containers
    print(
        f"  >> 周期负载/分钟: target_cpu≈{total_cpu / 2:.2f}s/min"
        f" (占 1 核 {total_cpu / 2 / 60 * 100:.2f}%)"
    )


def main() -> None:
    db = SessionLocal()
    try:
        devices = (
            db.query(Device)
            .filter(
                Device.type.in_(OPS_TARGET_TYPES),
                Device.status == "online",
                Device.os_system.ilike("%windows%"),
            )
            .all()
        )
        for d in devices:
            creds = resolve_credentials(d)
            if not creds or not creds[1]:
                print(f"skip device {d.name}: 无 WinRM 密码")
                continue
            bench(
                f"设备 {d.name}({d.os_system})",
                d.ip_address,
                d.winrm_port or 5985,
                creds[0],
                creds[1],
            )

        bindings = (
            db.query(PveGuestBinding, PveConnection)
            .join(PveConnection, PveConnection.id == PveGuestBinding.connection_id)
            .filter(PveGuestBinding.enabled == 1, PveConnection.enabled == 1)
            .filter(PveGuestBinding.os_system == "windows")
            .all()
        )
        for b, c in bindings:
            if not b.ip_address or not b.username or not b.password_enc:
                print(f"skip binding [{c.name}] {b.vmid}: 未配 IP/凭据")
                continue
            bench(
                f"绑定虚机 [{c.name}] qemu {b.vmid}",
                b.ip_address,
                b.winrm_port or 5985,
                b.username,
                decrypt(b.password_enc),
            )
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
