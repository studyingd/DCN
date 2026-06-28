"""Minimal 5-field cron expression parser.

Field order: minute hour day_of_month month day_of_week
Supports: *, specific values, ranges (1-5), step values (*/10), lists (1,3,5)
"""

from datetime import datetime, timedelta


def _parse_field(field: str, min_val: int, max_val: int) -> set[int]:
    result: set[int] = set()
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            step = int(step)
            if "-" in base:
                a, b = base.split("-", 1)
                start, end = int(a), int(b)
            elif base == "*":
                start, end = min_val, max_val
            else:
                start, end = int(base), max_val
            for v in range(start, end + 1, step):
                result.add(v)
        elif "-" in part:
            a, b = part.split("-", 1)
            for v in range(int(a), int(b) + 1):
                result.add(v)
        elif part == "*":
            result.update(range(min_val, max_val + 1))
        else:
            result.add(int(part))
    return result


def parse_cron(expr: str) -> dict:
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"Expected 5 fields, got {len(fields)}: '{expr}'")
    return {
        "minute": _parse_field(fields[0], 0, 59),
        "hour": _parse_field(fields[1], 0, 23),
        "day": _parse_field(fields[2], 1, 31),
        "month": _parse_field(fields[3], 1, 12),
        # cron: Sunday=0; store as-is, convert at comparison time
        "weekday": _parse_field(fields[4], 0, 6),
    }


def next_cron_time(expr: str, after: datetime) -> datetime:
    """Return the next datetime matching the cron expression after *after*."""
    parsed = parse_cron(expr)
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + timedelta(days=366)
    while candidate <= limit:
        # Convert Python weekday (Mon=0..Sun=6) to cron weekday (Sun=0..Sat=6)
        cron_wd = (candidate.weekday() + 1) % 7
        if (
            candidate.month in parsed["month"]
            and candidate.day in parsed["day"]
            and cron_wd in parsed["weekday"]
            and candidate.hour in parsed["hour"]
            and candidate.minute in parsed["minute"]
        ):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError(f"No matching time found within 1 year for: {expr}")
