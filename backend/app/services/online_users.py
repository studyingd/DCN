"""In-memory online user tracking via heartbeat."""

import threading
import time

# user_id -> (timestamp, username, role_name)
_online_users: dict[int, tuple[float, str, str]] = {}
_lock = threading.Lock()

HEARTBEAT_TTL = 300  # 5 minutes


def record_heartbeat(user_id: int, username: str, role_name: str = "") -> None:
    now = time.time()
    cutoff = now - HEARTBEAT_TTL
    with _lock:
        # Prune stale entries on every write so the dict cannot grow without
        # bound even if get_online_users() is called rarely or never.
        stale = [uid for uid, (ts, _, _) in _online_users.items() if ts < cutoff]
        for uid in stale:
            del _online_users[uid]
        _online_users[user_id] = (now, username, role_name)


def get_online_users() -> list[dict]:
    """Return list of users with heartbeat within last 5 minutes."""
    cutoff = time.time() - HEARTBEAT_TTL
    with _lock:
        # Prune stale entries
        stale = [uid for uid, (ts, _, _) in _online_users.items() if ts < cutoff]
        for uid in stale:
            del _online_users[uid]
        return [
            {"user_id": uid, "username": uname, "role_name": rname}
            for uid, (_, uname, rname) in _online_users.items()
        ]
