"""
Terminal service — handles SSH connections via paramiko and bridges them
to the async WebSocket world.
"""

import asyncio
import logging
import socket
import uuid
from typing import Optional

import paramiko

from app.models.device import Device
from app.services.ssh import HostKeyMismatchError, connect_device

logger = logging.getLogger(__name__)


class SSHTerminalSession:
    """Wraps a single paramiko SSH interactive shell session."""

    def __init__(
        self,
        device: Device,
        username: str,
        password: str,
        private_key: Optional[str] = None,
        cols: int = 80,
        rows: int = 24,
        db=None,
        initial_command: Optional[str] = None,
    ):
        self.device = device
        self.username = username
        self.password = password
        self.private_key = private_key
        self.cols = cols
        self.rows = rows
        self.session_id = str(uuid.uuid4())
        self._db = db  # optional session for TOFU host-key persistence
        # 连接建立后自动执行的命令(如 `docker exec -it <容器> sh` 进入容器)
        self.initial_command = initial_command

        self._client: Optional[paramiko.SSHClient] = None
        self._channel: Optional[paramiko.Channel] = None
        self._connected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the SSH connection and request an interactive shell."""
        try:
            self._client, _key = await asyncio.to_thread(
                connect_device,
                self.device,
                self.username,
                self.password,
                db=self._db,
                timeout=10,
                banner_timeout=15,
                private_key=self.private_key,
            )
        except HostKeyMismatchError as exc:
            raise ConnectionError(
                f"SSH host key mismatch for {self.device.ip_address} — possible MITM"
            ) from exc
        except paramiko.AuthenticationException as exc:
            raise ConnectionError(
                f"SSH authentication failed for {self.device.ip_address}: {exc}"
            ) from exc
        except socket.timeout as exc:
            raise ConnectionError(
                f"SSH connection to {self.device.ip_address}:{self.device.ssh_port} timed out"
            ) from exc
        except paramiko.SSHException as exc:
            raise ConnectionError(
                f"SSH connection error to {self.device.ip_address}: {exc}"
            ) from exc
        except OSError as exc:
            raise ConnectionError(
                f"Cannot reach {self.device.ip_address}:{self.device.ssh_port} — {exc}"
            ) from exc

        try:
            self._channel = self._client.invoke_shell(
                term="xterm-256color",
                width=self.cols,
                height=self.rows,
            )
        except Exception as exc:
            # 认证已通过但 shell 开不起来(MaxSessions 耗尽、shell 被禁等):
            # 必须关掉刚建立的 Transport,否则 TCP 连接与 paramiko 线程随重连累积。
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            raise ConnectionError(
                f"SSH shell open failed for {self.device.ip_address}: {exc}"
            ) from exc

        # Force UTF-8 locale so Chinese characters render correctly
        try:
            self._channel.set_environment_variable("LANG", "en_US.UTF-8")
        except Exception:
            pass

        self._connected = True

        # Send export command to force UTF-8 on the remote shell.
        # Many servers ignore set_environment_variable, so this is the reliable fallback.
        await asyncio.sleep(0.1)
        self._channel.send(
            "export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 2>/dev/null\n".encode("utf-8")
        )

        # 进入容器等场景:连接后自动执行指定命令(命令已在路由层校验)
        if self.initial_command:
            await asyncio.sleep(0.1)
            self._channel.send((self.initial_command + "\n").encode("utf-8"))

    async def close(self) -> None:
        """Shut down the channel and client."""
        self._connected = False
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                pass
            self._channel = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------
    # I/O helpers (all run paramiko calls in a thread to stay async-safe)
    # ------------------------------------------------------------------

    async def send_input(self, data: str) -> None:
        """Write *data* to the shell stdin."""
        if self._channel is None or self._channel.closed:
            return
        await asyncio.to_thread(self._channel.send, data)

    async def recv_output(self, size: int = 4096) -> Optional[str]:
        """Read available output from the shell (non-blocking in thread)."""
        if self._channel is None or self._channel.closed:
            return None
        try:
            data = await asyncio.to_thread(self._channel.recv, size)
            if not data:
                return None
            return data.decode("utf-8", errors="replace")
        except (OSError, paramiko.SSHException):
            return None

    async def resize_pty(self, cols: int, rows: int) -> None:
        """Resize the remote PTY."""
        self.cols = cols
        self.rows = rows
        if self._channel is not None and not self._channel.closed:
            await asyncio.to_thread(self._channel.resize_pty, cols, rows)

    @property
    def is_connected(self) -> bool:
        return (
            self._connected and self._channel is not None and not self._channel.closed
        )


# ======================================================================
# High-level helper functions used directly by the router
# ======================================================================


async def create_ssh_connection(
    device: Device,
    username: str,
    password: str,
    private_key: Optional[str] = None,
    cols: int = 80,
    rows: int = 24,
    db=None,
    initial_command: Optional[str] = None,
) -> SSHTerminalSession:
    """Convenience wrapper — create and connect in one call."""
    session = SSHTerminalSession(
        device=device,
        username=username,
        password=password,
        private_key=private_key,
        cols=cols,
        rows=rows,
        db=db,
        initial_command=initial_command,
    )
    await session.connect()
    return session
