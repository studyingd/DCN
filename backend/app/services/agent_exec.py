"""远程取证执行层:凭据解析/SSH 会话复用/命令执行(从 agent.py 拆出,Phase-1)。

_SshSession 懒开连接由 _open_lock 串行;connect_device 对非 ORM 目标
(AgentTarget)跳过 TOFU host key 回写。
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy.orm import Session

from app.models.device import Device
from app.services.agent_types import AgentError
from app.services.crypto import decrypt

logger = logging.getLogger(__name__)


def _resolve_creds(db: Session, device: Device) -> tuple[str, str, str] | None:
    direct_username = getattr(device, "username", None)
    direct_password = getattr(device, "password", None)
    direct_key = getattr(device, "ssh_key", None)
    if direct_username and (direct_password or direct_key):
        return direct_username, direct_password or "", direct_key or ""
    username = getattr(device, "remote_username", None)
    password = (
        decrypt(device.remote_password_enc)
        if getattr(device, "remote_password_enc", None)
        else ""
    )
    ssh_key = (
        decrypt(device.remote_ssh_key_enc)
        if getattr(device, "remote_ssh_key_enc", None)
        else ""
    )
    if not username or (not password and not ssh_key):
        return None
    return username, password, ssh_key


class _SshSession:
    """一次诊断内复用的 SSH 连接(Linux 目标)。

    实测目标机(CentOS 7 / OpenSSH 7.4)每次新建连接的 paramiko 认证阶段要 5~10s
    (服务端 sshd 反查/PAM 阶段耗时)，而诊断命令本身只要 0.1s 左右。归因分析一轮
    通常执行 2 项以上诊断，逐条重连会把整个分析拖成分钟级。

    这里在一轮 ``_loop`` 内复用同一条 transport，把 N 次握手压成 1 次。
    每个诊断运行独占一个实例;交互 ``_loop`` 单线程，单-pass 归因并行
    取证，懒开连接由 ``_open_lock`` 串行(paramiko transport 本身支持
    多线程并发使用)。
    """

    def __init__(self) -> None:
        self._client = None
        self._open_lock = threading.Lock()

    @staticmethod
    def _alive(client) -> bool:
        try:
            transport = client.get_transport()
            return transport is not None and transport.is_active()
        except Exception:
            return False

    def _open(self, db: Session, device, username, password, ssh_key, timeout: int):
        from app.services.ssh import connect_device, open_ssh_client

        if isinstance(device, Device):
            # connect_device 负责 TOFU：首次连上时把 host key 写回设备记录。
            client, _key = connect_device(
                device,
                username,
                password,
                db=db,
                timeout=min(timeout, 15),
                private_key=ssh_key or None,
            )
            return client
        client, _key = open_ssh_client(
            device.ip_address,
            device.ssh_port,
            username,
            password,
            timeout=min(timeout, 15),
            pinned_key_b64=device.ssh_host_key or None,
            private_key=ssh_key or None,
            allow_tofu=not bool(device.ssh_host_key),
        )
        return client

    def exec(
        self,
        db: Session,
        device,
        username: str,
        password: str,
        ssh_key: str,
        command: str,
        timeout: int,
    ) -> tuple[int, str, str]:
        from app.services.ssh import exec_ssh_command

        if self._client is not None and not self._alive(self._client):
            self.close()
        if self._client is None:
            with self._open_lock:
                if self._client is None:
                    self._client = self._open(
                        db, device, username, password, ssh_key, timeout
                    )
        try:
            return exec_ssh_command(self._client, command, timeout=timeout)
        except Exception:
            # 连接可能在两条命令之间被对端回收：丢弃缓存、重建后重试一次。
            self.close()
            self._client = self._open(db, device, username, password, ssh_key, timeout)
            return exec_ssh_command(self._client, command, timeout=timeout)

    def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.close()
        except Exception:
            logger.debug("close reused SSH client failed", exc_info=True)


def _exec_diagnostic(
    db: Session,
    device: Device,
    command: str,
    timeout: int,
    session: "_SshSession | None" = None,
) -> tuple[int, str, str]:
    """经设备通道执行注册表命令(Windows→WinRM,Linux→SSH)。"""
    creds = _resolve_creds(db, device)
    if creds is None:
        raise AgentError("设备未绑定有效凭据,无法执行诊断")
    username, password, ssh_key = creds

    if device.is_windows:
        from app.services.winrm import run_on_device

        if isinstance(device, Device):
            return run_on_device(device, username, password, command, timeout=timeout)
        from app.services.winrm import run_powershell

        return run_powershell(
            device.ip_address,
            username,
            password,
            command,
            port=device.winrm_port,
            timeout=timeout,
        )

    if session is not None:
        return session.exec(db, device, username, password, ssh_key, command, timeout)

    from app.services.ssh import exec_on_device, exec_ssh_command, open_ssh_client

    if isinstance(device, Device):
        return exec_on_device(
            device,
            username,
            password,
            command,
            timeout=timeout,
            db=db,
            private_key=ssh_key or None,
        )
    client, _key = open_ssh_client(
        device.ip_address,
        device.ssh_port,
        username,
        password,
        timeout=min(timeout, 15),
        pinned_key_b64=device.ssh_host_key or None,
        private_key=ssh_key or None,
        allow_tofu=not bool(device.ssh_host_key),
    )
    try:
        return exec_ssh_command(client, command, timeout=timeout)
    finally:
        client.close()


def _diagnostic_output(exit_code: int, out: str, err: str) -> str:
    """挑出该交给 LLM 的那一段输出。

    成功时只用 stdout。旧写法 ``out or err`` 在 stdout 为空时回退到 stderr，
    而 stderr 是 PowerShell 的告警/进度流。对诊断而言「命令成功且无输出」
    本身就是结论(例如近 24h 无服务失败事件)，回退之后模型读到的是几百字符
    CLIXML 噪音，得先自己看懂那段 XML 才能得出「无异常」——白耗 token、
    且结论靠运气。exit_code 已经能区分成败，失败时才需要 stderr 当错误信息。
    """
    if exit_code == 0:
        return out or ""
    return err or out or ""
