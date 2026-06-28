import warnings
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    """Naive UTC 'now'.

    The DB connection is pinned to UTC (``SET time_zone='+00:00'``) and tzinfo is
    stripped on write, so datetimes read back from the DB are naive-but-UTC.
    Use this helper instead of ``datetime.now()`` (local) when computing values
    that will be compared against DB-read datetimes, so both sides are naive UTC.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_isoformat(dt: datetime | None) -> str | None:
    """Serialize a datetime to ISO 8601 with explicit UTC timezone.

    SQLite strips timezone info on storage, so datetimes read back from the DB
    are naive. This helper ensures every serialized timestamp carries a 'Z'
    suffix so that JavaScript (and other clients) correctly interpret it as UTC.

    Warns when a naive datetime is passed to help catch incorrect datetime
    construction (use datetime.now(timezone.utc) instead of datetime.now()).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        warnings.warn(
            f"utc_isoformat received a naive datetime ({dt}); assuming UTC. "
            "Use datetime.now(timezone.utc) to silence this warning.",
            RuntimeWarning,
            stacklevel=2,
        )
    s = dt.isoformat()
    if dt.tzinfo is None:
        return s + "Z"
    return s


# ── Control-character sanitization (log-injection defense) ───────────
# Characters that can forge new log lines / break parsers when embedded in
# attacker-controlled values that end up in logs.
_CONTROL_CHARS = "".join(chr(c) for c in range(0x20)) + "\x7f"
_CONTROL_TRANSLATE = {
    ord(c): rf"\x{ord(c):02x}" for c in _CONTROL_CHARS if c not in ("\t",)
}


def sanitize_for_log(value: str) -> str:
    """Escape control characters (CR/LF/etc.) so a value cannot forge log lines.

    Reused by the trace middleware for the client-supplied ``x-trace-id`` header
    (the request path is already sanitized inline there).
    """
    if not value:
        return value
    return (
        value.translate(_CONTROL_TRANSLATE)
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def apply_update(obj: Any, data: dict, allowed: list[str]) -> None:
    """Apply only allowlisted fields from ``data`` to ``obj`` via setattr.

    Guards update endpoints against mass-assignment: even if a Pydantic update
    schema later grows a new field, only columns explicitly listed here are
    persisted. ``data`` is typically ``body.model_dump(exclude_unset=True)``.
    """
    for field in allowed:
        if field in data:
            setattr(obj, field, data[field])
