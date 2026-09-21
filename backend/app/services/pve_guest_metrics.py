"""Guest filesystem metrics for PVE guests.

QEMU Guest Agent is the preferred source because it works through the PVE
guest channel and does not require guest network credentials.  When QGA is
disabled or unavailable, a configured PVE guest binding is queried over the
same SSH/WinRM paths used for ordinary server metrics.
"""

from __future__ import annotations

from typing import Any


def _usable_disks(disks: Any) -> list[dict]:
    """Normalize filesystem rows and discard rows without usable capacity."""
    if not isinstance(disks, list):
        return []
    normalized: list[dict] = []
    for disk in disks:
        if not isinstance(disk, dict):
            continue
        try:
            total = int(disk.get("total_bytes", disk.get("size_bytes")))
            used_value = disk.get("used_bytes")
            used = int(used_value) if used_value is not None else None
        except (TypeError, ValueError):
            continue
        if total <= 0 or used is None or used < 0:
            continue
        normalized.append(
            {
                "mount": str(disk.get("mount") or "未命名卷"),
                "filesystem_type": disk.get("filesystem_type") or None,
                "total_bytes": total,
                "used_bytes": min(used, total),
            }
        )
    return sorted(normalized, key=lambda item: item["mount"].lower())


def _base_result(qga: dict | None) -> dict:
    result = dict(qga or {})
    result.update(
        {
            "filesystem_available": False,
            "filesystem_source": None,
            "filesystem_disks": [],
            "filesystem_error": None,
        }
    )
    return result


def collect_guest_filesystems(
    qga: dict | None,
    *,
    ip_address: str | None,
    os_system: str | None,
    username: str | None,
    password: str | None,
    ssh_key: str | None = None,
    ssh_port: int = 22,
    winrm_port: int = 5985,
    ssh_host_key: str | None = None,
) -> dict:
    """Collect guest filesystem usage with QGA-first remote fallback.

    The public fields are safe to return to the UI.  ``_ssh_host_key`` is a
    private field used by the PVE router to persist first-contact SSH TOFU.
    """
    result = _base_result(qga)

    # QGA is authoritative whenever fsinfo returned usable rows.  QGA may be
    # partially available (for example, osinfo succeeds while fsinfo fails),
    # so an empty disk list is allowed to fall through to SSH/WinRM.
    qga_disks = _usable_disks(result.get("qga_disks"))
    if qga_disks:
        result.update(
            filesystem_available=True,
            filesystem_source="qga",
            filesystem_disks=qga_disks,
        )
        return result

    host = (ip_address or "").strip()
    user = (username or "").strip()
    system = (os_system or "").strip().lower()
    if not host or not user:
        result["filesystem_error"] = (
            "QGA 未返回可用文件系统数据，且未配置虚拟机 IP 或用户名"
            if result.get("qga_available")
            else "未配置虚拟机 IP 或用户名"
        )
        return result
    # 排除法:识别不出 Windows 的一律按 Linux(SSH)采集,与设备侧口径一致。
    if system != "windows":
        system = "linux"

    try:
        if system == "windows":
            if not (password or "").strip():
                result["filesystem_error"] = "Windows 文件系统采集需要 WinRM 密码"
                return result
            from app.services.metrics_parser import parse_windows_metrics
            from app.services.winrm import run_powershell

            script = """
Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
  Write-Output "disk=$($_.DeviceID),$($_.Size),$($_.Size-$_.FreeSpace)"
}
""".strip()
            code, out, err = run_powershell(
                host,
                user,
                password or "",
                script,
                port=winrm_port,
                timeout=15,
            )
            disks = _usable_disks(parse_windows_metrics(out).get("disks"))
            if not disks:
                result["filesystem_error"] = (err or out or f"WinRM 命令退出码 {code}")[
                    :300
                ]
                return result
            result.update(
                filesystem_available=True,
                filesystem_source="winrm",
                filesystem_disks=disks,
            )
            return result

        if not (password or "").strip() and not (ssh_key or "").strip():
            result["filesystem_error"] = "Linux 文件系统采集需要 SSH 密码或私钥"
            return result
        import socket

        import paramiko

        from app.services.metrics_parser import parse_linux_metrics
        from app.services.ssh import exec_ssh_command, open_ssh_client
        from app.services.ssh_pool import ssh_pool

        # 与 files 路由同口径:只有传输层故障才作废池连接(OSError 刻意不含,
        # 免得一次业务级错误把同主机的在用 Transport 一并掐断)。
        _TRANSPORT_ERRORS = (paramiko.SSHException, EOFError, socket.timeout)

        # SSH 兑底走连接池复用 transport(同文件管理器先例):详情页 30s 轮询 /
        # 磁盘采集循环 5min 一轮,QGA 不可用的虚机每次都新握手(实测 164ms+
        # 认证,慢网络更久),复用后只剩一次 channel open。池键额外并入 pinned
        # host key:首次 TOFU 捕获的 key 落库后,后续借用按新键走"强制校验"
        # 的连接,不复用 TOFU 时期的那条。
        pool_key = ssh_pool.key(
            host, ssh_port, user, password or None, ssh_key or None
        ) + (ssh_host_key or None,)
        captured: dict = {}

        def _connect():
            client, captured_key = open_ssh_client(
                host,
                ssh_port,
                user,
                password or None,
                timeout=10,
                pinned_key_b64=ssh_host_key or None,
                private_key=ssh_key or None,
                allow_tofu=not bool(ssh_host_key),
            )
            captured["key"] = captured_key
            return client

        ssh_pool.mark_in_use(pool_key)
        try:
            client, _created = ssh_pool.acquire(pool_key, _connect)
            try:
                code, out, err = exec_ssh_command(
                    client,
                    "df -B1 -P 2>/dev/null || df -P",
                    timeout=15,
                )
                ssh_pool.touch(pool_key)
            except _TRANSPORT_ERRORS:
                ssh_pool.discard(pool_key)
                raise
        finally:
            ssh_pool.release(pool_key)
        disks = _usable_disks(parse_linux_metrics(f"@@disk\n{out}").get("disks"))
        if not disks:
            result["filesystem_error"] = (err or out or f"SSH 命令退出码 {code}")[:300]
            return result
        result.update(
            filesystem_available=True,
            filesystem_source="ssh",
            filesystem_disks=disks,
        )
        if not ssh_host_key and captured.get("key"):
            result["_ssh_host_key"] = captured["key"]
        return result
    except Exception as exc:
        result["filesystem_error"] = str(exc)[:300]
        return result
