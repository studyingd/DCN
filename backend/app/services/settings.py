"""Settings service — manages feature toggles and blocked commands."""

from sqlalchemy.orm import Session

from app.models.setting import Setting

# Default settings
DEFAULTS = {
    "audit_enabled": "true",
    "recording_enabled": "false",
    "command_interception_enabled": "false",
    "audit_retention_days": "90",
    "recording_retention_days": "90",
    "blocked_commands": '["rm\\s+-rf\\s+/", "dd\\s+if=", "mkfs", "shutdown", "init\\s+[06]", ":(){ :|:& };:", ">\\s*/dev/sda"]',
}


def get_setting(db: Session, key: str) -> str:
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        return row.value
    return DEFAULTS.get(key, "")


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def get_all_settings(db: Session) -> dict:
    result = dict(DEFAULTS)
    rows = db.query(Setting).all()
    for row in rows:
        result[row.key] = row.value
    return result


def is_audit_enabled(db: Session) -> bool:
    return get_setting(db, "audit_enabled").lower() != "false"


def is_recording_enabled(db: Session) -> bool:
    return get_setting(db, "recording_enabled").lower() == "true"


def is_command_interception_enabled(db: Session) -> bool:
    return get_setting(db, "command_interception_enabled").lower() == "true"


def get_blocked_commands(db: Session) -> list[str]:
    import json

    raw = get_setting(db, "blocked_commands")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
