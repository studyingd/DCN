"""
Power control service — execute shutdown / reboot commands on remote devices.

通道约定(平台不对 Windows 使用 SSH):
  * Windows → WinRM(Stop-Computer / Restart-Computer);
  * Linux   → SSH(shutdown -h now / reboot)。

指令发出后统一探测设备端口确认真实离线——"命令已送达"不等于"机器已关机"。
"""

import logging
import socket
import time

import paramiko

from app.database import SessionLocal
from app.models.device import Device
from app.services.ssh import HostKeyMismatchError, connect_device, exec_ssh_command

logger = logging.getLogger(__name__)


def _exec_ssh_command(
    device: Device,
    username: str,
    password: str,
    command: str,
    timeout: int = 15,
    private_key: str | None = None,
) -> tuple[int, str, str]:
    """Run a single command over SSH (host-key verified, bounded read)."""
    db = SessionLocal()
    try:
        try:
            client, _key = connect_device(
                device,
                username,
                password,
                db=db,
                timeout=timeout,
                private_key=private_key,
            )
        except HostKeyMismatchError:
            return 1, "", "SSH host key mismatch — possible MITM"
        try:
            return exec_ssh_command(client, command, timeout=timeout)
        finally:
            client.close()
    finally:
        db.close()


def _is_port_open(ip: str, port: int, timeout: float = 3.0) -> bool:
    """Check if a TCP port is reachable on the given host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((ip, port))
            return True
        except OSError:
            return False


def _probe_ports(device: Device) -> list[int]:
    """TCP ports to probe when checking whether the device is still online."""
    if device.is_windows:
        return [p for p in (device.winrm_port or 5985, device.rdp_port or 3389) if p]
    return [device.ssh_port] if device.ssh_port else [22]


def _device_offline(device: Device, timeout: float = 1.5) -> bool:
    """True if none of the device's known service ports answer."""
    return not any(
        _is_port_open(device.ip_address, p, timeout) for p in _probe_ports(device)
    )


def _wait_for_power_off(device: Device, seconds: int = 30) -> bool:
    """Poll the device until it appears offline, or the timeout expires."""
    deadline = time.monotonic() + seconds
    while True:
        if _device_offline(device):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def _verify_power_result(device: Device, result: dict, reboot: bool) -> dict:
    """Confirm a claimed-success power op actually took the device offline.

    Why: a power path can report success merely because the command was *sent*
    (the connection often drops mid-flight as the machine goes down). The
    user-visible truth is whether the device actually powers off, so we verify
    it and downgrade to an honest failure (with an actionable hint) when not.
    """
    if not result.get("success"):
        # Direct path failure — preserve its message (already specific).
        return result

    method = result.get("method", "")
    action = "重启" if reboot else "关机"
    if _wait_for_power_off(device, seconds=30):
        return {
            "success": True,
            "method": method,
            "message": f"{action}指令已生效，设备正在{action}（已确认离线）",
        }

    hint = ""
    if device.is_windows:
        hint = "（建议：确认设备 WinRM 服务已开启且端口可达，绑定凭据需为本地管理员）"
    return {
        "success": False,
        "method": method,
        "message": f"已发送{action}指令，但设备 30 秒内未离线，{action}未生效。{hint}",
    }


def shutdown_device(
    device: Device, username: str, password: str, *, private_key: str | None = None
) -> dict:
    """Shut down the device (Windows→WinRM, Linux→SSH), then verify it powers off."""
    if not device.ip_address:
        return {"success": False, "message": "设备未配置 IP 地址"}

    if device.is_windows:
        result = _winrm_power(device, username, password, reboot=False)
        return _verify_power_result(device, result, reboot=False)

    return _verify_power_result(
        device,
        _ssh_shutdown(device, username, password, private_key=private_key),
        reboot=False,
    )


def reboot_device(
    device: Device, username: str, password: str, *, private_key: str | None = None
) -> dict:
    """Reboot the device (Windows→WinRM, Linux→SSH), then verify it goes down."""
    if not device.ip_address:
        return {"success": False, "message": "设备未配置 IP 地址"}

    if device.is_windows:
        result = _winrm_power(device, username, password, reboot=True)
        return _verify_power_result(device, result, reboot=True)

    return _verify_power_result(
        device,
        _ssh_reboot(device, username, password, private_key=private_key),
        reboot=True,
    )


def _winrm_power(device: Device, username: str, password: str, reboot: bool) -> dict:
    """Windows power op via WinRM (Stop-Computer / Restart-Computer).

    机器一开始关机,WinRM 连接 often 随即中断——"超时/传输错误"按指令已送达
    处理,交给调用方的离线验证裁决;认证失败/根本连不上则是硬失败。
    """
    from app.services.winrm import WinRMError, run_on_device

    action = "重启" if reboot else "关机"
    ps = "Restart-Computer -Force" if reboot else "Stop-Computer -Force"

    try:
        exit_code, out, err = run_on_device(device, username, password, ps, timeout=10)
    except WinRMError as exc:
        if exc.kind in ("auth", "connect"):
            return {"success": False, "method": "winrm", "message": str(exc)}
        logger.info(
            "WinRM %s %s: connection dropped after send (%s) — verifying",
            action,
            device.ip_address,
            exc,
        )
        return {
            "success": True,
            "method": "winrm",
            "message": f"{action}指令已发送（连接已断开，正在验证设备离线）",
        }

    logger.info(
        "WinRM %s %s: exit=%s out=%s err=%s",
        action,
        device.ip_address,
        exit_code,
        out,
        err,
    )
    if exit_code == 0:
        return {"success": True, "method": "winrm", "message": f"{action}指令已发送"}
    return {
        "success": False,
        "method": "winrm",
        "message": f"{action}命令执行失败 (exit={exit_code}): {err or out}",
    }


def _ssh_shutdown(
    device: Device, username: str, password: str, *, private_key: str | None = None
) -> dict:
    """Shut down a Linux device via SSH. A connection drop after the command is
    sent is treated as tentative success (the device is likely shutting down);
    the caller verifies by polling the device offline."""
    return _ssh_power(
        device,
        username,
        password,
        "shutdown -h now",
        reboot=False,
        private_key=private_key,
    )


def _ssh_reboot(
    device: Device, username: str, password: str, *, private_key: str | None = None
) -> dict:
    """Reboot a Linux device via SSH. See _ssh_power for drop handling."""
    return _ssh_power(
        device, username, password, "reboot", reboot=True, private_key=private_key
    )


def _ssh_power(
    device: Device,
    username: str,
    password: str,
    cmd: str,
    reboot: bool,
    *,
    private_key: str | None = None,
) -> dict:
    action = "重启" if reboot else "关机"
    db = SessionLocal()
    try:
        try:
            client, _key = connect_device(
                device, username, password, db=db, timeout=10, private_key=private_key
            )
        except HostKeyMismatchError:
            return {
                "success": False,
                "method": "ssh",
                "message": "SSH 主机密钥不匹配（疑似中间人攻击）",
            }
        except paramiko.AuthenticationException:
            return {
                "success": False,
                "method": "ssh",
                "message": "SSH 认证失败，请检查凭据",
            }
        except (socket.timeout, OSError) as exc:
            return {
                "success": False,
                "method": "ssh",
                "message": f"无法连接设备: {exc}",
            }

        try:
            exit_code, out, err = exec_ssh_command(client, cmd, timeout=10)
            logger.info(
                "%s %s: exit=%s out=%s err=%s",
                action,
                device.ip_address,
                exit_code,
                out,
                err,
            )
            if exit_code == 0:
                return {
                    "success": True,
                    "method": "ssh",
                    "message": f"{action}指令已发送",
                }
            return {
                "success": False,
                "method": "ssh",
                "message": f"{action}命令执行失败 (exit={exit_code}): {err or out}",
            }
        except (socket.timeout, OSError, EOFError, paramiko.SSHException) as exc:
            # Command already sent — a dropped connection usually means the device
            # is shutting down. Report tentative success; caller verifies.
            logger.info(
                "%s %s: connection lost after send (%s) — verifying",
                action,
                device.ip_address,
                exc,
            )
            return {
                "success": True,
                "method": "ssh",
                "message": f"{action}指令已发送（连接已断开，正在验证设备离线）",
            }
        except Exception as exc:
            logger.exception("%s error for %s", action, device.ip_address)
            return {
                "success": False,
                "method": "ssh",
                "message": f"{action}失败: {exc}",
            }
        finally:
            client.close()
    finally:
        db.close()
