"""Shared SSH helpers: host-key verification (TOFU) and bounded command reads.

Centralizes the paramiko connection setup previously copy-pasted across
``terminal`` / ``inspection`` / ``power`` / ``os_detect`` / ``interface_status``.

Every connection now either:
  * enforces a previously pinned host key (any mismatch → ``HostKeyMismatchError``,
    blocking MITM), or
  * trusts the key on first contact (TOFU) and returns it so the caller persists
    it for enforcement on all subsequent connections.

Command output is read with a hard byte cap so a malicious or misbehaving device
cannot exhaust API-host memory.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import paramiko

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Upper bound on bytes read from a single command's stdout/stderr.
MAX_OUTPUT_BYTES = 2_000_000


class HostKeyMismatchError(Exception):
    """Server SSH host key does not match the pinned value — possible MITM."""


def open_ssh_client(
    host: str,
    port: int,
    username: str,
    password: str,
    *,
    timeout: int = 10,
    banner_timeout: int = 30,
    pinned_key_b64: str | None = None,
) -> tuple[paramiko.SSHClient, str]:
    """Open an SSH connection and return ``(client, remote_host_key_b64)``.

    Auth is password-only (``look_for_keys=False, allow_agent=False``).
    """
    client = paramiko.SSHClient()
    # AutoAdd lets connect succeed; we perform an explicit equality check below so
    # a pinned key is still strictly enforced (mismatch → HostKeyMismatchError).
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    client.connect(
        hostname=host,
        port=port,
        username=username,
        password=password,
        timeout=timeout,
        banner_timeout=banner_timeout,
        look_for_keys=False,
        allow_agent=False,
    )

    transport = client.get_transport()
    if transport is None:
        client.close()
        raise paramiko.SSHException("SSH transport unavailable after connect")
    remote_key = transport.get_remote_server_key()
    remote_key_b64 = remote_key.get_base64()

    if pinned_key_b64 and remote_key_b64 != pinned_key_b64:
        client.close()
        logger.warning(
            "SSH host key mismatch for %s:%d — rejecting (possible MITM)", host, port
        )
        raise HostKeyMismatchError(
            f"SSH host key mismatch for {host}:{port} — possible MITM"
        )
    return client, remote_key_b64


def connect_device(
    device,
    username: str,
    password: str,
    *,
    db: "Session | None" = None,
    timeout: int = 10,
    banner_timeout: int = 30,
) -> tuple[paramiko.SSHClient, str]:
    """Connect to a Device, enforcing/persisting its pinned SSH host key (TOFU).

    On first contact (``device.ssh_host_key`` empty) the captured key is written
    back when a ``db`` session is supplied; every later call enforces it.

    When ``db`` is supplied the device is re-fetched into that session so the
    TOFU write actually commits (the passed ``device`` may belong to another
    session and would otherwise be a silent no-op).
    """
    if db is not None:
        dev = db.get(type(device), device.id) or device
    else:
        dev = device
    pinned = dev.ssh_host_key or None
    client, key = open_ssh_client(
        dev.ip_address,
        dev.ssh_port or 22,
        username,
        password,
        timeout=timeout,
        banner_timeout=banner_timeout,
        pinned_key_b64=pinned,
    )
    if not pinned and db is not None and dev.ssh_host_key != key:
        dev.ssh_host_key = key
        db.commit()
        logger.info("Recorded SSH host key for device %s (id=%s)", dev.name, dev.id)
    return client, key


def _read_bounded(fileobj, max_bytes: int) -> str:
    """Read up to ``max_bytes`` from a paramiko channel file, decoding safely."""
    chunks: list[str] = []
    total = 0
    while total < max_bytes:
        chunk = fileobj.read(min(65536, max_bytes - total))
        if not chunk:
            break
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        chunks.append(chunk)
        total += len(chunk)
    return "".join(chunks).strip()


def exec_ssh_command(
    client: paramiko.SSHClient,
    command: str,
    *,
    timeout: int = 15,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> tuple[int, str, str]:
    """Run a command on an open client; bounded stdout/stderr read.

    Returns ``(exit_code, stdout, stderr)``.
    """
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = _read_bounded(stdout, max_bytes)
    err = _read_bounded(stderr, max_bytes)
    return exit_code, out, err


def exec_on_device(
    device,
    username: str,
    password: str,
    command: str,
    *,
    timeout: int = 15,
    db: "Session | None" = None,
) -> tuple[int, str, str]:
    """Open → exec → close a single command on a Device (with TOFU key pinning)."""
    client, _key = connect_device(
        device, username, password, db=db, timeout=min(timeout, 15)
    )
    try:
        return exec_ssh_command(client, command, timeout=timeout)
    finally:
        client.close()
