"""
Power control service — execute shutdown / reboot commands on remote devices
via SSH, Win32 RPC, or Guacamole RDP keyboard injection.
"""

import asyncio
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
) -> tuple[int, str, str]:
    """Run a single command over SSH (host-key verified, bounded read)."""
    db = SessionLocal()
    try:
        try:
            client, _key = connect_device(device, username, password, db=db, timeout=timeout)
        except HostKeyMismatchError:
            return 1, "", "SSH host key mismatch — possible MITM"
        try:
            return exec_ssh_command(client, command, timeout=timeout)
        finally:
            client.close()
    finally:
        db.close()


def _is_windows(device: Device) -> bool:
    return (device.os_system or "").lower() == "windows"


def _is_port_open(ip: str, port: int, timeout: float = 3.0) -> bool:
    """Check if a TCP port is reachable on the given host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((ip, port))
            return True
        except OSError:
            return False


def _is_ssh_port_open(device: Device, timeout: float = 3) -> bool:
    """Check if the SSH port is reachable."""
    return _is_port_open(device.ip_address, device.ssh_port, timeout)


def _win32_shutdown(device: Device, reboot: bool = False) -> dict:
    """Attempt Windows shutdown via Win32 InitiateSystemShutdown RPC.

    Returns a result dict with a ``method`` field ("win32") and a human-readable
    message explaining any failure (auth rejected, access denied, etc.). The
    caller decides whether to fall back to the RDP path based on ``success``.
    """
    try:
        import ctypes
        import ctypes.wintypes

        mpr = ctypes.windll.mpr
        advapi32 = ctypes.windll.advapi32

        class NETRESOURCEW(ctypes.Structure):
            _fields_ = [
                ("dwScope", ctypes.wintypes.DWORD),
                ("dwType", ctypes.wintypes.DWORD),
                ("dwDisplayType", ctypes.wintypes.DWORD),
                ("dwUsage", ctypes.wintypes.DWORD),
                ("lpLocalName", ctypes.wintypes.LPWSTR),
                ("lpRemoteName", ctypes.wintypes.LPWSTR),
                ("lpComment", ctypes.wintypes.LPWSTR),
                ("lpProvider", ctypes.wintypes.LPWSTR),
            ]

        # Declare explicit signatures + use_last_error so GetLastError() is the
        # error from InitiateSystemShutdownW itself (not from a later call).
        mpr.WNetAddConnection2W.argtypes = [
            ctypes.POINTER(NETRESOURCEW),
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
        ]
        mpr.WNetAddConnection2W.restype = ctypes.wintypes.DWORD
        mpr.WNetCancelConnection2W.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
        ]
        mpr.WNetCancelConnection2W.restype = ctypes.wintypes.DWORD
        advapi32.InitiateSystemShutdownW.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.BOOL,
        ]
        advapi32.InitiateSystemShutdownW.restype = ctypes.wintypes.BOOL
        # NOTE: do NOT use `use_last_error`/ctypes.get_last_error() here — it
        # unreliably returns 0 for this call (observed). Read GetLastError()
        # directly, immediately after the call, with no intervening Win32 calls.
        get_last_error = ctypes.windll.kernel32.GetLastError
        get_last_error.restype = ctypes.wintypes.DWORD

        from app.database import SessionLocal
        from app.models.credential import Credential
        from app.services.crypto import decrypt

        username = ""
        password = ""
        if device.credential_id:
            db = SessionLocal()
            try:
                cred = (
                    db.query(Credential)
                    .filter(Credential.id == device.credential_id)
                    .first()
                )
                if cred:
                    username = cred.username or ""
                    password = decrypt(cred.password_enc) if cred.password_enc else ""
            finally:
                db.close()

        if not username or not password:
            return {
                "success": False,
                "method": "win32",
                "message": "未找到设备凭据，无法通过 Win32 执行关机",
            }

        remote_name = "\\\\" + device.ip_address + "\\IPC$"
        nr = NETRESOURCEW()
        nr.dwType = 0
        nr.lpLocalName = None
        nr.lpRemoteName = remote_name
        nr.lpProvider = None

        # SMB error codes → human reason
        SMB_ERR = {
            5: "访问被拒绝",
            53: "网络路径不可达",
            67: "错误的共享名",
            1219: "已存在到该主机的连接（凭据冲突）",
            1326: "用户名或密码错误",
            1327: "账户限制（空密码或策略）",
            2242: "密码已过期",
        }
        res = mpr.WNetAddConnection2W(ctypes.byref(nr), password, username, 0)
        if res != 0:
            reason = SMB_ERR.get(res, f"错误码 {res}")
            logger.info("Win32 shutdown: SMB auth failed (code %d)", res)
            return {
                "success": False,
                "method": "win32",
                "message": f"Win32 SMB 认证失败：{reason}",
            }

        machine = "\\\\" + device.ip_address
        # Force close apps, 0s timeout so it powers off promptly.
        result = advapi32.InitiateSystemShutdownW(
            machine, "Shutdown by DCN", 0, True, reboot
        )
        # Read GetLastError IMMEDIATELY — before WNetCancelConnection2W clobbers it.
        err = get_last_error()
        mpr.WNetCancelConnection2W(remote_name, 0, True)

        if result:
            logger.info("Win32 shutdown: success (reboot=%s)", reboot)
            return {
                "success": True,
                "method": "win32",
                "message": "重启指令已发送" if reboot else "关机指令已发送",
            }

        SHUTDOWN_ERR = {
            5: "访问被拒绝——账户非管理员，或受 UAC 远程限制（LocalAccountTokenFilterPolicy）过滤",
            1722: "RPC 服务器不可用",
            53: "网络路径不可达",
        }
        reason = SHUTDOWN_ERR.get(err, f"错误码 {err}")
        logger.info("Win32 shutdown: failed (code %d — %s)", err, reason)
        return {
            "success": False,
            "method": "win32",
            "message": f"Win32 远程关机被拒绝：{reason}",
        }
    except Exception as exc:
        logger.debug("Win32 shutdown not available: %s", exc)
        return {
            "success": False,
            "method": "win32",
            "message": f"Win32 关机不可用：{exc}",
        }


async def _rdp_shutdown(device: Device, reboot: bool = False) -> dict:
    """Shutdown via Guacamole RDP keyboard injection (open RDP session, send key sequence)."""
    from app.config import GUACD_HOST, GUACD_PORT
    from app.database import SessionLocal
    from app.models.credential import Credential
    from app.services.crypto import decrypt
    from app.services.guacamole import GuacamoleSession, start_rdp_relay

    # Resolve credentials
    username = ""
    password = ""
    if device.credential_id:
        db = SessionLocal()
        try:
            cred = (
                db.query(Credential)
                .filter(Credential.id == device.credential_id)
                .first()
            )
            if cred:
                username = cred.username or ""
                password = decrypt(cred.password_enc) if cred.password_enc else ""
        finally:
            db.close()

    if not username or not password:
        return {
            "success": False,
            "method": "rdp",
            "message": "未找到设备凭据，无法通过 RDP 执行关机",
        }

    rdp_port = device.rdp_port or 3389
    session = GuacamoleSession(host=GUACD_HOST, port=GUACD_PORT)

    relay_server = None
    try:
        relay_server, local_port = await start_rdp_relay(device.ip_address, rdp_port)
        import platform

        relay_hostname = device.ip_address
        if GUACD_HOST in ("localhost", "127.0.0.1") and platform.system() != "Linux":
            relay_hostname = "host.docker.internal"
        relay_port = local_port

        await session.connect()
        await session.handshake_rdp(
            hostname=relay_hostname,
            port=relay_port,
            username=username,
            password=password,
            width=800,
            height=600,
        )

        # Wait for RDP session to stabilize — read instructions in background
        async def _drain():
            try:
                while session.is_connected:
                    await session.read_instruction()
            except Exception:
                pass

        drain_task = asyncio.create_task(_drain())
        await asyncio.sleep(3)

        # Send Win+R to open Run dialog
        await session.write_instruction("key", "65515", "1")  # Win down
        await asyncio.sleep(0.05)
        await session.write_instruction("key", "114", "1")  # r down
        await asyncio.sleep(0.05)
        await session.write_instruction("key", "114", "0")  # r up
        await asyncio.sleep(0.05)
        await session.write_instruction("key", "65515", "0")  # Win up

        # Wait for Run dialog to appear
        await asyncio.sleep(1.5)

        # Type the shutdown command
        if reboot:
            cmd_text = "shutdown /r /f /t 0"
        else:
            cmd_text = "shutdown /s /f /t 0"

        for char in cmd_text:
            keysym = _char_to_keysym(char)
            if keysym:
                await session.write_instruction("key", str(keysym), "1")
                await session.write_instruction("key", str(keysym), "0")
                await asyncio.sleep(0.03)

        # Press Enter
        await asyncio.sleep(0.3)
        await session.write_instruction("key", "65293", "1")
        await session.write_instruction("key", "65293", "0")

        # Cancel drain task
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass

        logger.info(
            "RDP shutdown keys sent to %s (reboot=%s)", device.ip_address, reboot
        )
        return {
            "success": True,
            "method": "rdp",
            "message": "关机指令已通过 RDP 发送"
            if not reboot
            else "重启指令已通过 RDP 发送",
        }

    except Exception as exc:
        logger.exception("RDP shutdown failed for %s", device.ip_address)
        return {"success": False, "method": "rdp", "message": f"RDP 关机失败: {exc}"}
    finally:
        await session.close()
        if relay_server:
            relay_server.close()
            await relay_server.wait_closed()


def _char_to_keysym(char: str) -> int | None:
    """Convert an ASCII character to a Guacamole keysym."""
    code = ord(char)
    if 32 <= code <= 126:
        return code
    return None


def _probe_ports(device: Device) -> list[int]:
    """TCP ports to probe when checking whether the device is still online."""
    ports = [device.ssh_port] if device.ssh_port else []
    if _is_windows(device):
        ports.append(445)  # SMB — used by Win32 RPC
        ports.append(device.rdp_port or 3389)
    return list(dict.fromkeys(p for p in ports if p)) or [22]


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


def _verify_power_result(
    device: Device, result: dict, reboot: bool, win32_reason: str = ""
) -> dict:
    """Confirm a claimed-success power op actually took the device offline.

    Why: power paths (especially the fire-and-forget RDP keystroke path, and
    the legacy SSH path) can report success merely because a command or
    keystroke was *sent*. The user-visible truth is whether the device
    actually powers off, so we verify it and downgrade to an honest failure
    (with an actionable hint) when it does not.

    ``win32_reason`` carries the Win32 RPC failure reason (access denied, etc.)
    so that when the RDP fallback also fails to actually power off the device,
    the user sees the real root cause rather than a generic message.
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
    if _is_windows(device):
        hint = "（建议：为设备绑定具有远程关机权限的管理员账户，或为设备开启 SSH 服务后重试）"
    detail = f"原因：{win32_reason}。" if win32_reason else ""
    return {
        "success": False,
        "method": method,
        "message": f"已发送{action}指令，但设备 30 秒内未离线，{action}未生效。{detail}{hint}",
    }


def shutdown_device(device: Device, username: str, password: str) -> dict:
    """Shut down the device via SSH, Win32 RPC, or RDP, then verify it powers off."""
    if not device.ip_address:
        return {"success": False, "message": "设备未配置 IP 地址"}

    if _is_windows(device):
        if _is_ssh_port_open(device):
            result = _ssh_shutdown(device, username, password)
            return _verify_power_result(device, result, reboot=False)
        # Win32 RPC first; on failure fall back to RDP keystroke injection.
        win32_result = _win32_shutdown(device, reboot=False)
        if win32_result.get("success"):
            return _verify_power_result(device, win32_result, reboot=False)
        rdp_result = _run_async(_rdp_shutdown(device, reboot=False))
        # Carry the Win32 reason so a verification failure surfaces the real
        # root cause (e.g. access denied) rather than a generic message.
        result = rdp_result if rdp_result.get("success") else win32_result
        return _verify_power_result(
            device, result, reboot=False, win32_reason=win32_result.get("message", "")
        )

    return _verify_power_result(
        device, _ssh_shutdown(device, username, password), reboot=False
    )


def reboot_device(device: Device, username: str, password: str) -> dict:
    """Reboot the device via SSH, Win32 RPC, or RDP, then verify it powers off."""
    if not device.ip_address:
        return {"success": False, "message": "设备未配置 IP 地址"}

    if _is_windows(device):
        if _is_ssh_port_open(device):
            result = _ssh_reboot(device, username, password)
            return _verify_power_result(device, result, reboot=True)
        win32_result = _win32_shutdown(device, reboot=True)
        if win32_result.get("success"):
            return _verify_power_result(device, win32_result, reboot=True)
        rdp_result = _run_async(_rdp_shutdown(device, reboot=True))
        result = rdp_result if rdp_result.get("success") else win32_result
        return _verify_power_result(
            device, result, reboot=True, win32_reason=win32_result.get("message", "")
        )

    return _verify_power_result(
        device, _ssh_reboot(device, username, password), reboot=True
    )


def _ssh_shutdown(device: Device, username: str, password: str) -> dict:
    """Shut down the device via SSH. A connection drop after the command is sent
    is treated as tentative success (the device is likely shutting down); the
    caller verifies by polling the device offline."""
    cmd = "shutdown /s /f /t 0" if _is_windows(device) else "shutdown -h now"
    db = SessionLocal()
    try:
        try:
            client, _key = connect_device(device, username, password, db=db, timeout=10)
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
            return {"success": False, "method": "ssh", "message": f"无法连接设备: {exc}"}

        try:
            exit_code, out, err = exec_ssh_command(client, cmd, timeout=10)
            logger.info(
                "Shutdown %s: exit=%s out=%s err=%s", device.ip_address, exit_code, out, err
            )
            if exit_code == 0:
                return {"success": True, "method": "ssh", "message": "关机指令已发送"}
            return {
                "success": False,
                "method": "ssh",
                "message": f"关机命令执行失败 (exit={exit_code}): {err or out}",
            }
        except (socket.timeout, OSError, EOFError, paramiko.SSHException) as exc:
            # Command already sent — a dropped connection usually means the device
            # is shutting down. Report tentative success; caller verifies.
            logger.info(
                "Shutdown %s: connection lost after send (%s) — verifying",
                device.ip_address,
                exc,
            )
            return {
                "success": True,
                "method": "ssh",
                "message": "关机指令已发送（连接已断开，正在验证设备离线）",
            }
        except Exception as exc:
            logger.exception("Shutdown error for %s", device.ip_address)
            return {"success": False, "method": "ssh", "message": f"关机失败: {exc}"}
        finally:
            client.close()
    finally:
        db.close()


def _ssh_reboot(device: Device, username: str, password: str) -> dict:
    """Reboot the device via SSH. See _ssh_shutdown for connection-drop handling."""
    cmd = "shutdown /r /f /t 0" if _is_windows(device) else "reboot"
    db = SessionLocal()
    try:
        try:
            client, _key = connect_device(device, username, password, db=db, timeout=10)
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
            return {"success": False, "method": "ssh", "message": f"无法连接设备: {exc}"}

        try:
            exit_code, out, err = exec_ssh_command(client, cmd, timeout=10)
            logger.info(
                "Reboot %s: exit=%s out=%s err=%s", device.ip_address, exit_code, out, err
            )
            if exit_code == 0:
                return {"success": True, "method": "ssh", "message": "重启指令已发送"}
            return {
                "success": False,
                "method": "ssh",
                "message": f"重启命令执行失败 (exit={exit_code}): {err or out}",
            }
        except (socket.timeout, OSError, EOFError, paramiko.SSHException) as exc:
            logger.info(
                "Reboot %s: connection lost after send (%s) — verifying",
                device.ip_address,
                exc,
            )
            return {
                "success": True,
                "method": "ssh",
                "message": "重启指令已发送（连接已断开，正在验证设备离线）",
            }
        except Exception as exc:
            logger.exception("Reboot error for %s", device.ip_address)
            return {"success": False, "method": "ssh", "message": f"重启失败: {exc}"}
        finally:
            client.close()
    finally:
        db.close()


def _run_async(coro):
    """Run an async coroutine from sync context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=30)
    else:
        return asyncio.run(coro)
