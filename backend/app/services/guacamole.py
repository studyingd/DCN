"""
Guacamole protocol service — async TCP bridge to guacd for browser-based RDP.

The Guacamole protocol uses length-prefixed text instructions:
    LENGTH.VALUE,LENGTH.VALUE,...;
Example: 6.select,3.rdp;

This module implements the minimal protocol subset needed to:
1. Open a TCP connection to guacd
2. Perform the handshake (select RDP protocol, send connection params)
3. Bidirectionally forward instructions between WebSocket and guacd
"""

import asyncio
import logging
import socket
from typing import Optional

from app.config import GUAC_RDP_COLOR_DEPTH, GUAC_RDP_IGNORE_CERT, GUAC_RDP_SECURITY
from app.validators import is_blocked_host

logger = logging.getLogger(__name__)


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def start_rdp_relay(
    target_host: str, target_port: int
) -> tuple[asyncio.AbstractServer, int]:
    """
    Start a local TCP relay that forwards connections to target_host:target_port.
    Returns (server, local_port).

    Rejects link-local / loopback / metadata targets (SSRF defense) — the host
    is user-configurable via device.ip_address.
    """
    if is_blocked_host(target_host):
        raise ValueError(
            f"Refusing to relay to blocked host {target_host} (link-local/loopback/metadata)"
        )

    async def _handle_relay_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        try:
            remote_reader, remote_writer = await asyncio.open_connection(
                target_host, target_port
            )
        except Exception as e:
            logger.error("Relay: cannot reach %s:%d: %s", target_host, target_port, e)
            writer.close()
            return
        try:

            async def pump(dst: asyncio.StreamWriter, src: asyncio.StreamReader):
                try:
                    while True:
                        data = await src.read(65536)
                        if not data:
                            break
                        dst.write(data)
                        await dst.drain()
                except Exception:
                    pass
                finally:
                    try:
                        dst.close()
                    except Exception:
                        pass

            await asyncio.gather(
                pump(writer, remote_reader),
                pump(remote_writer, reader),
            )
        finally:
            for w in (writer, remote_writer):
                try:
                    w.close()
                except Exception:
                    pass

    port = _find_free_port()
    server = await asyncio.start_server(_handle_relay_client, "127.0.0.1", port)
    logger.info(
        "RDP relay started on 127.0.0.1:%d -> %s:%d", port, target_host, target_port
    )
    return server, port


def _build_instruction(opcode: str, *args: str) -> bytes:
    """Build a Guacamole protocol instruction from opcode and arguments.

    The length prefix counts **Unicode characters** (code points), per the
    Guacamole protocol spec — this matches both guacd (C) and the browser's
    guacamole-common-js parser, which slices with ``String.substring`` (UTF-16
    code units, equal to code-point count for BMP text incl. Chinese).

    Counting UTF-8 **bytes** here instead is wrong: for any multi-byte arg the
    prefix over-counts relative to what the JS parser expects, so the browser
    slices the element at the wrong offset, every subsequent instruction in the
    same frame is misaligned, and the stream effectively dies (acks stop being
    dispatched → upload hangs in flushing). Historical ASCII-only traffic hid
    this because byte count == char count for ASCII.
    """

    def _elem(text: str) -> str:
        return f"{len(text)}.{text}"

    parts = [_elem(opcode)] + [_elem(arg) for arg in args]
    return (",".join(parts) + ";").encode("utf-8")


def _parse_instruction(data: bytes) -> tuple[Optional[tuple[str, list[str]]], bytes]:
    """
    Parse one complete Guacamole instruction from a byte buffer.

    The length prefix is a **character count** (protocol spec). ``data`` is raw
    UTF-8 bytes, so a character count of N does NOT mean N bytes — we must scan
    past N UTF-8 characters to find the element's byte boundary. Slicing bytes
    at ``start + length`` would land mid-character for any multi-byte element.

    Returns (instruction_tuple, remaining_buffer) where instruction_tuple is
    (opcode, [args]) or None if no complete instruction is available yet.
    """
    elements = []
    idx = 0

    while idx < len(data):
        # Find the '.' separator
        dot_pos = data.find(b".", idx)
        if dot_pos == -1:
            return None, data  # Incomplete length prefix

        try:
            char_count = int(data[idx:dot_pos])
        except ValueError:
            raise ValueError(f"Invalid Guacamole instruction length at byte {idx}")

        value_start = dot_pos + 1

        # Advance past `char_count` UTF-8 characters to find the byte boundary.
        # UTF-8 continuation bytes are 0b10xxxxxx (0x80-0xBF); all others start
        # a new character. Counting non-continuation bytes == counting characters.
        # After counting the char_count-th leading byte we must still skip its
        # trailing continuation bytes, otherwise we land mid-character.
        pos = value_start
        chars_seen = 0
        while pos < len(data):
            if (data[pos] & 0xC0) != 0x80:
                if chars_seen == char_count:
                    break  # 已越过 char_count 个完整字符
                chars_seen += 1
            pos += 1
        if chars_seen < char_count:
            return None, data  # Incomplete value(字符不够)
        value_end = pos

        elements.append(data[value_start:value_end].decode("utf-8"))

        # Check what follows the value
        idx = value_end
        if idx >= len(data):
            return None, data  # Need more data

        next_byte = data[idx : idx + 1]
        if next_byte == b",":
            idx += 1  # More elements follow
        elif next_byte == b";":
            idx += 1  # End of instruction
            if not elements:
                return None, data[idx:]
            opcode = elements[0]
            args = elements[1:]
            return (opcode, args), data[idx:]
        else:
            raise ValueError(f"Unexpected delimiter: {next_byte!r}")

    return None, data


def _parse_instruction_frame(
    data: bytes,
) -> tuple[Optional[tuple[str, list[str]]], bytes, bytes]:
    """Parse one instruction while preserving its exact wire bytes."""
    parsed, remaining = _parse_instruction(data)
    if parsed is None:
        return None, b"", data
    frame = data[: len(data) - len(remaining)]
    return parsed, frame, remaining


class GuacamoleSession:
    """Manages a TCP connection to guacd and the Guacamole protocol handshake."""

    def __init__(self, host: str = "localhost", port: int = 4822):
        self.host = host
        self.port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._buffer = b""
        self._connected = False

    async def connect(self) -> None:
        """Open TCP connection to guacd."""
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        # Guacamole carries many small interactive instructions (keyboard,
        # mouse and incremental drawing updates). Disable Nagle buffering so
        # those packets are delivered immediately instead of waiting for a
        # coalescing window.
        try:
            sock = (
                self._writer.transport.get_extra_info("socket")
                if self._writer.transport
                else None
            )
            if sock is not None:
                import socket as _socket

                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        except Exception:
            logger.debug("Unable to set TCP_NODELAY for guacd socket", exc_info=True)
        self._connected = True
        logger.info("Connected to guacd at %s:%d", self.host, self.port)

    async def handshake_rdp(
        self,
        hostname: str,
        port: int,
        username: str = "",
        password: str = "",
        width: int = 1024,
        height: int = 768,
        dpi: int = 96,
        session_id: str = "",
        enable_drive: bool = True,
    ) -> None:
        """
        Perform the Guacamole protocol handshake for an RDP connection.

        guacd 1.5.0 protocol:
        1. Send 'select' to choose RDP protocol
        2. Read 'args' listing required parameters
        3. Send 'size', 'audio', 'video', 'image', then 'connect' with values
        4. Read 'ready' confirmation

        ``session_id`` selects a per-session virtual drive path under
        ``/var/guacd/drives`` so each RDP session gets its own GuacamoleFS
        (enables browser⇄Windows file transfer). Empty disables the drive.
        """
        # Step 1: Select RDP protocol
        await self.write_instruction("select", "rdp")

        # Step 2: Read args list (required connection parameters)
        opcode, args = await self.read_instruction()
        if opcode != "args":
            raise ConnectionError(f"Expected args from guacd, got: {opcode}")

        # Step 3: Send display size, audio, video, image, then connect with RDP params
        await self.write_instruction("size", str(width), str(height), str(dpi))
        await self.write_instruction("audio")
        await self.write_instruction("video")
        await self.write_instruction("image", "image/png", "image/jpeg")

        # Step 4: Build connect instruction with RDP parameters.
        # Build a name→value map; for each arg name guacd requested, look up the value
        # (or empty string for unknowns). This makes us version-independent: if guacd
        # adds/removes args in a future version, we still send exactly the right count.

        # Per-session virtual drive (GuacamoleFS) for browser⇄Windows file transfer.
        # Sanitize session_id defensively — it's normally a UUID, but guard against
        # path traversal regardless. When empty, the drive is disabled.
        safe_session = (
            "".join(c for c in session_id if c.isalnum() or c in "-_")
            if enable_drive
            else ""
        )
        drive_path = f"/var/guacd/drives/{safe_session}" if safe_session else ""

        values_by_name = {
            # In Guacamole protocol, the first arg's NAME is the version identifier
            # (e.g., "VERSION_1_5_0" or "VERSION_1_6_0"). Send the matching version value.
            "VERSION_1_5_0": "VERSION_1_5_0",
            "VERSION_1_6_0": "VERSION_1_5_0",  # we still speak the 1.5.0 protocol
            "hostname": hostname,
            "port": str(port),
            "domain": "",
            "username": username,
            "password": password,
            "width": str(width),
            "height": str(height),
            "dpi": str(dpi),
            "initial-program": "",
            "color-depth": GUAC_RDP_COLOR_DEPTH
            if GUAC_RDP_COLOR_DEPTH in {"8", "16", "24", "32"}
            else "16",
            "disable-audio": "",
            "enable-printing": "",
            "printer-name": "",
            "enable-drive": "true" if drive_path else "",
            "drive-name": "GuacamoleFS" if drive_path else "",
            "drive-path": drive_path,
            "create-drive-path": "true" if drive_path else "",
            "disable-download": "",
            "disable-upload": "",
            "console": "",
            "console-audio": "",
            "server-layout": "",
            "security": GUAC_RDP_SECURITY,
            "ignore-cert": "true" if GUAC_RDP_IGNORE_CERT else "false",
            "disable-auth": "",
            "remote-app": "",
            "remote-app-dir": "",
            "remote-app-args": "",
            "static-channels": "",
            "client-name": "",
            # Keep the RDP stream lightweight for interactive management. These
            # visual effects create continuous bitmap traffic and noticeable
            # input lag on high-latency links.
            "enable-wallpaper": "",
            "enable-theming": "",
            "enable-font-smoothing": "",
            "enable-full-window-drag": "",
            "enable-desktop-composition": "",
            "enable-menu-animations": "",
            "disable-bitmap-caching": "",
            "disable-offscreen-caching": "",
            "disable-glyph-caching": "",
            "preconnection-id": "",
            "preconnection-blob": "",
            "timezone": "",
            "enable-sftp": "",
            "sftp-hostname": "",
            "sftp-host-key": "",
            "sftp-port": "",
            "sftp-username": "",
            "sftp-password": "",
            "sftp-private-key": "",
            "sftp-passphrase": "",
            "sftp-directory": "",
            "sftp-root-directory": "",
            "sftp-server-alive-interval": "",
            "sftp-disable-download": "",
            "sftp-disable-upload": "",
            "resize-method": "display-update",
            "enable-audio-input": "",
            "enable-touch": "",
            "read-only": "",
            "gateway-hostname": "",
            "gateway-port": "",
            "gateway-domain": "",
            "gateway-username": "",
            "gateway-password": "",
            "load-balance-info": "",
            "disable-copy": "",
            "disable-paste": "",
            "wol-send-packet": "",
            "wol-mac-addr": "",
            "wol-broadcast-addr": "",
            "wol-udp-port": "",
            "wol-wait-time": "",
            "force-lossless": "",
            "normalize-clipboard": "",
            # Args added in guacd 1.6.0+ — defaults to empty
            "enable-drive-mapping": "",
            "drive-write": "true" if drive_path else "",
            "kbd-layout": "",
            "host-key": "",
            "relaxed-order": "",
            "ticket": "",
        }
        # Map each arg guacd requested to its value. Unknown args → empty string.
        connect_args = [values_by_name.get(name, "") for name in args]
        logger.info(
            "RDP handshake: %d args from guacd, sending %d values",
            len(args),
            len(connect_args),
        )
        await self.write_instruction("connect", *connect_args)

        # Step 5: Wait for ready
        opcode, args = await self.read_instruction()
        if opcode == "error":
            raise ConnectionError(f"guacd RDP connection failed: {args}")
        if opcode != "ready":
            raise ConnectionError(f"Expected ready from guacd, got: {opcode}")

        logger.info("RDP session established to %s:%d", hostname, port)

    async def read_instruction(self) -> tuple[str, list[str]]:
        """Read the next complete Guacamole instruction from guacd."""
        while True:
            result, self._buffer = _parse_instruction(self._buffer)
            if result is not None:
                return result

            # Need more data from TCP
            if self._reader is None:
                raise ConnectionError("Not connected to guacd")
            chunk = await self._reader.read(65536)
            if not chunk:
                raise ConnectionError("guacd connection closed")
            self._buffer += chunk

    async def read_instruction_batch(
        self, max_instructions: int = 256
    ) -> list[tuple[str, list[str]]]:
        """Read one or more complete instructions from a single TCP batch.

        guacd commonly writes dozens of drawing instructions together. Sending
        each one as an individual WebSocket frame creates substantial framing,
        scheduling and browser dispatch overhead. Keep instruction boundaries
        intact while forwarding all currently buffered instructions together.
        """
        first = await self.read_instruction()
        batch = [first]
        while len(batch) < max_instructions:
            result, remaining = _parse_instruction(self._buffer)
            if result is None:
                break
            self._buffer = remaining
            batch.append(result)
        return batch

    async def read_instruction_batch_raw(
        self, max_instructions: int = 512, coalesce_ms: float = 8.0
    ) -> tuple[list[tuple[str, list[str]]], bytes]:
        """Read a batch and keep its original wire representation.

        guacd 以小碎包下发(实测平均 ~2.7KB/TCP 包,一次屏幕刷新含上千条 copy/
        img 指令)。若每个 TCP 包都单独成一个 WebSocket 帧,浏览器事件循环会被
        数百帧淹没——表现为「画面响应非常卡」。这里在拿到至少一条指令后,用极
        短超时再捞一次 socket,把 guacd 紧跟着发的后续小包合并进**同一个** WS
        帧,大幅降低帧数与浏览器调度开销。``coalesce_ms`` 是攒批等待上限,
        过大会增加交互延迟,8ms 是一个往返内人眼无感的量级。
        """
        batch: list[tuple[str, list[str]]] = []
        frames: list[bytes] = []
        first = True
        while len(batch) < max_instructions:
            result, frame, remaining = _parse_instruction_frame(self._buffer)
            if result is None:
                if self._reader is None:
                    raise ConnectionError("Not connected to guacd")
                try:
                    if first:
                        # 阻塞等第一条指令(连接空闲时在此挂起)
                        chunk = await self._reader.read(65536)
                    else:
                        # 已有数据:短暂等待把紧跟着的小包也攒进来
                        chunk = await asyncio.wait_for(
                            self._reader.read(65536), timeout=coalesce_ms / 1000.0
                        )
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    raise ConnectionError("guacd connection closed")
                self._buffer += chunk
                continue
            first = False
            self._buffer = remaining
            batch.append(result)
            frames.append(frame)
        return batch, b"".join(frames)

    async def write_instruction(self, opcode: str, *args: str) -> None:
        """Send a Guacamole instruction to guacd."""
        if self._writer is None:
            raise ConnectionError("Not connected to guacd")
        data = _build_instruction(opcode, *args)
        self._writer.write(data)
        await self._writer.drain()

    async def write_raw(self, data: bytes) -> None:
        """Send raw bytes to guacd (pre-formatted Guacamole instruction)."""
        if self._writer is None:
            raise ConnectionError("Not connected to guacd")
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        """Close the TCP connection to guacd."""
        self._connected = False
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        self._reader = None

    @property
    def is_connected(self) -> bool:
        return (
            self._connected
            and self._writer is not None
            and not self._writer.is_closing()
        )
