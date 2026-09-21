import logging
import warnings
from datetime import datetime, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import config

logger = logging.getLogger(__name__)


def as_utc_aware(dt: datetime | None) -> datetime | None:
    """Normalize a DB datetime to an aware UTC value.

    ORM datetime columns go through ``app.database.UTCDateTime`` and are already
    aware UTC.  This is for the values that bypass it — raw SQL rows, in-memory
    snapshots, and client-supplied timestamps without an offset — which are all
    treated as UTC, matching the UTC-pinned DB connection.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_isoformat(dt: datetime | None) -> str | None:
    """Serialize a datetime to ISO 8601 with explicit UTC timezone.

    MySQL connections are pinned to UTC. Legacy rows without timezone metadata
    are treated as UTC so clients receive an explicit ``Z`` suffix.

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


def display_timezone() -> tzinfo:
    """Timezone used when rendering timestamps for humans.

    Notifications (Feishu cards, Bitable fields, webhook text) are read by
    operators, so naive-UTC values from the DB must be shifted out of UTC.
    Defaults to ``Asia/Shanghai``; set ``DISPLAY_TIMEZONE`` to override, or to
    an empty string to follow the server's system timezone.  An unknown name
    falls back to system local time instead of breaking delivery.
    """
    name = (config.DISPLAY_TIMEZONE or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("DISPLAY_TIMEZONE=%r 无效，改用服务器系统时区", name)
    return datetime.now(timezone.utc).astimezone().tzinfo


def format_local_time(value: Any, fmt: str = "%Y/%m/%d %H:%M") -> str:
    """Render a timestamp in :func:`display_timezone` for human consumption.

    Accepts a ``datetime`` or an ISO-8601 string.  Values without timezone
    metadata are interpreted as UTC — MySQL ``DATETIME`` columns lose tzinfo on
    the way back out for anything that bypasses ``UTCDateTime``, so treating
    them as local time would display times hours off.  Empty input renders
    "now"; anything unparseable is returned unchanged so no information is
    silently dropped.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            parsed = datetime.now(timezone.utc)
        else:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(display_timezone()).strftime(fmt)


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
