"""Session recorder — writes asciicast v2 format and audit logs."""

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.audit_log import AuditLog
from app.models.session_recording import SessionRecording
from app.services.settings import (
    get_blocked_commands,
    is_audit_enabled,
    is_command_interception_enabled,
    is_recording_enabled,
)
from app.services.storage import upload_recording

logger = logging.getLogger(__name__)


class SessionRecorder:
    """Records terminal sessions in asciicast v2 format and audits commands."""

    def __init__(
        self,
        session_id: str,
        device_id: int,
        user_id: int,
        device_name: str,
        device_ip: str,
        ssh_username: str,
        conn_type: str = "ssh",
        system_username: str = "",
    ):
        self.session_id = session_id
        self.device_id = device_id
        self.user_id = user_id
        self.device_name = device_name
        self.device_ip = device_ip
        self.ssh_username = ssh_username
        self.conn_type = conn_type
        self.system_username = system_username

        self.start_time = time.time()
        self.events: list[str] = []
        self.input_buffer = ""
        self._max_events = 500000  # Limit recording size to prevent OOM
        self.audit_enabled = False
        self.recording_enabled = False
        self.interception_enabled = False
        self.blocked_patterns: list[re.Pattern] = []
        self._lock = threading.Lock()
        self._db_recording_id: int | None = None
        # Accumulates the current visible terminal line from output echo.
        # When the user presses Enter, we extract the command from this line.
        self._visible_line = ""
        # Set to True when Tab/arrow/Ctrl+R are used — _visible_line has the
        # authoritative command (completions, history).  For normal typing we
        # trust input_buffer instead (no race condition with output echo).
        self._use_visible_line = False

    def initialize(self):
        """Check settings and initialize recording/interception."""
        db: Session = SessionLocal()
        try:
            self.audit_enabled = is_audit_enabled(db)
            self.recording_enabled = is_recording_enabled(db)
            self.interception_enabled = is_command_interception_enabled(db)

            patterns = get_blocked_commands(db)
            self.blocked_patterns = [re.compile(p, re.IGNORECASE) for p in patterns]

            if not self.audit_enabled:
                logger.info("Session %s: audit disabled", self.session_id)
                return

            # Write asciicast header
            if self.recording_enabled:
                header = json.dumps(
                    {
                        "version": 2,
                        "width": 80,
                        "height": 24,
                        "timestamp": int(self.start_time),
                        "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
                        "command": f"{self.conn_type} {self.ssh_username}@{self.device_ip}",
                    }
                )
                self.events.append(header)

                # Create DB record
                rec = SessionRecording(
                    session_id=self.session_id,
                    device_id=self.device_id,
                    user_id=self.user_id,
                    device_name=self.device_name,
                    device_ip=self.device_ip,
                    username=self.system_username or self.ssh_username,
                    conn_type=self.conn_type,
                    started_at=datetime.now(timezone.utc),
                )
                db.add(rec)
                db.commit()
                db.refresh(rec)
                self._db_recording_id = rec.id

            # Log session start
            self._write_audit(db, "session_start")
        finally:
            db.close()

        logger.info(
            "Session %s: recording=%s, interception=%s",
            self.session_id,
            self.recording_enabled,
            self.interception_enabled,
        )

    def record_output(self, data: str):
        """Record terminal output and accumulate the visible line for command extraction."""
        if self.recording_enabled:
            elapsed = round(time.time() - self.start_time, 6)
            event = json.dumps(["o", elapsed, data])
            with self._lock:
                if len(self.events) < self._max_events:
                    self.events.append(event)

        # Strip ANSI escape sequences to get plain text
        plain = _strip_ansi(data)
        # Build visible line character by character.
        # \r (carriage return) moves cursor to start of line — overwrite from beginning.
        # \n (newline) starts a new line — clear the visible line.
        for ch in plain:
            if ch == "\n":
                self._visible_line = ""
            elif ch == "\r":
                # Cursor returns to start of line; subsequent chars overwrite
                self._visible_line = ""
            else:
                self._visible_line += ch

    def process_input(self, data: str) -> tuple[bool, str | None]:
        """Process user input. Returns (should_forward, warning_message).

        Returns:
            - (True, None) if input should be forwarded
            - (False, warning) if command was blocked
        """
        if not self.audit_enabled:
            return True, None

        # Record the raw input for asciicast
        if self.recording_enabled:
            elapsed = round(time.time() - self.start_time, 6)
            event = json.dumps(["i", elapsed, data])
            with self._lock:
                self.events.append(event)

        # Handle special keys for input_buffer tracking
        if data == "\x03":  # Ctrl+C
            self.input_buffer = ""
        elif data == "\x15":  # Ctrl+U — clear line
            self.input_buffer = ""
        elif data == "\x7f" or data == "\x08":  # Backspace / Delete
            self.input_buffer = self.input_buffer[:-1]
        elif data == "\t":  # Tab — output echo will have the completion
            self._use_visible_line = True
        elif data == "\x12":  # Ctrl+R — reverse search
            self._use_visible_line = True
        elif data == "\x1b":  # Bare Escape — cancel
            self.input_buffer = ""
        elif data.startswith("\x1b[A") or data.startswith("\x1b[B"):  # Up/Down arrows
            self._use_visible_line = True
        elif data.startswith("\x1b[C") or data.startswith(
            "\x1b[D"
        ):  # Left/Right arrows
            pass
        elif data == "\r" or data == "\n":
            pass  # Enter — handled below, don't append to buffer
        else:
            # Regular printable characters
            self.input_buffer += data

        # Check if Enter was pressed
        if "\r" in data or "\n" in data:
            if self._use_visible_line:
                command = self._extract_command_from_visible_line()
                if not command:
                    command = self.input_buffer.strip()
            else:
                # Normal typing — input_buffer is synchronous so it never has
                # a race condition with output echo.
                command = self.input_buffer.strip()
                if not command:
                    command = self._extract_command_from_visible_line()

            logger.debug(
                "Session %s command extracted: use_visible=%s buf='%s' vis='%s' cmd='%s'",
                self.session_id,
                self._use_visible_line,
                self.input_buffer,
                self._visible_line,
                command,
            )

            self._use_visible_line = False
            self.input_buffer = ""
            self._visible_line = ""

            if not command:
                return True, None

            # Check against blocked patterns (only when interception is enabled)
            if self.interception_enabled:
                for pattern in self.blocked_patterns:
                    if pattern.search(command):
                        warning = f"\r\n\x1b[41m\x1b[37m [安全拦截] 命令已被阻断: {command} \x1b[0m\r\n"
                        logger.warning(
                            "Blocked command in session %s: %s",
                            self.session_id,
                            command,
                        )

                        db = SessionLocal()
                        try:
                            self._write_audit(
                                db, "command_blocked", command=command, blocked=True
                            )
                        finally:
                            db.close()

                        return False, warning

            # Command allowed — log it
            db = SessionLocal()
            try:
                self._write_audit(db, "command_executed", command=command)
            finally:
                db.close()

        return True, None

    def _has_commands(self) -> bool:
        """Check if any command_executed or command_blocked events were recorded."""
        db = SessionLocal()
        try:
            count = (
                db.query(AuditLog)
                .filter(
                    AuditLog.session_id == self.session_id,
                    AuditLog.event_type.in_(["command_executed", "command_blocked"]),
                )
                .count()
            )
            return count > 0
        finally:
            db.close()

    def _get_recording_duration(self) -> int:
        """Get the actual duration from recording event timestamps (not wall clock)."""
        if len(self.events) < 2:
            return 0
        try:
            last_event = json.loads(self.events[-1])
            if len(last_event) >= 2:
                return int(last_event[1])
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
        return int(time.time() - self.start_time)

    def finish(self):
        """Finalize recording: upload to MinIO, update DB."""
        if not self.audit_enabled:
            return
        db = SessionLocal()
        try:
            has_commands = self._has_commands()

            if not has_commands:
                # Keep the audit trail even when no commands were extracted — the
                # heuristic (_has_commands) parses shell prompts and can miss
                # commands on non-standard prompts, line-editing, Ctrl+R recall,
                # etc. Deleting the recording + audit logs here would silently
                # destroy evidence of a real (possibly destructive) session.
                # Instead: finalize the recording row, write session_end audit,
                # and only skip the (empty) cast upload.
                if self._db_recording_id:
                    rec = (
                        db.query(SessionRecording)
                        .filter(SessionRecording.id == self._db_recording_id)
                        .first()
                    )
                    if rec:
                        rec.ended_at = datetime.now(timezone.utc)
                        rec.duration_seconds = self._get_recording_duration()
                self._write_audit(db, "session_end")
                db.commit()
                logger.info(
                    "Session %s: no commands detected — kept audit trail, "
                    "skipped recording upload",
                    self.session_id,
                )
                return

            self._write_audit(db, "session_end")

            if self.recording_enabled and self.events:
                # Build the cast file content
                cast_content = "\n".join(self.events)

                # Upload to MinIO
                try:
                    file_path = upload_recording(
                        self.session_id, cast_content.encode("utf-8")
                    )
                except Exception as e:
                    logger.error("Failed to upload recording: %s", e)
                    file_path = None

                # Use actual recording duration from event timestamps
                duration = self._get_recording_duration()
                if self._db_recording_id:
                    rec = (
                        db.query(SessionRecording)
                        .filter(SessionRecording.id == self._db_recording_id)
                        .first()
                    )
                    if rec:
                        rec.file_path = file_path
                        rec.file_size = len(cast_content.encode("utf-8"))
                        rec.ended_at = datetime.now(timezone.utc)
                        rec.duration_seconds = duration
                        db.commit()

                logger.info(
                    "Session %s recording saved (%d bytes, %ds)",
                    self.session_id,
                    len(cast_content),
                    duration,
                )
        finally:
            db.close()

    def _write_audit(
        self,
        db: Session,
        event_type: str,
        command: str | None = None,
        blocked: bool = False,
    ):
        log = AuditLog(
            session_id=self.session_id,
            device_id=self.device_id,
            device_name=self.device_name,
            device_ip=self.device_ip,
            user_id=self.user_id,
            username=self.system_username or self.ssh_username,
            event_type=event_type,
            command=command,
            blocked=1 if blocked else 0,
            created_at=datetime.now(timezone.utc),
        )
        db.add(log)
        db.commit()

    def _extract_command_from_visible_line(self) -> str:
        """Extract the command from the accumulated visible line.

        The visible line contains both the prompt and the command the user typed
        (including tab-completed text and history navigation results), as echoed
        by the remote shell. We strip the prompt prefix and return just the command.
        """
        line = self._visible_line.strip()
        if not line:
            return ""
        # Common prompt patterns — the command starts after the last prompt indicator.
        # Order matters: check longer/more specific patterns first.
        for sep in ("$ ", "# ", "> ", "% "):
            idx = line.rfind(sep)
            if idx != -1:
                return line[idx + len(sep) :].strip()
        return line


# ANSI escape sequence pattern — handles CSI (including private mode ? prefix),
# OSC, SGR, and character-set designation sequences.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"  # CSI sequences: ESC [ params letter
    r"|\x1b\].*?(?:\x07|\x1b\\)"  # OSC sequences: ESC ] ... BEL/ST
    r"|\x1b\([B0UK]"  # Character set designation: ESC ( letter
    r"|\x1b[>=<]"  # Keypad mode: ESC =/>/<
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)
