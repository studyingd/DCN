"""
Interface status service — fetch live network interface up/down state
for devices over SSH, with per-device TTL cache.

Used by the rack U-view to render port LEDs.
"""

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.credential import Credential
from app.models.device import Device
from app.models.rack import Rack
from app.services.crypto import decrypt
from app.services.inspection_parser import (
    parse_interface_brief,
    parse_linux_links,
    parse_windows_adapters,
)
from app.services.ssh import connect_device, exec_ssh_command

logger = logging.getLogger(__name__)

# ── Per-vendor / per-OS commands ──

_NETWORK_COMMANDS = {
    "huawei": "display interface brief",
    "h3c": "display interface brief",
    "cisco": "show ip interface brief",
    "ruijie": "show interfaces status",
    "zte": "show interface brief",
}
_LINUX_CMD = "ip -o link"
_WINDOWS_CMD = r"powershell -Command \"Get-NetAdapter | Select-Object Name,Status | Format-Table -AutoSize\""

# ── Cache state ──

_cache_ttl_seconds = 25
_cache: dict[int, dict] = {}  # device_id -> entry

# Per-device locks dedup concurrent interface-status fetches so two callers for
# the same device don't open duplicate SSH connections (the previous _inflight
# asyncio.Lock dict was declared but never wired in).
_device_locks: dict[int, threading.Lock] = {}
_device_locks_guard = threading.Lock()


def _device_lock(device_id: int) -> threading.Lock:
    with _device_locks_guard:
        lock = _device_locks.get(device_id)
        if lock is None:
            lock = threading.Lock()
            _device_locks[device_id] = lock
        return lock


def _vendor_from_device(device: Device) -> str | None:
    """Heuristic vendor detection from device.os_system."""
    os_lower = (device.os_system or "").lower()
    if "huawei" in os_lower or "vrp" in os_lower:
        return "huawei"
    if "cisco" in os_lower or "ios" in os_lower:
        return "cisco"
    if "h3c" in os_lower or "comware" in os_lower:
        return "h3c"
    if "ruijie" in os_lower or "rgos" in os_lower:
        return "ruijie"
    if "zte" in os_lower:
        return "zte"
    return None


def _target_type(device: Device) -> str:
    """Same logic as inspection_commands.get_target_type but without the import."""
    if device.type in ("switch", "router", "firewall"):
        return "network"
    os_lower = (device.os_system or "").lower()
    if "windows" in os_lower:
        return "windows"
    return "linux"


def _command_for(device: Device) -> tuple[str, str] | None:
    """Return (target_type, command) or None if not inspectable."""
    ttype = _target_type(device)
    if ttype == "network":
        vendor = _vendor_from_device(device) or "huawei"
        return ttype, _NETWORK_COMMANDS.get(vendor, _NETWORK_COMMANDS["huawei"])
    if ttype == "linux":
        return ttype, _LINUX_CMD
    if ttype == "windows":
        return ttype, _WINDOWS_CMD
    return None


def _resolve_creds(device: Device) -> tuple[str, str] | None:
    """Resolve SSH creds from device.credential_id; return None if not available."""
    if not device.credential_id:
        return None
    db = SessionLocal()
    try:
        cred = (
            db.query(Credential).filter(Credential.id == device.credential_id).first()
        )
        if not cred:
            return None
        username = cred.username or ""
        password = decrypt(cred.password_enc) if cred.password_enc else ""
        if not username or not password:
            return None
        return username, password
    finally:
        db.close()


def _exec_ssh_one(
    device: Device, username: str, password: str, command: str, timeout: int = 8
):
    """Synchronous SSH exec with host-key verification (TOFU); raises on failure."""
    db = SessionLocal()
    try:
        client, _key = connect_device(device, username, password, db=db, timeout=timeout)
        try:
            _exit, out, _err = exec_ssh_command(client, command, timeout=timeout)
            return out
        finally:
            client.close()
    finally:
        db.close()


def _unavailable(device_id: int, status: str, error: str) -> dict:
    return {
        "device_id": device_id,
        "status": status,
        "fetched_at": None,
        "interfaces": [],
        "total": 0,
        "up": 0,
        "down": 0,
        "source": "unavailable",
        "error": error,
    }


def _from_cache_or_fetch(device: Device) -> dict:
    """
    Returns the status entry for a device.
    Online devices: cache-through; offline or no-creds: instant unavailable.
    """
    now = time.time()

    # Fast path: cache hit (any age, any source)
    cached = _cache.get(device.id)
    if cached:
        age = now - cached["_ts"]
        if age < _cache_ttl_seconds:
            return {k: v for k, v in cached.items() if k != "_ts"}

    # Device offline? skip SSH
    if (device.status or "").lower() != "online":
        entry = _unavailable(device.id, device.status or "offline", "device offline")
        entry["_ts"] = now
        _cache[device.id] = entry
        return entry

    # No IP?
    if not device.ip_address:
        entry = _unavailable(device.id, device.status, "no IP address")
        entry["_ts"] = now
        _cache[device.id] = entry
        return entry

    # Resolve creds
    creds = _resolve_creds(device)
    if creds is None:
        entry = _unavailable(device.id, device.status, "no credentials")
        entry["_ts"] = now
        _cache[device.id] = entry
        return entry

    # Pick command
    cmd_info = _command_for(device)
    if cmd_info is None:
        entry = _unavailable(device.id, device.status, "unsupported device type")
        entry["_ts"] = now
        _cache[device.id] = entry
        return entry

    ttype, command = cmd_info
    username, password = creds

    # Serialize per-device SSH fetches: concurrent callers for the same device
    # would otherwise open duplicate SSH connections.
    with _device_lock(device.id):
        # Double-check under the lock — another caller may have just filled it.
        cached = _cache.get(device.id)
        if cached and (now - cached["_ts"]) < _cache_ttl_seconds:
            return {k: v for k, v in cached.items() if k != "_ts"}

        # Run SSH in a thread (paramiko is sync)
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None

        def _do_ssh():
            return _exec_ssh_one(
                device,
                username,
                password,
                command,
            )

        try:
            if loop and loop.is_running():
                # We're inside an async context — use to_thread
                raw = asyncio.run_coroutine_threadsafe(
                    asyncio.to_thread(_do_ssh), loop
                ).result(timeout=12)
            else:
                raw = _do_ssh()
        except Exception as exc:
            logger.info("Interface SSH failed for %s: %s", device.name, exc)
            entry = _unavailable(device.id, device.status, f"ssh: {type(exc).__name__}")
            entry["_ts"] = now
            _cache[device.id] = entry
            return entry

        # Parse output
        if ttype == "network":
            parsed = parse_interface_brief(raw)
        elif ttype == "linux":
            parsed = parse_linux_links(raw)
        elif ttype == "windows":
            parsed = parse_windows_adapters(raw)
        else:
            parsed = {"success": False}

        if not parsed.get("success"):
            entry = _unavailable(device.id, device.status, "no interfaces parsed")
            entry["_ts"] = now
            _cache[device.id] = entry
            return entry

        entry = {
            "device_id": device.id,
            "status": device.status,
            "fetched_at": datetime.now(timezone.utc).isoformat() + "Z",
            "interfaces": parsed["interfaces"][:48],  # cap for sane payload size
            "total": parsed["total"],
            "up": parsed["up"],
            "down": parsed["down"],
            "source": "ssh",
            "error": None,
            "_ts": now,
        }
        _cache[device.id] = entry
        return entry


def get_interface_status(device: Device) -> dict:
    """Public API: fetch interface status for a single device (cache-through)."""
    entry = _from_cache_or_fetch(device)
    # Strip the internal _ts before returning to the API layer
    return {k: v for k, v in entry.items() if k != "_ts"}


def get_interfaces_for_rack(rack: Rack) -> dict:
    """Fetch interface status for every device in a rack. Returns {device_id_str: entry}."""
    result: dict[str, dict] = {}
    for d in rack.devices or []:
        result[str(d.id)] = get_interface_status(d)
    return result
