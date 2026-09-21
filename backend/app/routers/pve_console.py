"""PVE 虚拟机 Web 控制台 —— noVNC 的 WebSocket 代理。

浏览器无法直连 PVE(内网/自签证书),所以由后端做 WebSocket 中转:
  浏览器 noVNC <--(本站 WS)--> 后端代理 <--(websockets 客户端)--> PVE vncwebsocket

票据设计:
  * console-ticket 端点(device:remote)先向 PVE 申请 vncproxy(拿到 port + vncticket),
    再把这些参数装进本平台一次性 ws_ticket 返回给前端;
  * 前端用 ws_ticket 连 /ws/pve-console,vncticket 作为 noVNC 的 VNC 密码。
"""

import asyncio
import logging
import ssl
from typing import Any

import websockets
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.middleware.rate_limiter import limiter
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.models.user import User
from app.routers.terminal import _handle_rdp
from app.services.containers_collector import pve_target_id
from app.services.crypto import decrypt
from app.services.permissions import (
    require_pve_guest_permission,
    user_can_access_pve,
)
from app.services.pve import PveError, build_client, guest_agent_summary
from app.services.pve_guest_status import record_guest_ip
from app.services.terminal import create_ssh_connection
from app.services.ws_ticket import consume_ticket, issue_ticket
from app.validators import validate_container_name

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pve-console"])


@router.post("/api/pve/connections/{conn_id}/guests/{gtype}/{vmid}/console-ticket")
# 每次申请票据都会同步调 PVE 的 /cluster/resources 与 vncproxy，PVE 侧开销不小，
# 且票据本身 30s 一次性有效，正常点开控制台远用不到这个频次。
@limiter.limit("30/minute")
def issue_console_ticket(
    conn_id: int,
    gtype: str,
    vmid: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_pve_guest_permission(manage=True)),
    force_pve: bool = Query(False),
    width: int = Query(1280, ge=320, le=7680),
    height: int = Query(720, ge=240, le=4320),
):
    """申请 PVE 控制台票据(一次性,30s 内有效)。"""
    if gtype != "qemu":
        # LXC 支持已彻底移除(2026-09-17 用户拍板),全链路只认 qemu。
        raise HTTPException(
            status_code=400, detail="平台仅支持 qemu 虚拟机(LXC 已移除)"
        )
    conn = db.query(PveConnection).filter(PveConnection.id == conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="PVE 连接不存在")
    if not conn.enabled:
        raise HTTPException(status_code=400, detail="该 PVE 连接已停用")

    client = build_client(conn)
    try:
        guests = client.list_guest_resources()
        guest = next(
            (item for item in guests if int(item.get("vmid", -1)) == vmid), None
        )
        if not guest or not guest.get("node"):
            raise PveError(f"PVE 中找不到虚机/容器 {vmid}")
        node = str(guest["node"])
        binding = (
            db.query(PveGuestBinding)
            .filter_by(connection_id=conn_id, guest_type=gtype, vmid=vmid)
            .first()
        )
        qga = {}
        try:
            qga = guest_agent_summary(client, node, gtype, vmid)
        except Exception:
            qga = {}
        # Prefer the guest-level remote channel when it is completely configured.
        guest_os = str(
            (qga.get("qga_os_system") if qga.get("qga_available") else None)
            or (binding.os_system if binding else "")
            or ""
        ).lower()
        # 排除法:Windows 需 QGA/binding 显式确证,否则一律按 Linux(SSH)出控制台。
        if guest_os != "windows":
            guest_os = "linux"
        guest_ip = (
            qga.get("qga_ip_address") if qga.get("qga_available") else None
        ) or (binding.ip_address if binding else None)
        # 运行期 QGA 报出的实时地址记入「最近已知 IP」(停机后告警地址列兜底)
        if qga.get("qga_available") and qga.get("qga_ip_address"):
            record_guest_ip(conn_id, vmid, str(qga["qga_ip_address"]))
        has_secret = bool(
            binding
            and (
                binding.password_enc
                if guest_os == "windows"
                else (binding.password_enc or binding.ssh_key_enc)
            )
        )
        remote_ok = bool(
            binding
            and binding.enabled
            and guest_ip
            and binding.username
            and has_secret
            and guest_os in ("linux", "windows")
        )
        if remote_ok and not force_pve:
            payload = {
                "kind": "pve-remote",
                "mode": "ssh" if guest_os == "linux" else "rdp",
                "binding_id": binding.id,
                "conn_id": conn_id,
                "gtype": gtype,
                "vmid": vmid,
                "target_ip": guest_ip,
                "width": width,
                "height": height,
                "user_id": current_user.id,
                "session_version": int(getattr(current_user, "session_version", 0)),
            }
            # target_id 是 containers/automation/files 共用的合成负数 ID。
            # 带上它，前端的文件管理器才能对虚拟机走同一套 /files/* 接口。
            return {
                "mode": payload["mode"],
                "ticket": issue_ticket(payload),
                "target_id": pve_target_id(conn_id, vmid),
            }

        data = client.vncproxy(node, gtype, vmid)
    except PveError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    port = data.get("port")
    vncticket = data.get("ticket")
    if not port or not vncticket:
        raise HTTPException(status_code=502, detail="PVE vncproxy 未返回票据")

    ws_ticket = issue_ticket(
        {
            "conn_id": conn_id,
            "gtype": gtype,
            "vmid": vmid,
            "node": node,
            "port": port,
            "vncticket": vncticket,
            "user_id": current_user.id,
            "session_version": int(getattr(current_user, "session_version", 0)),
        }
    )
    return {"mode": "pve", "ticket": ws_ticket, "vncticket": vncticket, "node": node}


def _load_conn(conn_id: int) -> PveConnection | None:
    db = SessionLocal()
    try:
        return db.query(PveConnection).filter(PveConnection.id == conn_id).first()
    finally:
        db.close()


@router.websocket("/ws/pve-console")
async def pve_console_ws(websocket: WebSocket, ticket: str = Query(...)):
    payload = consume_ticket(ticket)
    if not payload:
        await websocket.accept()
        await websocket.close(code=4001)
        return

    conn_id = payload.get("conn_id")
    gtype = payload.get("gtype")
    vmid = payload.get("vmid")
    node = payload.get("node")
    port = payload.get("port")
    vncticket = payload.get("vncticket")

    conn = _load_conn(conn_id)
    if not conn or not conn.enabled:
        await websocket.accept()
        await websocket.close(code=4004)
        return

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == int(payload.get("user_id", 0))).first()
        authorized = bool(
            user
            and user.is_active
            and int(getattr(user, "session_version", 0))
            == int(payload.get("session_version", 0))
            and user_can_access_pve(
                user,
                db,
                manage=True,
                guest=(int(conn_id), str(gtype), int(vmid))
                if conn_id and gtype and vmid
                else None,
            )
        )
    finally:
        db.close()
    if not authorized:
        await websocket.accept()
        await websocket.close(code=4003)
        return

    client = build_client(conn)
    try:
        ws_url = client.vncwebsocket_url(node, gtype, vmid, port, vncticket)
    except Exception:
        await websocket.accept()
        await websocket.close(code=1011)
        return

    # PVE 自签证书:verify_ssl=0 时不校验
    ssl_ctx = ssl.create_default_context()
    if not conn.verify_ssl:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    await websocket.accept()

    try:
        async with websockets.connect(
            ws_url,
            ssl=ssl_ctx,
            additional_headers={"Authorization": client.authorization_header},
            subprotocols=["binary"],
            max_size=None,
            ping_interval=None,
        ) as pve_ws:
            await _proxy_bidirectional(websocket, pve_ws)
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception as exc:
        logger.warning("PVE console proxy error: %s", exc)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


def _load_binding(binding_id: int) -> PveGuestBinding | None:
    db = SessionLocal()
    try:
        return (
            db.query(PveGuestBinding).filter(PveGuestBinding.id == binding_id).first()
        )
    finally:
        db.close()


async def _authorize_remote_ticket(websocket: WebSocket, payload: dict) -> User | None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == int(payload.get("user_id", 0))).first()
        # ticket 里带了 guest 身份，因此可以直接叠加虚拟机级 ACL。
        guest = None
        if payload.get("conn_id") and payload.get("vmid"):
            guest = (
                int(payload["conn_id"]),
                str(payload.get("gtype") or "qemu"),
                int(payload["vmid"]),
            )
        if (
            not user
            or not user.is_active
            or not user_can_access_pve(user, db, manage=True, guest=guest)
        ):
            await websocket.accept()
            await websocket.close(code=4003)
            return None
        if int(getattr(user, "session_version", 0)) != int(
            payload.get("session_version", 0)
        ):
            await websocket.accept()
            await websocket.close(code=4001)
            return None
        return user
    finally:
        db.close()


@router.websocket("/ws/pve-terminal")
async def pve_terminal_ws(websocket: WebSocket, ticket: str = Query(...)):
    payload = consume_ticket(ticket)
    if (
        not payload
        or payload.get("kind") != "pve-remote"
        or payload.get("mode") != "ssh"
    ):
        await websocket.accept()
        await websocket.close(code=4001)
        return
    if await _authorize_remote_ticket(websocket, payload) is None:
        return
    binding = _load_binding(int(payload.get("binding_id", 0)))
    if (
        not binding
        or not binding.enabled
        or not binding.ip_address
        or not binding.username
    ):
        await websocket.accept()
        await websocket.send_json(
            {"type": "error", "data": "虚拟机 SSH 运维接入未配置"}
        )
        await websocket.close(code=4000)
        return
    await websocket.accept()
    db = SessionLocal()
    session = None
    try:
        if payload.get("target_ip"):
            binding.ip_address = str(payload["target_ip"])
        # 容器终端:票据里带了 container 则连接后自动 docker exec 进入
        # (与设备终端 /ws/terminal 的同一段三段式 fallback;容器名已过白名单校验)
        # bash 优先(历史/补全体验),镜像里没有再退 sh/ash。
        container_val = payload.get("container") or None
        initial_command = None
        if container_val:
            try:
                container_val = validate_container_name(container_val)
                initial_command = (
                    f"docker exec -it {container_val} bash"
                    f" || docker exec -it {container_val} sh"
                    f" || docker exec -it {container_val} ash"
                )
            except ValueError:
                container_val = None
                initial_command = None
        session = await create_ssh_connection(
            binding,
            binding.username,
            decrypt(binding.password_enc) if binding.password_enc else "",
            db=db,
            private_key=decrypt(binding.ssh_key_enc) if binding.ssh_key_enc else None,
            initial_command=initial_command,
        )
        await websocket.send_json(
            {
                "type": "connected",
                "message": (
                    f"已进入容器 {container_val}"
                    if container_val
                    else f"SSH connected to {binding.ip_address}:{binding.ssh_port}"
                ),
            }
        )

        async def reader():
            while session and session.is_connected:
                out = await session.recv_output()
                if out is None:
                    break
                await websocket.send_json({"type": "output", "data": out})

        async def writer():
            while True:
                msg = await websocket.receive_json()
                typ = msg.get("type")
                if typ == "input":
                    await session.send_input(str(msg.get("data", "")))
                elif typ == "resize":
                    await session.resize_pty(
                        int(msg.get("cols", 80)), int(msg.get("rows", 24))
                    )
                elif typ == "paste":
                    await session.send_input(str(msg.get("data", "")))

        done, pending = await asyncio.wait(
            [asyncio.create_task(reader()), asyncio.create_task(writer())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    except Exception as exc:
        logger.warning("PVE SSH console error: %s", exc)
        try:
            await websocket.send_json({"type": "error", "data": "SSH 连接失败"})
        except Exception:
            pass
    finally:
        if session:
            await session.close()
        db.close()
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/ws/pve-rdp")
async def pve_rdp_ws(
    websocket: WebSocket, ticket: str = Query(...), session_id: str | None = Query(None)
):
    payload = consume_ticket(ticket)
    if (
        not payload
        or payload.get("kind") != "pve-remote"
        or payload.get("mode") != "rdp"
    ):
        await websocket.accept(subprotocol="guacamole")
        await websocket.close(code=4001)
        return
    # RDP/Guacamole requires its WebSocket subprotocol during acceptance.
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == int(payload.get("user_id", 0))).first()
        authorized = bool(
            user
            and user.is_active
            and user_can_access_pve(
                user,
                db,
                manage=True,
                guest=(
                    int(payload["conn_id"]),
                    str(payload.get("gtype") or "qemu"),
                    int(payload["vmid"]),
                )
                if payload.get("conn_id") and payload.get("vmid")
                else None,
            )
            and int(getattr(user, "session_version", 0))
            == int(payload.get("session_version", 0))
        )
    finally:
        db.close()
    if not authorized:
        await websocket.accept(subprotocol="guacamole")
        await websocket.close(code=4003)
        return
    binding = _load_binding(int(payload.get("binding_id", 0)))
    if (
        not binding
        or not binding.enabled
        or not binding.ip_address
        or not binding.username
        or not binding.password_enc
    ):
        await websocket.accept(subprotocol="guacamole")
        await websocket.close(code=4000)
        return
    await websocket.accept(subprotocol="guacamole")
    await _handle_rdp(
        websocket,
        0,
        binding.username,
        decrypt(binding.password_enc),
        user_id=int(payload.get("user_id", 0)),
        width=max(320, min(int(payload.get("width", 1280)), 7680)),
        height=max(240, min(int(payload.get("height", 720)), 4320)),
        session_id=session_id,
        target_host=str(payload.get("target_ip") or binding.ip_address),
        target_port=3389,
        target_name=f"PVE {binding.guest_type}/{binding.vmid}",
        # 必须启用虚拟盘：否则 guacd 不下发 filesystem 指令，前端文件管理器
        # 永远等不到 fsReady，上传按钮被禁用（点了没任何反应）。
        enable_drive=True,
    )


async def _proxy_bidirectional(browser_ws: WebSocket, pve_ws: Any) -> None:
    """浏览器 <-> PVE 双向二进制帧转发;任一方向断开即结束。"""

    async def browser_to_pve() -> None:
        while True:
            message = await browser_ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None and message.get("text") is not None:
                data = message["text"].encode("utf-8")
            if data is not None:
                await pve_ws.send(data)

    async def pve_to_browser() -> None:
        async for data in pve_ws:
            if isinstance(data, str):
                data = data.encode("utf-8")
            await browser_ws.send_bytes(data)

    done, pending = await asyncio.wait(
        [asyncio.create_task(browser_to_pve()), asyncio.create_task(pve_to_browser())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
