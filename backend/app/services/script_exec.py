"""脚本/电源操作的设备执行内核。

从 routers/scripts.py 下沉而来:service 层(automation/scheduler)此前反向
import router 的私有函数,层次倒置;执行内核属于 service,router 只做参数
校验、权限检查和响应组装。

签名从「整个 ScriptExecuteRequest body」改为显式关键字参数——service 调用方
(自动化任务/定时调度)不再需要为满足 schema 校验构造假 body。

行为口径与下沉前完全一致:SSH 并发信号量、通道分发(Windows→WinRM,
Linux→SSH)、错误兜底形状。
"""

import logging
import threading

from pydantic import BaseModel

from app.models.device import Device
from app.services.device_credentials import resolve_credentials
from app.services.power import _exec_ssh_command, reboot_device, shutdown_device

logger = logging.getLogger(__name__)

MAX_CONCURRENT_SSH = 10
_ssh_sem = threading.Semaphore(MAX_CONCURRENT_SSH)


class DeviceResult(BaseModel):
    # PVE 虚拟机(AgentTarget)没有真实 device.id,允许 None。
    device_id: int | None
    device_name: str
    ip_address: str | None
    success: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""


class PowerDeviceResult(BaseModel):
    device_id: int | None
    device_name: str
    ip_address: str | None
    os_system: str | None
    success: bool
    message: str = ""


def _resolve_creds(
    device: Device,
    username: str | None,
    password: str | None,
    ssh_key: str | None,
) -> tuple[str, str, str]:
    """Resolve SSH credentials: request-level > device-bound > defaults."""
    resolved = resolve_credentials(device, username, password, ssh_key)
    return resolved[0] or "root", resolved[1], resolved[2]


def run_script_on_device(
    device: Device,
    command: str,
    timeout: int = 30,
    *,
    username: str | None = None,
    password: str | None = None,
    ssh_key: str | None = None,
) -> DeviceResult:
    """Execute a single command on one device (called inside a worker thread).

    通道分发:Windows → WinRM(命令按 PowerShell 执行);其余 → SSH。
    """
    with _ssh_sem:
        user, pwd, key = _resolve_creds(device, username, password, ssh_key)
        try:
            if device.is_windows:
                from app.services.winrm import run_on_device

                exit_code, out, err = run_on_device(
                    device, user, pwd, command, timeout=timeout
                )
            else:
                exit_code, out, err = _exec_ssh_command(
                    device, user, pwd, command, timeout, private_key=key or None
                )
            return DeviceResult(
                device_id=device.id,
                device_name=device.name,
                ip_address=device.ip_address,
                success=(exit_code == 0),
                exit_code=exit_code,
                stdout=out,
                stderr=err,
            )
        except Exception as exc:
            return DeviceResult(
                device_id=device.id,
                device_name=device.name,
                ip_address=device.ip_address,
                success=False,
                error=str(exc),
            )


def run_power_on_device(
    device: Device,
    action: str,
    *,
    username: str | None = None,
    password: str | None = None,
    ssh_key: str | None = None,
) -> PowerDeviceResult:
    """Run shutdown/reboot on one device in a worker thread.

    Uses the device-owned credential (power operations follow the same trust
    path as the single-device power button).
    """
    user, pwd, key = _resolve_creds(device, username, password, ssh_key)
    user = user or "root"

    try:
        if action == "shutdown":
            result = shutdown_device(device, user, pwd, private_key=key or None)
        else:
            result = reboot_device(device, user, pwd, private_key=key or None)
        # power.py returns {"success": bool, "message": str}
        return PowerDeviceResult(
            device_id=device.id,
            device_name=device.name,
            ip_address=device.ip_address,
            os_system=device.os_system,
            success=bool(result.get("success")),
            message=result.get("message", ""),
        )
    except Exception as exc:
        logger.exception("Power action %s failed on %s", action, device.ip_address)
        return PowerDeviceResult(
            device_id=device.id,
            device_name=device.name,
            ip_address=device.ip_address,
            os_system=device.os_system,
            success=False,
            message=f"执行失败: {exc}",
        )
