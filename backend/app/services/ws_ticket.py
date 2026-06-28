"""Short-lived, single-use terminal session tickets.

A ticket lets the browser open a WebSocket WITHOUT placing the JWT or device
credentials in the URL. The SPA obtains a ticket via an authenticated HTTP
endpoint (cookie-auth); the WS consumes it (single use, short TTL). Credentials
are resolved server-side at issue time, so the ticket payload — and the URL —
never contain a secret.

State is in-process (same caveat as the in-memory rate limiter: not shared across
uvicorn workers). For multi-worker deployments, back this with Redis.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

TICKET_TTL_SECONDS = 30
_lock = threading.Lock()
# ticket -> (expires_at_epoch, payload)
_tickets: dict[str, tuple[float, dict[str, Any]]] = {}


def _prune_locked(now: float) -> None:
    expired = [k for k, (exp, _) in _tickets.items() if exp <= now]
    for k in expired:
        del _tickets[k]


def issue_ticket(payload: dict[str, Any]) -> str:
    """Issue a single-use ticket bound to ``payload``. Returns the opaque ticket."""
    ticket = secrets.token_urlsafe(32)
    expires = time.time() + TICKET_TTL_SECONDS
    with _lock:
        _prune_locked(time.time())
        _tickets[ticket] = (expires, dict(payload))
    return ticket


def consume_ticket(ticket: str | None) -> dict[str, Any] | None:
    """Return and invalidate the ticket (single use).

    Returns None if the ticket is missing, already consumed, or expired.
    """
    if not ticket:
        return None
    with _lock:
        _prune_locked(time.time())
        entry = _tickets.pop(ticket, None)
    if entry is None:
        return None
    expires, payload = entry
    if time.time() > expires:
        return None
    return payload
