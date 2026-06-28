"""Verification script for SessionRecorder command extraction logic."""

import importlib
import importlib.util
import os
import sys

# Direct import of recorder.py without triggering the package __init__
_spec = importlib.util.spec_from_file_location(
    "recorder",
    os.path.join(os.path.dirname(__file__), "app", "services", "recorder.py"),
)
recorder_mod = importlib.util.module_from_spec(_spec)

# Stub out all dependencies BEFORE loading
for mod_name in [
    "app",
    "app.database",
    "app.models",
    "app.models.session_recording",
    "app.models.audit_log",
    "app.services",
    "app.services.settings",
    "app.services.storage",
]:
    sys.modules[mod_name] = type(sys)(mod_name)


class _FakeDB:
    def add(self, *a, **kw):
        pass

    def commit(self):
        pass

    def refresh(self, *a):
        pass

    def query(self, *a):
        return self

    def filter(self, *a):
        return self

    def first(self):
        return None

    def close(self):
        pass


sys.modules["app.database"].SessionLocal = lambda: _FakeDB()
sys.modules["app.models.session_recording"].SessionRecording = None
sys.modules["app.models.audit_log"].AuditLog = None
sys.modules["app.services.settings"].is_recording_enabled = lambda db: False
sys.modules["app.services.settings"].is_command_interception_enabled = lambda db: True
sys.modules["app.services.settings"].get_blocked_commands = lambda db: []
sys.modules["app.services.storage"].upload_recording = lambda *a, **kw: None

_spec.loader.exec_module(recorder_mod)
SessionRecorder = recorder_mod.SessionRecorder
_strip_ansi = recorder_mod._strip_ansi


def make_recorder() -> SessionRecorder:
    rec = SessionRecorder("test", 1, 1, "dev", "10.0.0.1", "root")
    rec.interception_enabled = True
    rec._captured = []

    def _mock_audit(db, event_type, command=None, blocked=False):
        if event_type in ("command_executed", "command_blocked") and command:
            rec._captured.append(
                {"type": event_type, "command": command, "blocked": blocked}
            )

    rec._write_audit = _mock_audit
    return rec


def test_ansi_stripping():
    cases = [
        ("\x1b[?2004h", ""),
        ("\x1b[?2004l", ""),
        ("\x1b[32mhello\x1b[0m", "hello"),
        ("\x1b[1;34mworld\x1b[0m", "world"),
        ("plain text", "plain text"),
        ("\x1b]0;title\x07", ""),
        ("\x1b(B", ""),
        ("\x1b>", ""),
        ("prefix\x1b[?25hsuffix", "prefixsuffix"),
        ("\x1b[?1034h", ""),
        ("\x1b[K", ""),
    ]
    for inp, exp in cases:
        res = _strip_ansi(inp)
        assert res == exp, f"strip({inp!r})={res!r}, want {exp!r}"
    print("[PASS] ANSI stripping")


def test_normal_typing():
    rec = make_recorder()
    for ch in "cd /etc":
        rec.process_input(ch)
    rec.process_input("\r")
    assert rec._captured[0]["command"] == "cd /etc", rec._captured
    print("[PASS] cd /etc")


def test_ls():
    rec = make_recorder()
    for ch in "ls":
        rec.process_input(ch)
    rec.process_input("\r")
    assert rec._captured[0]["command"] == "ls", rec._captured
    print("[PASS] ls")


def test_complex():
    rec = make_recorder()
    for ch in "ls -la /etc/passwd":
        rec.process_input(ch)
    rec.process_input("\r")
    assert rec._captured[0]["command"] == "ls -la /etc/passwd", rec._captured
    print("[PASS] ls -la /etc/passwd")


def test_backspace():
    rec = make_recorder()
    for ch in "lss":
        rec.process_input(ch)
    rec.process_input("\x7f")
    rec.process_input("\r")
    assert rec._captured[0]["command"] == "ls", rec._captured
    print("[PASS] backspace")


def test_ctrl_c():
    rec = make_recorder()
    for ch in "bad":
        rec.process_input(ch)
    rec.process_input("\x03")
    rec.process_input("\r")
    assert len(rec._captured) == 0, rec._captured
    print("[PASS] Ctrl+C")


def test_ctrl_u():
    rec = make_recorder()
    for ch in "bad":
        rec.process_input(ch)
    rec.process_input("\x15")
    for ch in "ls":
        rec.process_input(ch)
    rec.process_input("\r")
    assert rec._captured[0]["command"] == "ls", rec._captured
    print("[PASS] Ctrl+U")


def test_escape():
    rec = make_recorder()
    for ch in "bad":
        rec.process_input(ch)
    rec.process_input("\x1b")
    rec.process_input("\r")
    assert len(rec._captured) == 0, rec._captured
    print("[PASS] Escape")


def test_cr_not_in_buffer():
    rec = make_recorder()
    for ch in "ls":
        rec.process_input(ch)
    assert rec.input_buffer == "ls", rec.input_buffer
    rec.process_input("\r")
    assert rec.input_buffer == "", rec.input_buffer
    print("[PASS] \\r not in buffer")


def test_visible_line():
    rec = make_recorder()
    rec._visible_line = "root@host:~$ cd /etc"
    assert rec._extract_command_from_visible_line() == "cd /etc"
    rec._visible_line = "[root@host ~]# ls -la"
    assert rec._extract_command_from_visible_line() == "ls -la"
    print("[PASS] visible line extraction")


def test_multi_commands():
    rec = make_recorder()
    cmds = ["ls", "cd /etc", "cat passwd", "cd ..", "pwd"]
    for cmd in cmds:
        for ch in cmd:
            rec.process_input(ch)
        rec.process_input("\r")
    recorded = [c["command"] for c in rec._captured]
    assert recorded == cmds, f"{recorded} != {cmds}"
    print(f"[PASS] multi-command: {recorded}")


def test_blocked():
    import re

    rec = make_recorder()
    rec.blocked_patterns = [re.compile(r"rm\s+-rf", re.IGNORECASE)]
    for ch in "rm -rf /":
        rec.process_input(ch)
    fwd, warn = rec.process_input("\r")
    assert fwd is False and rec._captured[0]["blocked"], rec._captured
    print("[PASS] blocked command")


def test_corrupted_visible_line():
    rec = make_recorder()
    rec._visible_line = "GARBAGE?2004h nonsense"
    for ch in "ls":
        rec.process_input(ch)
    rec.process_input("\r")
    assert rec._captured[0]["command"] == "ls", rec._captured
    print("[PASS] corrupted visible_line ignored")


if __name__ == "__main__":
    tests = [
        test_ansi_stripping,
        test_normal_typing,
        test_ls,
        test_complex,
        test_backspace,
        test_ctrl_c,
        test_ctrl_u,
        test_escape,
        test_cr_not_in_buffer,
        test_visible_line,
        test_multi_commands,
        test_blocked,
        test_corrupted_visible_line,
    ]
    p = f = 0
    for t in tests:
        try:
            t()
            p += 1
        except AssertionError as e:
            print(f"[FAIL] {t.__name__}: {e}")
            f += 1
        except Exception as e:
            print(f"[ERROR] {t.__name__}: {type(e).__name__}: {e}")
            f += 1
    print(f"\n{'=' * 50}\nResults: {p}/{len(tests)} passed")
    sys.exit(1 if f else 0)
