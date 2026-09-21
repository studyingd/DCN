"""
Core inspection service — orchestrate command execution, result parsing,
and database persistence for device inspections.

通道分发:Windows → WinRM(run_ps 执行 PowerShell);Linux → SSH(TOFU)。
"""

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.device import Device
from app.models.inspection import InspectionRecord
from app.models.inspection_item import InspectionItemResult
from app.services.device_credentials import resolve_credentials
from app.services.inspection_commands import get_commands, get_target_type
from app.services.inspection_parser import parse_item
from app.services.ssh import HostKeyMismatchError, connect_device, exec_ssh_command
from app.services.ssh_session import ReusedSshSession

logger = logging.getLogger(__name__)


def _resolve_creds(
    device: Device,
    credential_id: int | None = None,
    username: str | None = None,
    password: str | None = None,
    ssh_key: str | None = None,
) -> tuple[str, str, str]:
    """Resolve SSH credentials: request-level credential → device-bound → defaults."""
    resolved = resolve_credentials(device, username, password, ssh_key)
    return resolved[0] or "root", resolved[1], resolved[2]


def _exec_ssh_command(
    device: Device,
    username: str,
    password: str,
    command: str,
    timeout: int = 15,
    private_key: str | None = None,
    session: ReusedSshSession | None = None,
) -> tuple[int, str, str]:
    """Run a single SSH command on a device with host-key verification (TOFU).

    session 提供时经它复用连接(一轮巡检一次握手,见 ssh_session 模块注释);
    None 时保持旧行为:每条命令独立开/关一次连接。两条路径的 host key
    校验口径一致(均经 connect_device)。
    """
    db = SessionLocal()
    try:
        if session is not None:
            try:
                return session.exec(
                    db,
                    device,
                    username,
                    password,
                    private_key or "",
                    command,
                    timeout,
                )
            except HostKeyMismatchError:
                return 1, "", "SSH host key mismatch — possible MITM"
        try:
            client, _key = connect_device(
                device,
                username,
                password,
                db=db,
                timeout=timeout,
                private_key=private_key,
            )
        except HostKeyMismatchError:
            return 1, "", "SSH host key mismatch — possible MITM"
        try:
            return exec_ssh_command(client, command, timeout=timeout)
        finally:
            client.close()
    finally:
        db.close()


def _exec_command(
    device: Device,
    username: str,
    password: str,
    command: str,
    timeout: int = 15,
    private_key: str | None = None,
    session: ReusedSshSession | None = None,
) -> tuple[int, str, str]:
    """按设备类型分发执行:Windows → WinRM,其余 → SSH(可复用会话)。"""
    if device.is_windows:
        from app.services.winrm import WinRMError, run_on_device

        try:
            return run_on_device(device, username, password, command, timeout=timeout)
        except WinRMError as exc:
            return 1, "", str(exc)
    return _exec_ssh_command(
        device, username, password, command, timeout, private_key, session=session
    )


def _failed_item(message: str) -> dict:
    """命令没能产出可解析数据时的统一结果形状。

    value/unit 一律留空：失败项不该显示任何数字。否则前端会把错误信息
    当成指标渲染出「25.0 %  正常」这种假数据。
    """
    return {
        "success": False,
        "value": None,
        "unit": None,
        "status": "error",
        "details": None,
        "error_message": (message or "命令执行失败")[:500],
    }


def _build_item_result(
    item_type: str,
    target_type: str,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> tuple[dict, str]:
    """把一次命令执行的 (exit_code, stdout, stderr) 变成一个巡检项结果。

    返回 ``(parsed, raw_output)``。raw_output 仅供详情页的「原始输出」弹窗展示，
    不参与解析。

    两条铁律：

    1. **exit_code != 0 时不解析**。此时 stdout 为空、stderr 是错误信息，
       喂给解析器会解析出垃圾：``_last_percent`` 从 ``connect timeout=25`` 里
       捞出 25.0 当 CPU 使用率，``_parse_failed_services`` 把整行错误数成
       「1 个失败服务」，于是六项全挂的机器看上去「4 项正常」。
    2. **解析器判定失败时 status 必须收敛成 error**，否则 normal_count 被污染，
       ``_derive_overall_status`` 会把本该 failed 的记录判成 partial。
    """
    raw_output = stdout or stderr
    if exit_code != 0:
        return _failed_item(stderr or f"命令退出码 {exit_code}"), raw_output

    # 成功时只解析 stdout：stderr 是 PowerShell/命令的告警流，拿它当数据同样出垃圾。
    parsed = parse_item(item_type, stdout or "", target_type)
    if not parsed.get("success", True):
        parsed["status"] = "error"
        if stderr and not parsed.get("error_message"):
            parsed["error_message"] = stderr[:500]
    return parsed, raw_output


def _derive_overall_status(counts: dict[str, int], total: int) -> str:
    """按各检查项的状态汇总出整条巡检记录的结论。

    warning 同样是异常项（内存 80%、1 个服务挂掉都落在这一档）。只认 critical 的话，
    「有告警但没到严重」的机器会被判成 completed，前端渲染成「正常」，自动化任务也
    跟着显示成功，问题被静默吞掉。
    """
    if counts["error"] == total:
        return "failed"
    if counts["error"] + counts["critical"] > total / 2:
        return "failed"
    if counts["error"] or counts["critical"] or counts["warning"]:
        return "partial"
    return "completed"


def run_inspection(
    device: Device,
    mode: str = "standard",
    custom_items: list[str] | None = None,
    credential_id: int | None = None,
    username: str | None = None,
    password: str | None = None,
    ssh_key: str | None = None,
    user_id: int | None = None,
    batch_id: str | None = None,
    timeout: int = 30,
) -> dict:
    """
    Execute a full inspection on a single device.

    Returns a dict with record_id and all item results.
    """
    target_type = get_target_type(device)

    # Get command set
    cmds = get_commands(target_type, mode, custom_items)
    if not cmds:
        return {
            "device_id": device.id,
            "device_name": device.name,
            "ip_address": device.ip_address,
            "target_type": target_type,
            "status": "failed",
            "total_items": 0,
            "normal_count": 0,
            "warning_count": 0,
            "critical_count": 0,
            "error_count": 0,
            "items": [],
            "duration_ms": 0,
            "error": "无可用巡检命令",
        }

    # Resolve credentials
    ssh_user, ssh_pass, ssh_key_value = _resolve_creds(
        device, credential_id, username, password, ssh_key
    )

    # Create DB record
    db = SessionLocal()
    record_id = None
    try:
        record = InspectionRecord(
            device_id=device.id,
            device_name=device.name,
            device_ip=device.ip_address,
            target_type=target_type,
            # 精确 OS 名留档:设备台账值 / 虚拟机 QGA pretty-name,报告「系统」列用
            os_name=str(getattr(device, "os_system", "") or "") or None,
            mode=mode,
            status="running",
            total_items=len(cmds),
            started_at=datetime.now(timezone.utc),
            triggered_by=user_id,
            batch_id=batch_id,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        record_id = record.id
    finally:
        db.close()

    # Execute commands and parse results
    item_results = []
    start_time = time.time()

    # Linux 目标整轮复用一条 SSH 连接:实测部分目标机(如 CentOS 7)每次握手
    # 认证要 5~10s,而单条巡检命令只要 0.1s,6 条 core 命令逐条重连会白耗
    # 30~60s 纯握手。Windows 走 WinRM(无状态 HTTP),无需会话。
    # 命令、超时、host key 校验/TOFU、单条失败继续下一项——全部与旧行为
    # 一致,变的只是握手次数。
    ssh_session = None if device.is_windows else ReusedSshSession()
    try:
        for cmd_info in cmds:
            item_start = time.time()
            item_type = cmd_info["item_type"]
            command = cmd_info["command"]
            cmd_timeout = min(cmd_info["timeout"], timeout)

            try:
                exit_code, stdout, stderr = _exec_command(
                    device,
                    ssh_user,
                    ssh_pass,
                    command,
                    cmd_timeout,
                    ssh_key_value or None,
                    session=ssh_session,
                )
                parsed, raw_output = _build_item_result(
                    item_type, target_type, exit_code, stdout, stderr
                )
            except Exception as exc:
                logger.warning(
                    "Inspection command failed: %s on %s: %s",
                    item_type,
                    device.name,
                    exc,
                )
                parsed = _failed_item(str(exc))
                raw_output = ""

            item_duration = int((time.time() - item_start) * 1000)
            parsed["item_type"] = item_type
            parsed["command_used"] = command
            parsed["raw_output"] = (raw_output or "")[:5000]
            parsed["duration_ms"] = item_duration
            item_results.append(parsed)
    finally:
        if ssh_session is not None:
            ssh_session.close()

    total_duration = int((time.time() - start_time) * 1000)

    # Aggregate counts
    counts = {"normal": 0, "warning": 0, "critical": 0, "error": 0}
    for r in item_results:
        status = r.get("status", "error")
        counts[status] = counts.get(status, 0) + 1

    # Determine overall status
    overall_status = _derive_overall_status(counts, len(item_results))

    # Update DB record and persist items
    db = SessionLocal()
    try:
        record = (
            db.query(InspectionRecord).filter(InspectionRecord.id == record_id).first()
        )
        if record:
            record.status = overall_status
            record.normal_count = counts.get("normal", 0)
            record.warning_count = counts.get("warning", 0)
            record.critical_count = counts.get("critical", 0)
            record.error_count = counts.get("error", 0)
            record.finished_at = datetime.now(timezone.utc)
            record.duration_ms = total_duration

            # Persist item results
            for r in item_results:
                item = InspectionItemResult(
                    record_id=record_id,
                    item_type=r["item_type"],
                    success=r.get("success", False),
                    value=r.get("value"),
                    unit=r.get("unit"),
                    status=r.get("status", "error"),
                    details=r.get("details"),
                    raw_output=r.get("raw_output"),
                    error_message=r.get("error_message"),
                    command_used=r.get("command_used"),
                    executed_at=datetime.now(timezone.utc),
                    duration_ms=r.get("duration_ms"),
                )
                db.add(item)

            db.commit()
    finally:
        db.close()

    return {
        "device_id": device.id,
        "device_name": device.name,
        "ip_address": device.ip_address,
        "target_type": target_type,
        "record_id": record_id,
        "status": overall_status,
        "total_items": len(item_results),
        "normal_count": counts.get("normal", 0),
        "warning_count": counts.get("warning", 0),
        "critical_count": counts.get("critical", 0),
        "error_count": counts.get("error", 0),
        "duration_ms": total_duration,
        "items": item_results,
    }


def _failure_result(
    did: int,
    device: Device | None,
    error: str,
) -> dict:
    """组装一条失败的单台巡检结果(设备不存在 / 未配置 IP / 执行抛错)。"""
    return {
        "device_id": did,
        "device_name": device.name if device else f"unknown-{did}",
        "ip_address": getattr(device, "ip_address", None),
        "target_type": get_target_type(device) if device else "unknown",
        "status": "failed",
        "total_items": 0,
        "normal_count": 0,
        "warning_count": 0,
        "critical_count": 0,
        "error_count": 0,
        "duration_ms": 0,
        "items": [],
        "error": error,
    }


def run_batch_inspection(
    device_ids: list[int],
    mode: str = "standard",
    custom_items: list[str] | None = None,
    credential_id: int | None = None,
    username: str | None = None,
    password: str | None = None,
    ssh_key: str | None = None,
    user_id: int | None = None,
    timeout: int = 30,
) -> tuple[list[dict], str]:
    """
    Concurrent batch inspection. Devices are inspected in parallel (bounded),
    each with its own DB session and SSH/WinRM channel — sequential execution
    pinned a threadpool worker for minutes on large batches. Every InspectionRecord
    shares one batch_id for grouping; it is returned alongside the results.

    Returns (results, batch_id). results are ordered to match device_ids.
    """
    batch_id = str(uuid.uuid4())

    db = SessionLocal()
    try:
        devices = db.query(Device).filter(Device.id.in_(device_ids)).all()
        # Detach: each worker thread gets plain attribute holders, not ORM objects
        # bound to this (about-to-close) session.
        device_map = {d.id: d for d in devices}
        for d in devices:
            db.expunge(d)
    finally:
        db.close()

    def _run_one(did: int) -> dict:
        device = device_map.get(did)
        if not device:
            return _failure_result(did, None, "设备不存在")
        if not device.ip_address:
            return _failure_result(did, device, "设备未配置IP地址")
        try:
            return run_inspection(
                device=device,
                mode=mode,
                custom_items=custom_items,
                credential_id=credential_id,
                username=username,
                password=password,
                ssh_key=ssh_key,
                user_id=user_id,
                batch_id=batch_id,
                timeout=timeout,
            )
        except Exception as exc:
            logger.exception("Batch inspection failed for device %s", device.name)
            return _failure_result(did, device, str(exc)[:200])

    workers = max(1, min(len(device_ids), 8))
    if workers == 1:
        results_by_id = {did: _run_one(did) for did in device_ids}
    else:
        results_by_id = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for did, res in zip(
                device_ids, pool.map(_run_one, device_ids), strict=True
            ):
                results_by_id[did] = res

    # 保持入参顺序,前端按设备顺序对照结果
    return [results_by_id[did] for did in device_ids], batch_id
