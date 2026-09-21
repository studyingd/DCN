"""Target file transfer over SFTP (SSH).

Directory listing, upload, and download for SSH-reachable targets, reusing the
shared SSH connection helper (so TOFU host-key pinning is inherited).

目标有两类，靠路径里的 ``device_id`` 正负号区分（与 containers/automation 共用
同一套合成 ID）：

* ``>= 0``：``devices`` 表里的普通设备，权限口径 ``device:remote`` + 设备 ACL；
* ``< 0``：PVE 虚拟机（``pve_guest_bindings``），权限口径 ``pve:manage`` +
  虚拟机 ACL。此前虚机控制台只能敲命令、不能传文件，就是因为这四个端点
  只认 ``devices`` 行。

Trust model: file transfer does not grant any access the operator doesn't
already have via the SSH terminal — the target filesystem's own permissions
govern what can be read/written.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import socket
import stat
import uuid
from contextlib import contextmanager
from typing import NamedTuple

import paramiko
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.device import Device
from app.models.pve_guest_binding import PveGuestBinding
from app.models.user import User
from app.services.containers_collector import decode_pve_target_id
from app.services.crypto import decrypt
from app.services.device_credentials import resolve_credentials
from app.services.permissions import (
    require_any_permission,
    user_can_access_device,
    user_can_access_pve,
    user_can_use_credential,
    user_has_permission,
)
from app.services.ssh import connect_device
from app.services.ssh_pool import ssh_pool

logger = logging.getLogger(__name__)
router = APIRouter(tags=["files"])

# Hard caps to protect the API host from runaway transfers.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB
DOWNLOAD_CHUNK = 64 * 1024
# 上传分块：既用于接收请求体，也用于 SFTP 写入。已改为边收边写，不再落本地
# 暂存文件，所以这里只需要在“线程跳转次数”与“单块内存”之间取平衡：
# 1MB 配合 paramiko 流水线写入，实测局域网 50MB 约 1.2s。
UPLOAD_CHUNK = 1024 * 1024


class FileAuthRequest(BaseModel):
    credential_id: int | None = None
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=4096)
    ssh_key: str | None = Field(default=None, max_length=16384)


class FileListRequest(FileAuthRequest):
    path: str | None = Field(default=None, max_length=4096)


class FileDownloadRequest(FileAuthRequest):
    path: str = Field(min_length=1, max_length=4096)


class FileTarget(NamedTuple):
    """统一的文件传输目标。

    ``entity`` 是 ``Device`` 或 ``PveGuestBinding``：两者都具备
    ``id / ip_address / ssh_port / ssh_host_key``，因此 ``connect_device`` 的
    TOFU 主机密钥固定逻辑可以直接复用（它按 ``type(entity)`` 回表）。
    """

    kind: str  # "device" | "pve_guest"
    entity: object
    name: str


def _resolve_target(target_id: int, db: Session, current_user: User) -> FileTarget:
    """按 ID 正负号解析目标，并就地完成权限与 ACL 校验。"""
    if target_id >= 0:
        device = db.query(Device).filter(Device.id == target_id).first()
        if device is None:
            raise HTTPException(status_code=404, detail="设备不存在")
        if not user_has_permission(current_user, "device:remote", db):
            raise HTTPException(status_code=403, detail="权限不足")
        if not user_can_access_device(current_user, target_id, db):
            raise HTTPException(status_code=403, detail="无权访问该设备")
        return FileTarget("device", device, device.name)

    decoded = decode_pve_target_id(target_id)
    if not decoded:
        raise HTTPException(status_code=404, detail="PVE 虚拟机目标无效")
    connection_id, vmid = decoded
    binding = (
        db.query(PveGuestBinding)
        .filter_by(connection_id=connection_id, vmid=vmid, enabled=1)
        .first()
    )
    if binding is None:
        raise HTTPException(status_code=404, detail="虚拟机未配置或已停用运维接入")
    # 虚机的 SSH 控制台本身就是 pve:manage 级操作，文件传输保持同一口径，
    # 并叠加虚拟机白名单（device_scope='selected' 时生效）。
    if not user_can_access_pve(
        current_user,
        db,
        manage=True,
        guest=(connection_id, binding.guest_type, vmid),
    ):
        raise HTTPException(status_code=403, detail="无权访问该虚拟机")
    return FileTarget("pve_guest", binding, f"{binding.guest_type.upper()} {vmid}")


def _resolve_creds(
    target: FileTarget,
    credential_id: int | None,
    username: str | None,
    password: str | None,
    ssh_key: str | None,
    current_user: User,
    db: Session,
) -> tuple[str, str, str]:
    """Resolve (username, password, ssh_key)：请求显式值 → 目标已存凭据。"""
    # 前端 FileManager 把未填写的字段发成空串而非 null（axios 不会剔除），
    # 空串若参与"请求值优先"判断，就会把目标上已存的凭据覆盖成空 → SSH 认证
    # 失败。这里统一归一化，设备与虚拟机两条分支都受益。
    username = username or None
    password = password or None
    ssh_key = ssh_key or None
    if target.kind == "pve_guest":
        binding = target.entity
        user_ = (username or binding.username or "").strip()
        password_ = (
            password
            if password is not None
            else (decrypt(binding.password_enc) if binding.password_enc else "")
        )
        key_ = (
            ssh_key
            if ssh_key is not None
            else (decrypt(binding.ssh_key_enc) if binding.ssh_key_enc else "")
        )
        if not user_:
            raise HTTPException(status_code=400, detail="缺少 SSH 凭据")
        return user_, password_, key_

    device = target.entity
    if not user_can_use_credential(current_user, device, credential_id, db):
        raise HTTPException(status_code=403, detail="无权访问该设备")
    u, p, key = resolve_credentials(device, username, password, ssh_key)
    u = u.strip()
    if not u:
        raise HTTPException(status_code=400, detail="缺少 SSH 凭据")
    return u, p, key


def _safe_path(path: str | None) -> str:
    """Normalize a device path; reject null bytes."""
    if not path or path.strip() == "":
        return "."
    if "\x00" in path:
        raise HTTPException(status_code=400, detail="非法路径")
    return posixpath.normpath(path)


def _connect(entity, username: str, password: str, ssh_key: str, db: Session):
    try:
        client, _key = connect_device(
            entity, username, password, db=db, private_key=ssh_key or None
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SSH 连接失败: {exc}")
    return client


# 只有传输层级别的故障才该作废池里的连接；"文件不存在"这类业务错误与连接无关，
# 作废掉只会让下一次操作白白重付一次握手。paramiko 把 SFTP 的 ENOENT/EACCES 也
# 映射成 IOError(即 OSError),所以这里刻意**不含 OSError/socket.error**——否则
# 一次列目录失误就会把别人正在传输的同主机 Transport 一并掐断。
_TRANSPORT_ERRORS = (paramiko.SSHException, EOFError, socket.timeout)


def _pool_key(entity, username: str, password: str, ssh_key: str) -> tuple:
    return ssh_pool.key(
        getattr(entity, "ip_address", "") or "",
        int(getattr(entity, "ssh_port", None) or 22),
        username,
        password,
        ssh_key,
    )


@contextmanager
def _borrowed_sftp(entity, username: str, password: str, ssh_key: str, db: Session):
    """借一条复用的 SSH transport，并在其上新开一个 SFTP channel。

    握手（~164ms）+ ``open_sftp``（~64ms）曾经是每个请求都要重付的固定开销，
    文件管理器一次"列目录/上传/刷新"就是三次。现在 transport 跨请求复用，
    每次只付一次 channel open。SFTPClient 不是线程安全的，所以**只复用
    transport、每次新开 channel**。
    """
    key = _pool_key(entity, username, password, ssh_key)
    client, _created = ssh_pool.acquire(
        key, lambda: _connect(entity, username, password, ssh_key, db)
    )
    # 传输期间阻止 LRU 驱逐——慢速大文件下载的 Transport 不应被掐断。
    ssh_pool.mark_in_use(key)
    sftp = None
    try:
        sftp = client.open_sftp()
        yield sftp
        ssh_pool.touch(key)
    except _TRANSPORT_ERRORS:
        ssh_pool.discard(key)
        raise
    finally:
        ssh_pool.release(key)
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass


@router.post("/api/devices/{device_id}/files/list")
def list_files(
    device_id: int,
    body: FileListRequest,
    current_user: User = Depends(require_any_permission("device:remote", "pve:manage")),
    db: Session = Depends(get_db),
):
    resolved = _resolve_target(device_id, db, current_user)
    target = _safe_path(body.path)
    username_, password_, ssh_key_ = _resolve_creds(
        resolved,
        body.credential_id,
        body.username,
        body.password,
        body.ssh_key,
        current_user,
        db,
    )
    with _borrowed_sftp(resolved.entity, username_, password_, ssh_key_, db) as sftp:
        # Resolve to an ABSOLUTE path so the client can navigate above the
        # SFTP home (e.g. /tmp, /opt) and render `~` for home. normalize()
        # is the SFTP realpath; like listdir it requires the path to exist.
        abs_path = sftp.normalize(target)
        entries = []
        for attr in sftp.listdir_attr(abs_path):
            is_dir = stat.S_ISDIR(attr.st_mode or 0)
            entries.append(
                {
                    "name": attr.filename,
                    "size": attr.st_size or 0,
                    "type": "dir" if is_dir else "file",
                    "mtime": int(attr.st_mtime) if attr.st_mtime else None,
                }
            )
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        return {"path": abs_path, "entries": entries}


@router.post("/api/devices/{device_id}/files/upload")
async def upload_file(
    device_id: int,
    dest_dir: str = Form(...),
    credential_id: int | None = Form(default=None),
    username: str | None = Form(default=None),
    password: str | None = Form(default=None),
    ssh_key: str | None = Form(default=None),
    file: UploadFile = File(...),
    current_user: User = Depends(require_any_permission("device:remote", "pve:manage")),
    db: Session = Depends(get_db),
):
    resolved = _resolve_target(device_id, db, current_user)
    dest_dir = _safe_path(dest_dir)
    username_, password_, ssh_key_ = _resolve_creds(
        resolved, credential_id, username, password, ssh_key, current_user, db
    )
    filename = posixpath.basename(file.filename or "upload.bin")
    dest_path = posixpath.join(dest_dir, filename)
    temp_path = posixpath.join(dest_dir, f".dcn-upload-{uuid.uuid4().hex}.part")
    # 边收边写：不再先落本地暂存文件。
    #
    # 两个理由：
    #   1) 暂存会把传输切成「浏览器→后端」「后端→远端」两段严格串行的过程，
    #      前一段飞快跑完，进度条冲到 99% 后要在第二段干等；
    #   2) 多一次完整的本地磁盘读写。
    #
    # 现在每收到一块就立刻写往远端，TCP 背压会把真实速度回传给浏览器，进度条
    # 因此是可信的。SSH 握手与 SFTP 写入都是阻塞调用，逐个放进线程执行，事件
    # 循环不被占住（同一循环上还跑着 RDP 的 websocket 桥接）。
    total = 0
    completed = False

    pool_key = _pool_key(resolved.entity, username_, password_, ssh_key_)

    def _open() -> tuple:
        # transport 跨请求复用，握手（实测 ~164ms）只在池未命中时付一次。
        client, _created = ssh_pool.acquire(
            pool_key,
            lambda: _connect(resolved.entity, username_, password_, ssh_key_, db),
        )
        try:
            sftp = client.open_sftp()
        except Exception:
            ssh_pool.discard(pool_key)
            raise
        try:
            remote = sftp.file(temp_path, "wb")
        except Exception:
            sftp.close()
            ssh_pool.discard(pool_key)
            raise
        # paramiko 默认每个 write 都等一次服务端确认，一个 RTT 只推进一小块；
        # 开启流水线后写请求连续发出，吞吐通常能提升一个数量级。
        remote.set_pipelined(True)
        return client, sftp, remote

    client, sftp, remote = await asyncio.to_thread(_open)
    try:
        while True:
            chunk = await file.read(UPLOAD_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="文件超过大小上限 (200MB)")
            await asyncio.to_thread(remote.write, chunk)
            # 大文件上传可能超过池的空闲 TTL；每块续租一次，避免并发请求触发
            # 淘汰时把正在使用的 transport 关掉。
            ssh_pool.touch(pool_key)
        # close() 会等待流水线里所有写请求的回复，失败在这里才暴露出来。
        await asyncio.to_thread(remote.close)
        remote = None
        # Atomic replacement on the remote filesystem prevents consumers
        # from observing a partially uploaded destination file.
        await asyncio.to_thread(sftp.rename, temp_path, dest_path)
        completed = True
        ssh_pool.touch(pool_key)
    except _TRANSPORT_ERRORS:
        ssh_pool.discard(pool_key)
        raise
    except Exception:
        # 中途失败也要把已写入的字节数报出去，便于前端区分"没传"和"传了一半"。
        logger.info(
            "Upload to %s failed after %d bytes", resolved.name, total, exc_info=True
        )
        raise
    finally:
        if remote is not None:
            try:
                await asyncio.to_thread(remote.close)
            except Exception:
                pass
        if not completed:
            try:
                await asyncio.to_thread(sftp.remove, temp_path)
            except Exception:
                pass
        try:
            await asyncio.to_thread(sftp.close)
        except Exception:
            pass
        # 只关 channel，transport 留在池里给后续请求复用。

    return {"filename": filename, "path": dest_path, "size": total}


@router.post("/api/devices/{device_id}/files/download")
def download_file(
    device_id: int,
    body: FileDownloadRequest,
    current_user: User = Depends(require_any_permission("device:remote", "pve:manage")),
    db: Session = Depends(get_db),
):
    resolved = _resolve_target(device_id, db, current_user)
    src = _safe_path(body.path)
    username_, password_, ssh_key_ = _resolve_creds(
        resolved,
        body.credential_id,
        body.username,
        body.password,
        body.ssh_key,
        current_user,
        db,
    )
    filename = posixpath.basename(src) or "download.bin"
    # The path is remote/user-controlled; keep it out of response-header
    # syntax so quotes and control characters cannot corrupt Content-Disposition.
    filename = (
        "".join(
            "_" if ord(char) < 32 or ord(char) == 127 or char in {'"', "\\"} else char
            for char in filename
        )[:255]
        or "download.bin"
    )
    pool_key = _pool_key(resolved.entity, username_, password_, ssh_key_)
    client, _created = ssh_pool.acquire(
        pool_key, lambda: _connect(resolved.entity, username_, password_, ssh_key_, db)
    )
    sftp = client.open_sftp()
    try:
        sftp.stat(src)
    except Exception as exc:
        sftp.close()
        ssh_pool.touch(pool_key)
        raise HTTPException(status_code=404, detail=f"文件不存在: {exc}")

    def _stream():
        try:
            with sftp.file(src, "rb") as remote:
                # 顺序读默认每个 chunk 一个 RTT；预读让 paramiko 提前并发拉取，
                # 大文件下载吞吐明显提升。失败时退回顺序读，不影响正确性。
                try:
                    remote.prefetch()
                except Exception:
                    logger.debug("SFTP prefetch unavailable for %s", src)
                while True:
                    chunk = remote.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    # 长下载期间持续续租，避免被并发请求触发的淘汰关掉。
                    ssh_pool.touch(pool_key)
                    yield chunk
        except _TRANSPORT_ERRORS:
            ssh_pool.discard(pool_key)
            raise
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            # 只关 channel，transport 留给后续请求复用。

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(
        _stream(), media_type="application/octet-stream", headers=headers
    )
