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

import json
import logging
import secrets
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

try:
    from app.config import WS_TICKET_STORAGE
except Exception:
    WS_TICKET_STORAGE = "memory://"

_redis = None
if WS_TICKET_STORAGE.startswith("redis://"):
    try:
        import redis

        _redis = redis.Redis.from_url(WS_TICKET_STORAGE, decode_responses=True)
    except ImportError:
        logger.warning(
            "WS_TICKET_STORAGE uses Redis but redis package is unavailable; using memory"
        )

TICKET_TTL_SECONDS = 30
_lock = threading.Lock()
# ticket -> (expires_at_epoch, payload)
_tickets: dict[str, tuple[float, dict[str, Any]]] = {}
_credentials: dict[str, tuple[float, dict[str, Any]]] = {}


def _prune_locked(now: float) -> None:
    expired = [k for k, (exp, _) in _tickets.items() if exp <= now]
    for k in expired:
        del _tickets[k]
    expired_credentials = [k for k, (exp, _) in _credentials.items() if exp <= now]
    for k in expired_credentials:
        del _credentials[k]


def issue_ticket(payload: dict[str, Any]) -> str:
    """Issue a single-use ticket bound to ``payload``. Returns the opaque ticket."""
    ticket = secrets.token_urlsafe(32)
    expires = time.time() + TICKET_TTL_SECONDS
    if _redis is not None:
        try:
            _redis.setex(
                f"dcn:ws-ticket:{ticket}", TICKET_TTL_SECONDS, json.dumps(payload)
            )
            return ticket
        except Exception:
            logger.warning(
                "Redis ticket store unavailable; falling back to process memory"
            )
    with _lock:
        _prune_locked(time.time())
        _tickets[ticket] = (expires, dict(payload))
    return ticket


def peek_ticket(ticket: str | None) -> dict[str, Any] | None:
    """Read a ticket payload WITHOUT invalidating it.

    Used by the WebSocket handshake to re-validate the caller's session/permission
    BEFORE consuming the single-use ticket — otherwise a mid-handshake network
    drop burns the ticket and forces a full re-issue round-trip on reconnect.
    Returns None if the ticket is missing or expired.
    """
    if not ticket:
        return None
    if _redis is not None:
        try:
            raw = _redis.get(f"dcn:ws-ticket:{ticket}")
            if raw:
                try:
                    return json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    return None
        except Exception:
            logger.warning("Redis ticket store unavailable; checking process memory")
    with _lock:
        _prune_locked(time.time())
        entry = _tickets.get(ticket)
    if entry is None:
        return None
    expires, payload = entry
    if time.time() > expires:
        return None
    return payload


def consume_ticket(ticket: str | None) -> dict[str, Any] | None:
    """Return and invalidate the ticket (single use).

    Returns None if the ticket is missing, already consumed, or expired.
    """
    if not ticket:
        return None
    if _redis is not None:
        try:
            raw = _redis.getdel(f"dcn:ws-ticket:{ticket}")
            if raw:
                try:
                    return json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    return None
            # It may have been issued during a transient Redis outage and
            # stored in the local fallback below.
        except Exception:
            logger.warning("Redis ticket store unavailable; checking process memory")
    with _lock:
        _prune_locked(time.time())
        entry = _tickets.pop(ticket, None)
    if entry is None:
        return None
    expires, payload = entry
    if time.time() > expires:
        return None
    return payload


def issue_credential(payload: dict[str, Any]) -> str:
    """Store short-lived connection credentials under an opaque reference."""
    ref = secrets.token_urlsafe(32)
    expires = time.time() + TICKET_TTL_SECONDS
    if _redis is not None:
        try:
            _redis.setex(
                f"dcn:ws-credential:{ref}",
                TICKET_TTL_SECONDS,
                json.dumps(payload),
            )
            return ref
        except Exception:
            logger.warning("Redis credential store unavailable; using memory")
    with _lock:
        _prune_locked(time.time())
        _credentials[ref] = (expires, dict(payload))
    return ref


def consume_credential(ref: str | None) -> dict[str, Any] | None:
    """Consume a credential reference exactly once."""
    if not ref:
        return None
    if _redis is not None:
        try:
            raw = _redis.getdel(f"dcn:ws-credential:{ref}")
            if raw:
                try:
                    return json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    return None
        except Exception:
            logger.warning("Redis credential store unavailable; checking memory")
    with _lock:
        _prune_locked(time.time())
        entry = _credentials.pop(ref, None)
    if entry is None:
        return None
    expires, payload = entry
    return payload if time.time() <= expires else None
