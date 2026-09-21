"""
WebSocket terminal router.

Provides a single endpoint  ``/ws/terminal/{device_id}``  that brokers an
interactive SSH or RDP session between the browser and the target device.

Authentication and resolved credentials are carried in a short-lived,
single-use ticket issued by ``POST /api/terminal/ticket``.  The WebSocket URL
therefore never contains a JWT, username, or password.
"""

import asyncio
import json
import logging
import os
import uuid

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.config import GUACD_HOST, GUACD_PORT, GUACD_USE_RELAY
from app.database import SessionLocal, get_db
from app.models.device import Device
from app.models.user import User
from app.services import token_blacklist
from app.services.auth import decode_token
from app.services.crypto import decrypt
from app.services.guacamole import (
    GuacamoleSession,
    _build_instruction,
    _parse_instruction,
    start_rdp_relay,
)
from app.services.permissions import (
    require_permission,
    user_can_access_device,
    user_can_use_credential,
    user_has_permission,
)
from app.services.terminal import create_ssh_connection
from app.services.ws_ticket import (
    consume_credential,
    consume_ticket,
    issue_credential,
    issue_ticket,
    peek_ticket,
)
from app.validators import validate_container_name

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminal"])


class TerminalTicketRequest(BaseModel):
    device_id: int = Field(ge=1)
    conn_type: str = Field(default="ssh", min_length=1, max_length=8)
    credential_id: int | None = None
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=4096)
    ssh_key: str | None = Field(default=None, max_length=16384)
    width: int = Field(default=1024, ge=320, le=7680)
    height: int = Field(default=768, ge=240, le=4320)
    container: str | None = None  # 非空时进入该容器的交互式 shell(docker exec)

    @field_validator("conn_type")
    @classmethod
    def _validate_conn_type(cls, value):
        if value not in {"ssh", "rdp"}:
            raise ValueError("conn_type 必须是 ssh 或 rdp")
        return value


def _access_jti(request: Request) -> str:
    """Extract the current access token's jti (for blacklist re-check at WS time)."""
    token = request.cookies.get("dcn_access")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
    if not token:
        return ""
    try:
        return decode_token(token).get("jti", "")
    except Exception:
        return ""


@router.post("/api/terminal/ticket")
def create_terminal_ticket(
    body: TerminalTicketRequest,
    request: Request,
    current_user: User = Depends(require_permission("device:remote")),
    db: Session = Depends(get_db),
):
    """Issue a single-use terminal ticket (cookie-auth).

    The ticket carries server-resolved credentials so the WebSocket URL never
    contains the JWT, username, or password. The WS consumes the ticket within
    a short TTL.
    """

    device = db.query(Device).filter(Device.id == body.device_id).first()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    if not user_can_access_device(current_user, body.device_id, db):
        raise HTTPException(status_code=403, detail="无权访问该设备")

    # 平台约定 Windows 设备不装 SSH——远程操作用 RDP(WinRM 无交互式 shell)
    if body.conn_type == "ssh" and device.is_windows:
        raise HTTPException(
            status_code=400, detail="Windows 设备不支持 SSH 终端，请使用 RDP 连接"
        )

    # 容器终端仅支持 SSH(Linux 容器);校验容器名防注入
    container: str | None = None
    if body.container:
        if body.conn_type != "ssh":
            raise HTTPException(status_code=400, detail="容器终端仅支持 SSH 连接")
        try:
            container = validate_container_name(body.container)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not user_can_use_credential(current_user, device, body.credential_id, db):
        raise HTTPException(status_code=403, detail="该凭据未绑定到目标设备")
    username, password = _resolve_credentials(
        device, body.username or "", body.password, db
    )
    private_key = body.ssh_key or (
        decrypt(device.remote_ssh_key_enc) if device.remote_ssh_key_enc else ""
    )
    if body.conn_type == "ssh" and not username:
        username = "root"

    credential_ref = issue_credential(
        {
            "username": username,
            "password": password,
            "private_key": private_key,
        }
    )
    payload = {
        "user_id": current_user.id,
        "system_username": current_user.username,
        "device_id": body.device_id,
        "conn_type": body.conn_type,
        "credential_ref": credential_ref,
        "width": body.width,
        "height": body.height,
        "container": container,
        "access_jti": _access_jti(request),
        "session_version": int(getattr(current_user, "session_version", 0)),
    }
    return {"ticket": issue_ticket(payload)}


def _get_device(device_id: int) -> Device | None:
    """Fetch a device by primary key from its own short-lived session."""
    db: Session = SessionLocal()
    try:
        return db.query(Device).filter(Device.id == device_id).first()
    finally:
        db.close()


def _resolve_credentials(
    device: Device,
    username: str,
    password: str | None,
    db: Session | None = None,
) -> tuple[str, str]:
    """Resolve direct credentials, falling back to the device-owned secret."""
    # An empty string is how the terminal UI represents "use the saved
    # device credential". Treat it like an omitted password; otherwise the
    # empty value would override the encrypted device password and force a
    # second prompt on every connection.
    resolved_password = (
        password
        if password
        else (decrypt(device.remote_password_enc) if device.remote_password_enc else "")
    )
    return username or device.remote_username or "", resolved_password


# ------------------------------------------------------------------
# RDP handler via Guacamole
# ------------------------------------------------------------------


# Root of the per-session GuacamoleFS virtual drives (inside the guacd volume).
# The backend can only clean these up directly when it shares the mount (prod);
# on a dev host this rmtree is a harmless no-op unless overridden to a bind mount.
GUACD_DRIVE_ROOT = os.environ.get("GUACD_DRIVE_ROOT", "/var/guacd/drives")


def _cleanup_drive_dir(session_id: str) -> None:
    """Best-effort removal of a session's GuacamoleFS drive directory."""
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    if not safe:
        return
    path = os.path.join(GUACD_DRIVE_ROOT, safe)
    try:
        import shutil

        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        logger.debug("Drive dir cleanup skipped for %s", safe, exc_info=True)


def _sanitize_rdp_instructions(
    instructions: list[tuple[str, list[str]]],
    width: int,
    height: int,
    *,
    drop_server_mouse: bool = False,
) -> tuple[list[bytes], int, int, int]:
    """Sanitize a Guacamole instruction batch.

    ``mouse`` is a server-to-client cursor-position update. It is deliberately
    omitted in that direction: browsers already have the authoritative local
    pointer position, and applying delayed RDP pointer updates to a software
    cursor causes visible jumps/wraparound at the display edges. Client-origin
    mouse input is still forwarded and clamped normally.
    """
    current_width = max(1, int(width))
    current_height = max(1, int(height))
    clamped = 0
    rebuilt: list[bytes] = []

    def clamp_edge(value: int, limit: int) -> int:
        # Avoid exact framebuffer edges, which can be interpreted as wrap
        # points by some FreeRDP/RDP pointer paths.
        inset = 2 if limit > 5 else 0
        minimum = inset
        maximum = max(minimum, limit - 1 - inset)
        return max(minimum, min(maximum, value))

    for opcode, args in instructions:
        safe_args = list(args)
        if opcode == "size" and len(safe_args) >= 2:
            try:
                next_width = int(safe_args[0])
                next_height = int(safe_args[1])
                if next_width > 0 and next_height > 0:
                    current_width = next_width
                    current_height = next_height
            except (TypeError, ValueError):
                pass
        elif opcode == "mouse" and len(safe_args) >= 2:
            if drop_server_mouse:
                continue
            try:
                x = int(float(safe_args[0]))
                y = int(float(safe_args[1]))
                safe_x = clamp_edge(x, current_width)
                safe_y = clamp_edge(y, current_height)
                if safe_x != x or safe_y != y:
                    clamped += 1
                safe_args[0] = str(safe_x)
                safe_args[1] = str(safe_y)
            except (TypeError, ValueError):
                pass
        rebuilt.append(_build_instruction(opcode, *safe_args))

    return rebuilt, current_width, current_height, clamped


# 浏览器 → guacd 方向只有 mouse 需要钳位、size 需要更新当前分辨率，其余指令
# (blob/ack/key/end/sync…) 原样透传即可。Guacamole 指令形如
# ``<len>.<opcode>,<len>.<arg>...;``，参数是数字或 base64，字符集里没有 '.' 和
# ','，因此按精确字面量嗅探 opcode 不会误判。
_MOUSE_OPCODE = b"5.mouse,"
_SIZE_OPCODE = b"4.size,"


def _needs_rdp_rewrite(raw: bytes) -> bool:
    """该上行报文是否需要解析改写；绝大多数 blob 流量在此直接短路。

    上传大文件时每条 6KB 的 blob 都要经过这里，曾经对同一条指令做三次完整
    解析（鼠标计数一次、钳位一次、日志一次）并重新编码，全部发生在事件循环
    上。改成先嗅探 opcode，只在真的有 mouse/size 时才解析。
    """
    return _MOUSE_OPCODE in raw or _SIZE_OPCODE in raw


def _first_mouse_coords(raw: bytes) -> tuple[str, str]:
    """取出报文里第一条 mouse 指令的坐标，仅用于日志。"""
    parsed, _tail = _parse_instruction(raw)
    if parsed and parsed[0] == "mouse" and len(parsed[1]) >= 2:
        return parsed[1][0], parsed[1][1]
    return "?", "?"


def _sanitize_rdp_input(
    raw: bytes, width: int, height: int
) -> tuple[bytes, int, int, int]:
    """Clamp browser-originated mouse coordinates before forwarding to guacd."""
    remaining = raw
    instructions: list[tuple[str, list[str]]] = []
    while remaining:
        parsed, tail = _parse_instruction(remaining)
        if parsed is None or len(tail) >= len(remaining):
            return raw, width, height, 0
        instructions.append(parsed)
        remaining = tail
    rebuilt, current_width, current_height, clamped = _sanitize_rdp_instructions(
        instructions, width, height
    )
    return b"".join(rebuilt), current_width, current_height, clamped


async def _handle_rdp(
    websocket: WebSocket,
    device_id: int,
    username: str,
    password: str,
    user_id: int = 0,
    system_username: str = "",
    width: int = 1024,
    height: int = 768,
    session_id: str | None = None,
    target_host: str | None = None,
    target_port: int | None = None,
    target_name: str | None = None,
    enable_drive: bool = True,
) -> None:
    """Bridge a WebSocket connection to guacd for RDP."""

    logger.info("RDP: requested resolution %dx%d", width, height)

    device = _get_device(device_id) if device_id else None
    if device is None and not target_host:
        await websocket.send_json(
            {"type": "error", "data": f"Device {device_id} not found"}
        )
        await websocket.close(code=4004)
        return

    if not (target_host or (device and device.ip_address)):
        await websocket.send_json(
            {
                "type": "error",
                "data": f"Device {device_id} has no IP address configured",
            }
        )
        await websocket.close(code=4000)
        return

    target_host = target_host or device.ip_address
    rdp_port = target_port or (device.rdp_port if device else 3389) or 3389
    logger.info(
        "RDP: device=%s ip=%s port=%d user=%s",
        target_name or (device.name if device else "PVE guest"),
        target_host,
        rdp_port,
        username,
    )

    rdp_session_id = session_id or str(uuid.uuid4())

    session = GuacamoleSession(host=GUACD_HOST, port=GUACD_PORT)

    # Start TCP relay so guacd (Docker) can reach the RDP target through the host
    relay_server: asyncio.AbstractServer | None = None
    relay_hostname = target_host
    relay_port = rdp_port
    if GUACD_USE_RELAY:
        try:
            relay_server, local_relay_port = await start_rdp_relay(
                target_host, rdp_port
            )
            import platform

            if (
                GUACD_HOST in ("localhost", "127.0.0.1")
                and platform.system() != "Linux"
            ):
                relay_hostname = "host.docker.internal"
            relay_port = local_relay_port
            logger.info(
                "RDP relay started: guacd -> %s:%d -> %s:%d",
                relay_hostname,
                relay_port,
                target_host,
                rdp_port,
            )
        except Exception as exc:
            logger.warning("RDP relay failed to start, connecting directly: %s", exc)
    else:
        logger.info(
            "RDP relay disabled; using direct guacd -> %s:%d", target_host, rdp_port
        )

    try:
        logger.info("RDP: connecting to guacd at %s:%d ...", GUACD_HOST, GUACD_PORT)
        await session.connect()
        logger.info("RDP: connected to guacd, starting handshake ...")
        await session.handshake_rdp(
            hostname=relay_hostname,
            port=relay_port,
            username=username,
            password=password,
            width=width,
            height=height,
            session_id=rdp_session_id,
            enable_drive=enable_drive,
        )
        logger.info("RDP: handshake complete, starting bridge loops")
        rdp_dimensions = {"width": max(1, int(width)), "height": max(1, int(height))}

        async def read_from_guacd():
            """Forward Guacamole instructions from guacd to WebSocket."""
            count = 0
            suppressed_mouse_count = 0
            try:
                while session.is_connected:
                    batch, raw_batch = await session.read_instruction_batch_raw()
                    # Downstream file traffic is dominated by blob/ack instructions.
                    # Only mouse/size require filtering or clamping; everything else
                    # can be forwarded byte-for-byte without parse/rebuild overhead.
                    if not _needs_rdp_rewrite(raw_batch):
                        payload = raw_batch
                        next_width = rdp_dimensions["width"]
                        next_height = rdp_dimensions["height"]
                        clamped = 0
                    else:
                        safe_batch, next_width, next_height, clamped = (
                            _sanitize_rdp_instructions(
                                batch,
                                rdp_dimensions["width"],
                                rdp_dimensions["height"],
                                drop_server_mouse=True,
                            )
                        )
                        payload = b"".join(safe_batch)
                    rdp_dimensions["width"] = next_width
                    rdp_dimensions["height"] = next_height
                    suppressed_mouse_count += sum(
                        1 for opcode, _ in batch if opcode == "mouse"
                    )
                    if suppressed_mouse_count and suppressed_mouse_count <= 5:
                        logger.info(
                            "RDP server cursor-position updates suppressed; "
                            "browser native cursor is authoritative"
                        )
                    if clamped:
                        logger.warning(
                            "Clamped %d out-of-range RDP server mouse instruction(s) to %dx%d",
                            clamped,
                            next_width,
                            next_height,
                        )
                    # guacamole-common-js accepts multiple complete protocol
                    # instructions in one WebSocket message. This dramatically
                    # reduces per-frame overhead during screen updates.
                    if payload:
                        await websocket.send_text(payload.decode("utf-8"))
                    count += len(batch)
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "RDP -> browser: %d instructions (total=%d)",
                            len(batch),
                            count,
                        )
            except (ConnectionError, Exception) as e:
                logger.info("guacd read loop ended after %d instructions: %s", count, e)

        async def write_to_guacd():
            """Forward raw Guacamole instructions from WebSocket to guacd."""
            count = 0
            mouse_count = 0
            try:
                while session.is_connected:
                    raw = await websocket.receive_text()
                    if not raw:
                        break
                    raw_bytes = raw.encode("utf-8")
                    count += 1
                    if not _needs_rdp_rewrite(raw_bytes):
                        # 热路径（上传时的 blob 洪峰）：不解析、不重编码，直接转发。
                        await session.write_raw(raw_bytes)
                        continue

                    safe_raw, current_width, current_height, clamped = (
                        _sanitize_rdp_input(
                            raw_bytes,
                            rdp_dimensions["width"],
                            rdp_dimensions["height"],
                        )
                    )
                    rdp_dimensions["width"] = current_width
                    rdp_dimensions["height"] = current_height

                    mice = raw_bytes.count(_MOUSE_OPCODE)
                    if mice:
                        mouse_count += mice
                        if mouse_count <= 5 or mouse_count % 100 == 0:
                            # 只在真正要打日志时才解析坐标，避免每条鼠标指令都
                            # 多付一次完整解析。
                            coords = _first_mouse_coords(safe_raw)
                            logger.warning(
                                "RDP browser mouse #%d: x=%s y=%s size=%dx%d",
                                mouse_count,
                                coords[0],
                                coords[1],
                                current_width,
                                current_height,
                            )
                    if clamped:
                        logger.warning(
                            "Clamped %d out-of-range RDP mouse instruction(s) to %dx%d",
                            clamped,
                            current_width,
                            current_height,
                        )
                    if count <= 10 or count % 100 == 0:
                        logger.info("browser -> RDP: %d instruction(s)", count)
                    await session.write_raw(safe_raw)
            except (WebSocketDisconnect, Exception) as e:
                logger.info(
                    "WebSocket write loop ended after %d instructions: %s", count, e
                )

        read_task = asyncio.create_task(read_from_guacd())
        write_task = asyncio.create_task(write_to_guacd())

        done, pending = await asyncio.wait(
            [read_task, write_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in done:
            if task.exception():
                logger.error("Bridge task failed: %s", task.exception())

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        logger.info("RDP bridge ended")

    except ConnectionError as exc:
        logger.error("RDP connection error: %s", exc)
        try:
            await websocket.send_json({"type": "error", "data": "RDP 连接失败"})
        except Exception:
            pass
    except Exception as exc:
        logger.error("Unexpected RDP error: %s", exc)
        import traceback

        logger.error("Traceback: %s", traceback.format_exc())
        try:
            # Don't leak internal exception details to the client; full detail
            # is already logged server-side above.
            await websocket.send_json(
                {"type": "error", "data": "RDP 连接异常，请联系管理员"}
            )
        except Exception:
            pass
    finally:
        await session.close()
        if relay_server:
            relay_server.close()
            await relay_server.wait_closed()
            logger.info("RDP relay stopped")
        # Remove this session's GuacamoleFS drive dir (best-effort; see helper).
        _cleanup_drive_dir(rdp_session_id)
        try:
            await websocket.close()
        except Exception:
            pass


# ------------------------------------------------------------------
# Endpoint
# ------------------------------------------------------------------


@router.websocket("/ws/terminal/{device_id}")
async def terminal_ws(
    websocket: WebSocket,
    device_id: int,
    ticket: str = Query(...),
    session_id: str | None = Query(default=None),
):
    """
    Open an interactive terminal session to *device_id*.

    Query parameters
    ----------------
    ticket        : single-use opaque terminal ticket (required)
    session_id    : optional client-side session identifier for RDP drive cleanup

    Credentials and connection type are resolved from the ticket.  After the
    WebSocket is accepted, SSH clients send only ``input``, ``paste`` and
    ``resize`` messages; RDP clients speak the Guacamole protocol directly.
    """
    # --- 两阶段:先 peek(不消费)校验,全部通过后才消费票据 + 凭据 ----------
    # 一次性票据若在握手期被网络抖动烧掉,前端重连会拿到「票据无效」,
    # 已授权用户也得回 HTTP 重新开票。改为:peek 出 payload → 校验身份/权限
    # → 校验通过才原子 GETDEL 消费。校验失败不消费,票据留给合法重试。
    ticket_payload = peek_ticket(ticket)
    if not ticket_payload or ticket_payload.get("device_id") != device_id:
        await websocket.accept()
        await websocket.send_json(
            {"type": "error", "data": "终端票据无效或已过期，请重新发起连接"}
        )
        await websocket.close(code=4001)
        return

    conn_type = ticket_payload.get("conn_type", "ssh")
    width = int(ticket_payload.get("width", 1024))
    height = int(ticket_payload.get("height", 768))
    user_id_val = int(ticket_payload.get("user_id", 0))
    system_username_val = ticket_payload.get("system_username", "")
    access_jti = ticket_payload.get("access_jti", "")
    session_version = int(ticket_payload.get("session_version", 0))

    # 容器终端:票据里带了 container 则连接后自动 docker exec 进入
    container_val = ticket_payload.get("container") or None
    initial_command = None
    if container_val and conn_type == "ssh":
        try:
            container_val = validate_container_name(container_val)
            # bash 优先(历史/补全体验好);镜像里没有再退 sh/ash——`||` 链在
            # 容器内运行时逐个探测,无需预判镜像。distroless 镜像一个都没有时,
            # SSH shell 仍可用。
            initial_command = (
                f"docker exec -it {container_val} bash"
                f" || docker exec -it {container_val} sh"
                f" || docker exec -it {container_val} ash"
            )
        except ValueError:
            container_val = None
            initial_command = None

    subprotocol = "guacamole" if conn_type == "rdp" else None

    # --- Re-validate access (blacklist / active / permission / scope) ----
    db: Session = SessionLocal()
    try:
        error_data: str | None = None
        error_code = 4003
        if access_jti and token_blacklist.is_blacklisted(access_jti, db):
            error_data = "登录状态已变更，请重新发起连接"
            error_code = 4001
        else:
            user = db.query(User).filter(User.id == user_id_val).first()
            if user is None or not user.is_active:
                error_data = "账户已被禁用"
            elif int(getattr(user, "session_version", 0)) != session_version:
                error_data = "登录状态已变更，请重新发起连接"
                error_code = 4001
            elif not user_has_permission(user, "device:remote", db):
                error_data = "权限不足：无终端访问权限"
            elif not user_can_access_device(user, device_id, db):
                error_data = "权限不足：无权访问该设备"

        if error_data is not None:
            await websocket.accept(subprotocol=subprotocol)
            if conn_type == "rdp":
                await websocket.send_text(
                    _build_instruction("error", error_data, str(error_code)).decode()
                )
                await websocket.send_text(_build_instruction("disconnect").decode())
            else:
                await websocket.send_json({"type": "error", "data": error_data})
            await websocket.close(code=error_code)
            return
    finally:
        db.close()

    # --- 校验全部通过:此时才消费一次性票据与凭据 --------------------------
    ticket_payload = consume_ticket(ticket)
    if not ticket_payload:
        # 并发重连:另一连接抢先消费了同一张票 → 让本次关闭,客户端走重试。
        await websocket.accept()
        await websocket.send_json(
            {"type": "error", "data": "终端票据已被使用，请重新发起连接"}
        )
        await websocket.close(code=4001)
        return
    credentials = consume_credential(ticket_payload.get("credential_ref"))
    if credentials is None:
        await websocket.accept()
        await websocket.send_json(
            {"type": "error", "data": "终端凭据已过期，请重新发起连接"}
        )
        await websocket.close(code=4001)
        return
    username = credentials.get("username", "")
    password = credentials.get("password", "")
    private_key = credentials.get("private_key", "")
    if conn_type == "ssh" and not username:
        username = "root"

    # --- Accept the WebSocket -------------------------------------------
    await websocket.accept(subprotocol=subprotocol)

    # Credentials were resolved server-side when the ticket was issued, so the
    # WebSocket URL never carried a JWT, username, or password.

    # --- RDP via Guacamole ----------------------------------------------
    if conn_type == "rdp":
        await _handle_rdp(
            websocket,
            device_id,
            username,
            password,
            user_id=user_id_val,
            system_username=system_username_val,
            width=width,
            height=height,
            session_id=session_id,
        )
        return

    # --- Look up device -------------------------------------------------
    device = _get_device(device_id)
    if device is None:
        await websocket.send_json(
            {"type": "error", "data": f"Device {device_id} not found"}
        )
        await websocket.close(code=4004)
        return

    if not device.ip_address:
        await websocket.send_json(
            {
                "type": "error",
                "data": f"Device {device_id} has no IP address configured",
            }
        )
        await websocket.close(code=4000)
        return

    # --- Establish SSH (TOFU host-key pinning) -------------------------
    session = None
    _toku_db = SessionLocal()
    try:
        session = await create_ssh_connection(
            device=device,
            username=username,
            password=password,
            private_key=private_key or None,
            cols=80,
            rows=24,
            db=_toku_db,
            initial_command=initial_command,
        )

        await websocket.send_json(
            {
                "type": "connected",
                "message": (
                    f"已进入容器 {container_val}"
                    if container_val
                    else f"SSH connected to {device.ip_address}:{device.ssh_port}"
                ),
            }
        )

        async def read_ssh_output():
            try:
                while session.is_connected:
                    output = await session.recv_output()
                    if output is None:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "data": "SSH connection closed by remote host",
                            }
                        )
                        break
                    if output:
                        await websocket.send_json({"type": "output", "data": output})
                    await asyncio.sleep(0.01)
            except WebSocketDisconnect:
                raise
            except Exception:
                logger.warning("SSH read loop ended unexpectedly", exc_info=True)

        async def write_ssh_input():
            try:
                while session.is_connected:
                    raw = await websocket.receive_text()
                    if raw is None:
                        break
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type")

                    if msg_type == "input":
                        data = msg.get("data", "")
                        if data:
                            await session.send_input(data)

                    elif msg_type == "paste":
                        data = msg.get("data", "")
                        if data:
                            await session.send_input(data)

                    elif msg_type == "resize":
                        try:
                            cols = int(msg.get("cols", 80))
                            rows = int(msg.get("rows", 24))
                            if cols < 1 or rows < 1 or cols > 500 or rows > 500:
                                continue
                        except (ValueError, TypeError):
                            continue
                        await session.resize_pty(cols, rows)
            except WebSocketDisconnect:
                raise
            except Exception:
                logger.warning("SSH write loop ended unexpectedly", exc_info=True)

        read_task = asyncio.create_task(read_ssh_output())
        write_task = asyncio.create_task(write_ssh_input())

        done, pending = await asyncio.wait(
            [read_task, write_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            try:
                task.result()
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
            except Exception:
                logger.warning("SSH terminal task ended unexpectedly", exc_info=True)
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass

    except ConnectionError as exc:
        try:
            await websocket.send_json({"type": "error", "data": str(exc)})
        except Exception:
            pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("SSH terminal unexpected error: %s", exc, exc_info=True)
        try:
            await websocket.send_json(
                {"type": "error", "data": "连接发生意外错误，请重试或联系管理员"}
            )
        except Exception:
            pass
    finally:
        _toku_db.close()
        if session is not None:
            await session.close()
        try:
            await websocket.close()
        except Exception:
            pass
