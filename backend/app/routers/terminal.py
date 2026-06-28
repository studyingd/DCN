"""
WebSocket terminal router.

Provides a single endpoint  ``/ws/terminal/{device_id}``  that brokers an
interactive SSH or RDP session between the browser and the target device.

Authentication is performed by extracting a JWT *token* from the query string.
Credentials can be supplied directly via *username*/*password* or by referencing
a stored *credential_id*.
"""

import asyncio
import json
import logging
import uuid

import jwt
from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import GUACD_HOST, GUACD_PORT, JWT_ALGORITHM, JWT_SECRET
from app.database import SessionLocal, get_db
from app.models.credential import Credential
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
    user_has_permission,
)
from app.services.recorder import SessionRecorder
from app.services.settings import is_audit_enabled, is_recording_enabled
from app.services.storage import upload_rdp_recording
from app.services.terminal import create_ssh_connection
from app.services.ws_ticket import consume_ticket, issue_ticket

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminal"])


def _ws_access_payload(websocket: WebSocket) -> dict | None:
    """Decode the httpOnly access cookie on a same-origin WebSocket.

    Validates signature, expiry, and token type. Returns the payload (caller
    still checks the blacklist + permissions) or None.
    """
    token = websocket.cookies.get("dcn_access")
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception:
        return None
    if payload.get("type") != "access":
        return None
    return payload


class TerminalTicketRequest(BaseModel):
    device_id: int
    conn_type: str = "ssh"
    credential_id: int | None = None
    username: str | None = None
    password: str | None = None
    width: int = 1024
    height: int = 768


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
    from fastapi import HTTPException

    device = db.query(Device).filter(Device.id == body.device_id).first()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    if not user_can_access_device(current_user, body.device_id, db):
        raise HTTPException(status_code=403, detail="无权访问该设备")

    username, password = _resolve_credentials(
        body.credential_id, body.username or "", body.password or ""
    )
    if body.conn_type == "ssh" and not username:
        username = "root"

    payload = {
        "user_id": current_user.id,
        "system_username": current_user.username,
        "device_id": body.device_id,
        "conn_type": body.conn_type,
        "cred_username": username,
        "cred_password": password,
        "width": body.width,
        "height": body.height,
        "access_jti": _access_jti(request),
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
    credential_id: int | None,
    username: str,
    password: str,
) -> tuple[str, str]:
    """Resolve credentials: use stored credential if credential_id is given, else use direct params."""
    if not credential_id:
        return username, password

    db: Session = SessionLocal()
    try:
        cred = db.query(Credential).filter(Credential.id == credential_id).first()
        if not cred:
            return username, password
        resolved_user = cred.username or username
        resolved_pass = decrypt(cred.password_enc) if cred.password_enc else password
        return resolved_user, resolved_pass
    finally:
        db.close()


# ------------------------------------------------------------------
# RDP handler via Guacamole
# ------------------------------------------------------------------


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
) -> None:
    """Bridge a WebSocket connection to guacd for RDP."""

    logger.info("RDP: requested resolution %dx%d", width, height)

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

    rdp_port = device.rdp_port or 3389
    logger.info(
        "RDP: device=%s ip=%s port=%d user=%s",
        device.name,
        device.ip_address,
        rdp_port,
        username,
    )

    # --- RDP Recording setup ---
    _rec_db = SessionLocal()
    try:
        recording_active = is_recording_enabled(_rec_db) and is_audit_enabled(_rec_db)
    except Exception:
        recording_active = False
    finally:
        _rec_db.close()
    rdp_session_id = session_id or str(uuid.uuid4())
    rdp_start_time = None
    rdp_events: list[str] = []
    rdp_db_recording_id: int | None = None

    if recording_active:
        rdp_start_time = asyncio.get_running_loop().time()
        from datetime import datetime
        from datetime import timezone as tz

        from app.models.session_recording import SessionRecording as SR

        db = SessionLocal()
        try:
            rec = SR(
                session_id=rdp_session_id,
                device_id=device.id,
                user_id=user_id,
                device_name=device.name,
                device_ip=device.ip_address or "",
                username=username,
                conn_type="rdp",
            )
            db.add(rec)
            db.commit()
            db.refresh(rec)
            rdp_db_recording_id = rec.id
        except Exception:
            logger.exception("Failed to create RDP recording record")
            recording_active = False
        finally:
            db.close()

    session = GuacamoleSession(host=GUACD_HOST, port=GUACD_PORT)

    # Start TCP relay so guacd (Docker) can reach the RDP target through the host
    relay_server: asyncio.AbstractServer | None = None
    relay_hostname = device.ip_address
    relay_port = rdp_port
    try:
        relay_server, local_relay_port = await start_rdp_relay(
            device.ip_address, rdp_port
        )
        import platform

        if GUACD_HOST in ("localhost", "127.0.0.1") and platform.system() != "Linux":
            relay_hostname = "host.docker.internal"
        relay_port = local_relay_port
        logger.info(
            "RDP relay started: guacd -> %s:%d -> %s:%d",
            relay_hostname,
            relay_port,
            device.ip_address,
            rdp_port,
        )
    except Exception as exc:
        logger.warning("RDP relay failed to start, connecting directly: %s", exc)

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
        )
        logger.info("RDP: handshake complete, starting bridge loops")

        async def read_from_guacd():
            """Forward Guacamole instructions from guacd to WebSocket."""
            nonlocal recording_active
            count = 0
            try:
                while session.is_connected:
                    opcode, args = await session.read_instruction()
                    data = _build_instruction(opcode, *args)
                    await websocket.send_text(data.decode("utf-8"))
                    count += 1

                    # Record output instruction
                    if (
                        recording_active
                        and rdp_start_time is not None
                        and opcode != "nop"
                    ):
                        elapsed_ms = int(
                            (asyncio.get_running_loop().time() - rdp_start_time) * 1000
                        )
                        raw_text = data.decode("utf-8").replace("\n", "")
                        rdp_events.append(f"{elapsed_ms}|{raw_text}")

                    if opcode in (
                        "size",
                        "img",
                        "sync",
                        "png",
                        "jpeg",
                        "end",
                        "error",
                        "disconnect",
                    ):
                        arg_summary = []
                        for a in args[:6]:
                            arg_summary.append(a[:50] if len(a) > 50 else a)
                        logger.info(
                            "RDP -> browser: #%d opcode=%s args=%s",
                            count,
                            opcode,
                            arg_summary,
                        )
                    elif count <= 20 or count % 200 == 0:
                        logger.info(
                            "RDP -> browser: #%d opcode=%s args_count=%d",
                            count,
                            opcode,
                            len(args),
                        )
            except (ConnectionError, Exception) as e:
                logger.info("guacd read loop ended after %d instructions: %s", count, e)

        async def write_to_guacd():
            """Forward raw Guacamole instructions from WebSocket to guacd."""
            count = 0
            try:
                while session.is_connected:
                    raw = await websocket.receive_text()
                    if not raw:
                        break
                    try:
                        parsed, _ = _parse_instruction(raw.encode("utf-8"))
                        if parsed:
                            op = parsed[0]
                            count += 1
                            if count <= 10 or count % 100 == 0:
                                logger.info(
                                    "browser -> RDP: opcode=%s (total=%d)", op, count
                                )
                    except Exception:
                        pass
                    await session.write_raw(raw.encode("utf-8"))
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
        # --- Finalize RDP recording ---
        if recording_active and rdp_db_recording_id and rdp_events:
            try:
                from datetime import datetime
                from datetime import timezone as tz

                header = json.dumps(
                    {
                        "v": 1,
                        "type": "rdp",
                        "width": width,
                        "height": height,
                        "ts": int(asyncio.get_running_loop().time()),
                    }
                )
                lines = [header] + rdp_events
                file_data = "\n".join(lines).encode("utf-8")
                duration = rdp_events[-1].split("|")[0] if rdp_events else "0"
                duration_sec = int(duration) // 1000

                file_path = upload_rdp_recording(rdp_session_id, file_data)

                db = SessionLocal()
                try:
                    from app.models.session_recording import SessionRecording as SR

                    rec = db.query(SR).filter(SR.id == rdp_db_recording_id).first()
                    if rec:
                        rec.file_path = file_path
                        rec.file_size = len(file_data)
                        rec.ended_at = datetime.now(tz.utc)
                        rec.duration_seconds = duration_sec
                        db.commit()
                finally:
                    db.close()
                logger.info(
                    "RDP recording saved: %d events, %d bytes, %ds",
                    len(rdp_events),
                    len(file_data),
                    duration_sec,
                )
            except Exception:
                logger.exception("Failed to save RDP recording")

        await session.close()
        if relay_server:
            relay_server.close()
            await relay_server.wait_closed()
            logger.info("RDP relay stopped")
        try:
            await websocket.close()
        except Exception:
            pass


# ------------------------------------------------------------------
# RDP Recording Replay
# ------------------------------------------------------------------


@router.websocket("/ws/rdp-recording/{recording_id}")
async def rdp_recording_replay_ws(
    websocket: WebSocket,
    recording_id: int,
):
    """Replay an RDP session recording by streaming Guacamole instructions.

    Authenticated via the httpOnly access cookie (same-origin); requires
    ``audit:manage`` so only auditors/admins can replay sessions.
    """
    payload = _ws_access_payload(websocket)
    if payload is None:
        await websocket.close(code=4001)
        return

    from app.services.storage import download_rdp_recording

    db = SessionLocal()
    try:
        from app.models.session_recording import SessionRecording

        jti = payload.get("jti", "")
        if jti and token_blacklist.is_blacklisted(jti, db):
            await websocket.close(code=4001)
            return
        try:
            user_id = int(payload.get("sub", 0))
        except (TypeError, ValueError):
            await websocket.close(code=4001)
            return
        user = db.query(User).filter(User.id == user_id).first()
        if (
            user is None
            or not user.is_active
            or not user_has_permission(user, "audit:manage", db)
        ):
            await websocket.close(code=4003)
            return

        rec = (
            db.query(SessionRecording)
            .filter(SessionRecording.id == recording_id)
            .first()
        )
        if not rec or rec.conn_type != "rdp":
            await websocket.close(code=4004)
            return
        session_id = rec.session_id
    finally:
        db.close()

    data = download_rdp_recording(session_id)
    if not data:
        await websocket.close(code=4004)
        return

    lines = data.decode("utf-8").split("\n")
    if len(lines) < 2:
        await websocket.close(code=4004)
        return

    header = json.loads(lines[0])
    width = header.get("width", 1024)
    height = header.get("height", 768)

    events: list[tuple[int, str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split("|", 1)
        if len(parts) == 2:
            events.append((int(parts[0]), parts[1]))

    if not events:
        await websocket.close(code=4004)
        return

    total_duration_ms = events[-1][0]

    await websocket.accept(subprotocol="guacamole")

    # Send initial handshake response to initialize the Guacamole client
    from app.services.guacamole import _build_instruction

    ready = _build_instruction(
        "ready", "VERSION_1_5_0", f"Recording-{recording_id}", str(width), str(height)
    )
    await websocket.send_text(ready.decode("utf-8"))
    # Send initial size for the default layer (0)
    size_instr = _build_instruction("size", "0", str(width), str(height))
    await websocket.send_text(size_instr.decode("utf-8"))
    # Flush the initial size so the display canvas is created at the right dimensions.
    # Without sync, all queued drawing operations stay pending forever.
    initial_sync = _build_instruction("sync", "0")
    await websocket.send_text(initial_sync.decode("utf-8"))

    # Playback state — wait for client 'play' command
    playing = False
    speed = 1.0
    current_idx = 0
    start_wall = 0.0
    start_offset_ms = 0

    async def send_instructions():
        nonlocal current_idx, start_wall, start_offset_ms, playing, speed
        try:
            while True:
                if not playing:
                    await asyncio.sleep(0.05)
                    continue

                if current_idx >= len(events):
                    playing = False
                    await asyncio.sleep(0.05)
                    continue

                elapsed_ms = (
                    asyncio.get_running_loop().time() - start_wall
                ) * 1000 * speed + start_offset_ms

                # Batch instructions that are due into a single WebSocket message
                batch: list[str] = []
                while (
                    current_idx < len(events) and events[current_idx][0] <= elapsed_ms
                ):
                    batch.append(events[current_idx][1])
                    current_idx += 1

                if batch:
                    await websocket.send_text("".join(batch))
                    # Inject sync to flush the Guacamole client display.
                    # Old recordings may lack sync instructions; new recordings
                    # include them, but an extra sync is harmless.
                    sync_instr = _build_instruction("sync", str(int(elapsed_ms)))
                    await websocket.send_text(sync_instr.decode("utf-8"))

                if current_idx >= len(events):
                    playing = False
                    continue

                next_ms = events[current_idx][0]
                wait = (next_ms - elapsed_ms) / speed / 1000
                await asyncio.sleep(max(0.005, min(wait, 0.1)))

        except Exception:
            pass

    async def recv_commands():
        nonlocal playing, speed, current_idx, start_wall, start_offset_ms
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                if msg.get("action") == "pause":
                    start_offset_ms = (
                        start_offset_ms
                        + (asyncio.get_running_loop().time() - start_wall)
                        * 1000
                        * speed
                    )
                    playing = False
                elif msg.get("action") == "play":
                    start_wall = asyncio.get_running_loop().time()
                    playing = True
                elif msg.get("action") == "seek":
                    target_ms = int(msg.get("time", 0))
                    current_idx = 0
                    for i, (ts, _) in enumerate(events):
                        if ts >= target_ms:
                            current_idx = i
                            break
                    else:
                        current_idx = len(events)
                    start_offset_ms = target_ms
                    start_wall = asyncio.get_running_loop().time()
                    # Send all instructions up to seek point
                    for i in range(current_idx):
                        await websocket.send_text(events[i][1])
                    # Flush display after seek
                    seek_sync = _build_instruction("sync", str(target_ms))
                    await websocket.send_text(seek_sync.decode("utf-8"))
                elif msg.get("action") == "speed":
                    start_offset_ms = (
                        start_offset_ms
                        + (asyncio.get_running_loop().time() - start_wall)
                        * 1000
                        * speed
                    )
                    start_wall = asyncio.get_running_loop().time()
                    speed = float(msg.get("value", 1))
                elif msg.get("action") == "info":
                    await websocket.send_text(
                        json.dumps(
                            {
                                "action": "info",
                                "duration": total_duration_ms,
                                "width": width,
                                "height": height,
                            }
                        )
                    )
        except Exception:
            pass

    send_task = asyncio.create_task(send_instructions())
    recv_task = asyncio.create_task(recv_commands())

    done, pending = await asyncio.wait(
        [send_task, recv_task], return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

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
    token         : JWT access token (required)
    conn_type     : ``ssh`` or ``rdp``
    width/height  : RDP display dimensions

    After WebSocket is accepted, the client must send a JSON message:
    {"type": "auth", "username": "...", "password": "...", "credential_id": N}
    """
    # --- Consume the single-use terminal ticket -------------------------
    ticket_payload = consume_ticket(ticket)
    if not ticket_payload or ticket_payload.get("device_id") != device_id:
        await websocket.accept()
        await websocket.send_json(
            {"type": "error", "data": "终端票据无效或已过期，请重新发起连接"}
        )
        await websocket.close(code=4001)
        return

    conn_type = ticket_payload.get("conn_type", "ssh")
    username = ticket_payload.get("cred_username", "")
    password = ticket_payload.get("cred_password", "")
    width = int(ticket_payload.get("width", 1024))
    height = int(ticket_payload.get("height", 768))
    user_id_val = int(ticket_payload.get("user_id", 0))
    system_username_val = ticket_payload.get("system_username", "")
    access_jti = ticket_payload.get("access_jti", "")
    if conn_type == "ssh" and not username:
        username = "root"

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

    # --- Initialize session recorder -----------------------------------
    session_id = str(uuid.uuid4())
    recorder = SessionRecorder(
        session_id=session_id,
        device_id=device.id,
        user_id=user_id_val,
        device_name=device.name,
        device_ip=device.ip_address or "",
        ssh_username=username,
        conn_type="ssh",
        system_username=system_username_val,
    )
    recorder.initialize()

    # --- Establish SSH (TOFU host-key pinning) -------------------------
    session = None
    _toku_db = SessionLocal()
    try:
        session = await create_ssh_connection(
            device=device,
            username=username,
            password=password,
            cols=80,
            rows=24,
            db=_toku_db,
        )

        await websocket.send_json(
            {
                "type": "connected",
                "message": f"SSH connected to {device.ip_address}:{device.ssh_port}",
            }
        )

        # Wrap read/write with recorder
        async def read_with_recording():
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
                        recorder.record_output(output)
                        await websocket.send_json({"type": "output", "data": output})
                    await asyncio.sleep(0.01)
            except WebSocketDisconnect:
                raise
            except Exception:
                logger.warning("SSH read loop ended unexpectedly", exc_info=True)

        async def write_with_recording():
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
                            should_forward, warning = recorder.process_input(data)
                            if not should_forward:
                                # Command blocked — immediately clear the
                                # shell's input buffer so pending characters
                                # are discarded.  Ctrl+U clears the line,
                                # Ctrl+C sends SIGINT for a clean prompt.
                                await session.send_input("\x15")
                                await session.send_input("\x03")
                                if warning:
                                    await websocket.send_json(
                                        {"type": "output", "data": warning}
                                    )
                                    recorder.record_output(warning)
                            else:
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

        read_task = asyncio.create_task(read_with_recording())
        write_task = asyncio.create_task(write_with_recording())

        done, pending = await asyncio.wait(
            [read_task, write_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
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
        recorder.finish()
        _toku_db.close()
        if session is not None:
            await session.close()
        try:
            await websocket.close()
        except Exception:
            pass
