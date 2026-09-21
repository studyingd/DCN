"""Unified asynchronous automation execution service."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from app.config import AUTOMATION_MAX_CONCURRENT_TARGETS
from app.database import SessionLocal
from app.models.automation import AutomationJob, AutomationJobStep, AutomationJobTarget
from app.models.device import Device
from app.models.pve_guest_binding import PveGuestBinding
from app.services.crypto import decrypt
from app.services.inspection_commands import ITEM_LABELS

logger = logging.getLogger(__name__)


def recover_interrupted_jobs() -> int:
    """服务重启后回收失去后台线程的任务，避免永久卡在 0%/执行中。"""
    db = SessionLocal()
    try:
        jobs = (
            db.query(AutomationJob)
            .filter(AutomationJob.status.in_(("pending", "running")))
            .all()
        )
        count = 0
        for job in jobs:
            job.status = "failed"
            job.finished_at = _utcnow()
            job.summary_json = {
                "total": len(job.targets),
                "succeeded": 0,
                "warnings": 0,
                "failed": len(job.targets),
            }
            for target in job.targets:
                if target.status in ("pending", "running"):
                    target.status = "failed"
                    target.error_message = "服务重启，后台任务已中断"
                    target.finished_at = _utcnow()
            count += 1
        if count:
            db.commit()
        return count
    finally:
        db.close()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def start_automation_job(job_id: int) -> None:
    threading.Thread(
        target=_execute_job,
        args=(job_id,),
        daemon=True,
        name=f"automation-job-{job_id}",
    ).start()


def create_job_record(
    db,
    *,
    name: str,
    job_type: str,
    device_ids: list[int],
    config: dict[str, Any],
    user_id: int | None,
    user_name: str | None,
    trigger_type: str = "manual",
    pve_targets: list[dict[str, Any]] | None = None,
) -> AutomationJob:
    job = AutomationJob(
        name=name.strip(),
        job_type=job_type,
        trigger_type=trigger_type,
        status="pending",
        risk_level={
            "agent": "read_only",
            "inspection": "read_only",
            "script": "high",
            "power": "critical",
        }[job_type],
        config_json=config,
        created_by=user_id,
        created_by_name=user_name,
    )
    devices = db.query(Device).filter(Device.id.in_(device_ids)).all()
    device_map = {device.id: device for device in devices}
    for device_id in device_ids:
        device = device_map.get(device_id)
        if device:
            job.targets.append(
                AutomationJobTarget(
                    device_id=device.id,
                    device_name=device.name,
                    device_ip=device.ip_address,
                )
            )
    for item in pve_targets or []:
        job.targets.append(
            AutomationJobTarget(
                device_id=None,
                device_name=str(item.get("name") or f"PVE VM {item.get('vmid')}"),
                device_ip=None,
                target_type="pve_guest",
                pve_connection_id=int(item["connection_id"]),
                pve_guest_type=str(item.get("guest_type") or "qemu"),
                pve_vmid=int(item["vmid"]),
            )
        )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _append_step(target_id: int, data: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        db.add(
            AutomationJobStep(
                target_id=target_id,
                step_type=str(data.get("step_type") or "execution"),
                step_name=str(data.get("step_name") or "执行步骤")[:255],
                status=str(data.get("status") or "completed"),
                command=data.get("command"),
                exit_code=data.get("exit_code"),
                output=(data.get("output") or "")[:20000] or None,
                error_message=(data.get("error_message") or "")[:2000] or None,
                duration_ms=data.get("duration_ms"),
            )
        )
        db.commit()
    finally:
        db.close()


def _mark_target_running(target_id: int) -> None:
    db = SessionLocal()
    try:
        target = db.query(AutomationJobTarget).filter_by(id=target_id).first()
        if target:
            target.status = "running"
            target.started_at = _utcnow()
            db.commit()
    finally:
        db.close()


def _finish_target(
    target_id: int,
    status: str,
    result: dict[str, Any] | None,
    error: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        target = db.query(AutomationJobTarget).filter_by(id=target_id).first()
        if target:
            finished = _utcnow()
            target.status = status
            target.result_json = result
            target.error_message = error[:2000] if error else None
            target.finished_at = finished
            if target.started_at:
                start = target.started_at
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                target.duration_ms = int((finished - start).total_seconds() * 1000)
            db.commit()
    finally:
        db.close()


def _execute_agent(target: AutomationJobTarget, device: Device, config: dict) -> None:
    from app.services.agent import get_agent_config, run_diagnosis

    db = SessionLocal()
    try:
        cfg = get_agent_config(db)
        if not cfg.enabled or not cfg.configured:
            raise RuntimeError("Agent 未启用或模型配置不完整")
    finally:
        db.close()

    seen_steps = 0

    def persist_new_steps(current_steps):
        nonlocal seen_steps
        # run_diagnosis 在每个 tool 调用后触发回调；将新增步骤即时写入统一任务表。
        while seen_steps < len(current_steps):
            step = current_steps[seen_steps]
            _append_step(
                target.id,
                {
                    "step_type": step.get("tool", "diagnostic"),
                    "step_name": step.get("label") or step.get("key") or "诊断步骤",
                    "status": "completed" if step.get("ok") else "failed",
                    "output": step.get("preview"),
                    "error_message": step.get("error"),
                },
            )
            seen_steps += 1

    result = run_diagnosis(
        device,
        str(config.get("question") or "全面检查系统健康状态"),
        int(config.get("user_id") or 0),
        cfg,
        on_step=persist_new_steps,
        audit_device_id=device.id,
    )
    # 兼容最后一次回调未执行或异常中断的尾部步骤。
    persist_new_steps(result.get("steps", []))
    _finish_target(
        target.id,
        "completed" if result.get("status") == "completed" else "failed",
        {
            "report": result.get("report"),
            "legacy_run_id": result.get("run_id"),
        },
        result.get("error"),
    )


def _pve_runtime_device(target: AutomationJobTarget):
    """Build an unsaved Device-compatible target from persisted PVE access settings."""
    db = SessionLocal()
    try:
        binding = (
            db.query(PveGuestBinding)
            .filter_by(
                connection_id=target.pve_connection_id,
                guest_type=target.pve_guest_type,
                vmid=target.pve_vmid,
                enabled=1,
            )
            .first()
        )
        username = binding.username if binding else None
        if (
            not binding
            or not username
            or not (binding.password_enc or binding.ssh_key_enc)
        ):
            return None
        runtime_ip = binding.ip_address
        runtime_os = binding.os_system
        # Resolve QGA again at execution time so DHCP/address changes do not
        # leave automation targeting an old IP.  Any PVE/QGA failure cleanly
        # falls back to the persisted manual address and OS, preserving the
        # existing non-QGA behavior.
        try:
            from app.models.pve_connection import PveConnection
            from app.services.pve import build_client, guest_agent_summary
            from app.services.pve_guest_status import record_guest_ip

            conn = (
                db.query(PveConnection)
                .filter_by(id=target.pve_connection_id, enabled=1)
                .first()
            )
            if conn:
                pve_client = build_client(conn)
                guests = pve_client.list_guest_resources()
                guest = next(
                    (
                        row
                        for row in guests
                        if int(row.get("vmid", -1)) == int(target.pve_vmid or -1)
                    ),
                    None,
                )
                if guest and guest.get("node"):
                    qga = guest_agent_summary(
                        pve_client,
                        str(guest["node"]),
                        target.pve_guest_type or "qemu",
                        int(target.pve_vmid),
                    )
                    if qga.get("qga_available") and qga.get("qga_ip_address"):
                        runtime_ip = str(qga["qga_ip_address"])
                        # 运行期实时地址记入「最近已知 IP」(停机后告警地址列兜底)
                        record_guest_ip(conn.id, int(target.pve_vmid), runtime_ip)
                    if qga.get("qga_available"):
                        # 优先用 QGA 精确 pretty-name(如 'Microsoft Windows Server 2022
                        # Datacenter' / 'Debian GNU/Linux 12')——巡检记录会把它留档到
                        # os_name 供报告「系统」列显示;取不到再回退粗粒度类型。
                        precise = qga.get("qga_os_name")
                        if precise:
                            runtime_os = str(precise)
                            # 顺带把精确名持久化到 binding(迁移 0038),页面「系统」列
                            # 在 QGA 下次掉线时也还能显示准确值。
                            if binding.os_name != str(precise):
                                binding.os_name = str(precise)
                                db.commit()
                        elif qga.get("qga_os_system") in {"linux", "windows"}:
                            runtime_os = str(qga["qga_os_system"])
        except Exception:
            pass
        # QGA 不可用且 binding 里还没有精确名时,用运维凭据补探一次
        # (SSH 读 /etc/os-release / WinRM 读 Caption),拿到就持久化,
        # 页面「系统」列和巡检报告都直接受益。
        # 本函数在同步线程池里跑,直接用 os_detect 的同步内核。
        if not binding.os_name and runtime_ip:
            try:
                from app.services.os_detect import (
                    probe_linux_os_via_ssh_sync,
                    probe_windows_os_via_winrm_sync,
                )

                is_win = "windows" in (runtime_os or "").lower()
                password = (
                    decrypt(binding.password_enc) if binding.password_enc else None
                )
                precise = None
                if is_win and password:
                    precise = probe_windows_os_via_winrm_sync(
                        runtime_ip, username, password, binding.winrm_port or 5985
                    )
                elif not is_win:
                    precise = probe_linux_os_via_ssh_sync(
                        runtime_ip,
                        username,
                        password or "",
                        port=binding.ssh_port or 22,
                        pinned_key=binding.ssh_host_key,
                        private_key=(
                            decrypt(binding.ssh_key_enc)
                            if binding.ssh_key_enc
                            else None
                        ),
                    )
                if precise:
                    binding.os_name = precise
                    db.commit()
                    runtime_os = precise
            except Exception:
                pass
        if not runtime_ip:
            return None
        from app.services.agent import AgentTarget

        return AgentTarget(
            id=None,
            name=target.device_name,
            ip_address=runtime_ip,
            os_system=runtime_os,
            credential_id=None,
            username=username,
            password=decrypt(binding.password_enc) if binding.password_enc else None,
            ssh_key=decrypt(binding.ssh_key_enc) if binding.ssh_key_enc else None,
            ssh_port=binding.ssh_port,
            winrm_port=binding.winrm_port,
            ssh_host_key=binding.ssh_host_key,
        )
    finally:
        db.close()


def _execute_inspection(target: AutomationJobTarget, device, config: dict) -> None:
    """device 可是 ORM Device 或 PVE 虚拟机的 AgentTarget(明文凭据)。"""
    from app.services.inspection import run_inspection

    # AgentTarget 的凭据是明文字段(username/password/ssh_key),与 Device 的加密
    # 字段(remote_*)不同。显式透传,避免 resolve_credentials 读到空。
    result = run_inspection(
        device=device,
        mode=str(config.get("mode") or "core"),
        custom_items=config.get("items"),
        username=getattr(device, "username", None),
        password=getattr(device, "password", None),
        ssh_key=getattr(device, "ssh_key", None),
        user_id=int(config.get("user_id") or 0),
        timeout=int(config.get("timeout") or 30),
    )
    for item in result.get("items", []):
        item_status = str(item.get("status") or "error")
        item_type = str(item.get("item_type") or "")
        _append_step(
            target.id,
            {
                "step_type": "inspection",
                # 步骤名展示中文名；标签表缺项时回退原始 item_type，绝不写成 None。
                "step_name": ITEM_LABELS.get(item_type, item_type or "巡检项"),
                "status": "completed" if item_status == "normal" else item_status,
                "command": item.get("command_used"),
                "output": item.get("raw_output"),
                "error_message": item.get("error_message"),
                "duration_ms": item.get("duration_ms"),
            },
        )
    status_map = {"completed": "completed", "partial": "warning", "failed": "failed"}
    _finish_target(
        target.id,
        status_map.get(str(result.get("status")), "failed"),
        {
            "record_id": result.get("record_id"),
            "target_type": result.get("target_type"),
            "total_items": result.get("total_items", 0),
            "normal_count": result.get("normal_count", 0),
            "warning_count": result.get("warning_count", 0),
            "critical_count": result.get("critical_count", 0),
            "error_count": result.get("error_count", 0),
        },
        result.get("error"),
    )


def _execute_script(target: AutomationJobTarget, device, config: dict) -> None:
    """device 可是 ORM Device 或 PVE 虚拟机的 AgentTarget(明文凭据)。"""
    from app.services.script_exec import run_script_on_device

    command = str(config.get("command") or "")
    result = run_script_on_device(
        device,
        command,
        int(config.get("timeout") or 30),
        # AgentTarget 的明文凭据显式透传(其 remote_* 字段不存在,
        # resolve_credentials 的设备绑定回退读不到)
        username=getattr(device, "username", None),
        password=getattr(device, "password", None),
        ssh_key=getattr(device, "ssh_key", None),
    )
    _append_step(
        target.id,
        {
            "step_type": "script",
            "step_name": "执行脚本",
            "status": "completed" if result.success else "failed",
            "command": command,
            "exit_code": result.exit_code,
            "output": result.stdout or result.stderr,
            "error_message": result.error
            or (result.stderr if not result.success else None),
        },
    )
    _finish_target(
        target.id,
        "completed" if result.success else "failed",
        result.model_dump(),
        result.error or (result.stderr if not result.success else None),
    )


def _execute_power(target: AutomationJobTarget, device: Device, config: dict) -> None:
    from app.services.script_exec import run_power_on_device

    action = str(config.get("action") or "")
    result = run_power_on_device(device, action)
    _append_step(
        target.id,
        {
            "step_type": "power",
            "step_name": "关机" if action == "shutdown" else "重启",
            "status": "completed" if result.success else "failed",
            "output": result.message,
            "error_message": None if result.success else result.message,
        },
    )
    _finish_target(
        target.id,
        "completed" if result.success else "failed",
        result.model_dump(),
        None if result.success else result.message,
    )


def _execute_pve_power(target: AutomationJobTarget, config: dict) -> None:
    from app.models.pve_connection import PveConnection
    from app.services.pve import build_client

    db = SessionLocal()
    try:
        conn = (
            db.query(PveConnection)
            .filter_by(id=target.pve_connection_id, enabled=1)
            .first()
        )
        if not conn or not target.pve_vmid:
            raise RuntimeError("PVE 虚拟机目标不存在")
        guests = build_client(conn).list_guest_resources()
        guest = next(
            (g for g in guests if int(g.get("vmid", -1)) == target.pve_vmid), None
        )
        if not guest:
            raise RuntimeError("PVE 虚拟机不存在")
        build_client(conn).power(
            str(guest.get("node")),
            target.pve_guest_type or "qemu",
            target.pve_vmid,
            str(config.get("action") or "reboot"),
        )
        _finish_target(target.id, "completed", {"message": "PVE 虚拟机电源操作已发送"})
    except Exception as exc:
        _finish_target(target.id, "failed", None, str(exc))
    finally:
        db.close()


def _run_target(job_id: int, job_type: str, target_id: int, config: dict) -> None:
    """执行单个目标(串行时代的循环体,抽出以便并行):读 target → 执行 → 落终态。

    单台失败只标这台(失败隔离与串行时代一致);目标不存在/设备已删同样落
    failed 终态,不中断其它目标。
    """
    _mark_target_running(target_id)
    db = SessionLocal()
    try:
        target = db.query(AutomationJobTarget).filter_by(id=target_id).first()
        device = (
            db.query(Device).filter_by(id=target.device_id).first() if target else None
        )
        if not target or (not device and target.target_type != "pve_guest"):
            if target:
                _finish_target(target.id, "failed", None, "设备不存在")
            return
        db.expunge(target)
        if device:
            db.expunge(device)
    finally:
        db.close()

    try:
        if target.target_type == "pve_guest":
            # 虚拟机统一经 _pve_runtime_device 转成 AgentTarget(带凭据/IP/OS),
            # 巡检/脚本/Agent 都是连进 guest 内部执行,与电源操作(走 PVE API)分开。
            if job_type == "power":
                _execute_pve_power(target, config)
            else:
                runtime_device = _pve_runtime_device(target)
                if runtime_device is None:
                    raise RuntimeError("PVE 虚拟机尚未配置有效的运维接入(IP/凭据)")
                if job_type == "agent":
                    _execute_agent(target, runtime_device, config)
                elif job_type == "inspection":
                    _execute_inspection(target, runtime_device, config)
                elif job_type == "script":
                    _execute_script(target, runtime_device, config)
                else:
                    raise RuntimeError(f"PVE 虚拟机不支持任务类型 {job_type}")
        elif job_type == "agent":
            _execute_agent(target, device, config)
        elif job_type == "inspection":
            _execute_inspection(target, device, config)
        elif job_type == "script":
            _execute_script(target, device, config)
        elif job_type == "power":
            _execute_power(target, device, config)
    except Exception as exc:
        logger.exception("Automation job %s target %s failed", job_id, target_id)
        _append_step(
            target_id,
            {
                "step_type": job_type,
                "step_name": "执行失败",
                "status": "failed",
                "error_message": str(exc),
            },
        )
        _finish_target(target_id, "failed", None, str(exc))


def _execute_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.query(AutomationJob).filter_by(id=job_id).first()
        if not job:
            return
        job.status = "running"
        job.started_at = _utcnow()
        config = dict(job.config_json or {})
        config["user_id"] = job.created_by
        job_type = job.job_type
        target_ids = [target.id for target in job.targets]
        db.commit()
    finally:
        db.close()

    # 目标并发执行(曾逐台串行,50 台批量被单台耗时线性拖长):
    # 每台结果口径、失败隔离、展示顺序(targets/steps 按 id)均与串行一致;
    # 汇总仍在全部终态后计算。单目标任务退化为直接执行,不开线程池。
    concurrency = max(1, AUTOMATION_MAX_CONCURRENT_TARGETS)
    if len(target_ids) <= 1 or concurrency == 1:
        for target_id in target_ids:
            _run_target(job_id, job_type, target_id, config)
    else:
        with ThreadPoolExecutor(
            max_workers=min(concurrency, len(target_ids)),
            thread_name_prefix=f"automation-job-{job_id}",
        ) as pool:
            futures = [
                pool.submit(_run_target, job_id, job_type, target_id, config)
                for target_id in target_ids
            ]
            for future in as_completed(futures):
                # _run_target 内部已兑住所有异常(落 failed 终态),
                # 这里不会抛;防御性收取,避免未消费的 future 异常被吞。
                future.result()

    db = SessionLocal()
    try:
        job = db.query(AutomationJob).filter_by(id=job_id).first()
        if not job:
            return
        statuses = [target.status for target in job.targets]
        succeeded = sum(status == "completed" for status in statuses)
        warnings = sum(status == "warning" for status in statuses)
        failed = sum(status == "failed" for status in statuses)
        if failed == len(statuses):
            job.status = "failed"
        elif failed or warnings:
            job.status = "partial"
        else:
            job.status = "completed"
        job.summary_json = {
            "total": len(statuses),
            "succeeded": succeeded,
            "warnings": warnings,
            "failed": failed,
        }
        job.finished_at = _utcnow()
        db.commit()
        # 巡检报告投递：必须放在 commit 之后（报告要读 job.finished_at 与
        # 各 target 的 result_json），且绝不能让投递失败反过来影响任务状态。
        _maybe_notify_webhook(db, job)
    finally:
        db.close()


def _notify_flag(job: AutomationJob) -> bool:
    """读任务上的「Webhook 通知」开关(仅健康巡检有效,见 _maybe_notify_webhook)。

    语义是**默认开**：缺键 = 开（已确认 default-on 模型），只有显式 ``false`` 才关。
    这样旧前端（不发送该键）创建的任务也会照常推送，彻底避免
    「开关开了却没发」的反复问题；``report_webhook`` 作为旧键兼容读取。
    """
    config = job.config_json or {}
    if "webhook_notify" in config:
        return bool(config["webhook_notify"])
    if "report_webhook" in config:  # 旧键兼容
        return bool(config["report_webhook"])
    return True  # default-on：缺键视为开


def _maybe_notify_webhook(db, job: AutomationJob) -> None:
    """任务结束后的 Webhook 通知入口——**只有健康巡检会发**。

    Agent 智能判断 / 批量执行 / 电源操作一律不发:这些任务的结果在任务详情里
    实时可见,再推 webhook 只会产生噪音;巡检则走 inspection_report
    (摘要 + PDF 报告,报告里已含失败信息)。

    任何异常只记日志:通知发不出去不能把已跑完的任务标成失败,
    更不能让调度循环中断。
    """
    if job.job_type != "inspection":
        return
    if not _notify_flag(job):
        return
    try:
        from app.services.inspection_report import send_inspection_report

        send_inspection_report(db, job)
    except Exception:
        logger.exception("Failed to send automation notify for job %s", job.id)
