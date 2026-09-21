"""巡检报告装配与投递。

周期巡检跑完后，把结果装配成报告数据，生成 PDF，并按用户要求的形态发到飞书：

    先是总体说明（有没有异常）→ 有异常就直接点名「哪台主机的什么异常」→ 附带 PDF

对应 ``automation_schedules.config_json`` 里的 ``report_webhook`` 开关（前端在
创建周期巡检时勾选）。开关默认关闭，不影响既有计划。

只有 ``provider == "feishu_app"`` 的通道能收到 PDF 附件：群自定义机器人的
webhook 只接受 text/post/image/interactive，**没有上传接口**。这类通道会收到
纯文字摘要并在结果里标明附件被跳过，而不是静默什么都不发。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.automation import AutomationJob
from app.models.inspection import InspectionRecord
from app.models.webhook import Webhook
from app.services import feishu
from app.services.crypto import decrypt
from app.services.device_events import is_automation_notify_subscribed
from app.services.feishu import FeishuError
from app.services.inspection_commands import ITEM_LABELS
from app.services.report_pdf import (
    InspectionReportData,
    ReportHost,
    ReportItem,
    build_inspection_pdf,
)
from app.utils import format_local_time

logger = logging.getLogger(__name__)


# PDF 报告「系统」列只显示发行版重点名，不显示全长版本串——
# 'Microsoft Windows Server 2022 Datacenter' → 'Windows Server'、
# 'Debian GNU/Linux 12 (bookworm)' → 'Debian'。
# 顺序敏感：更具体的键必须排在泛化键前面（'windows server' 先于 'windows'）。
_OS_SHORT_NAMES: tuple[tuple[str, str], ...] = (
    ("windows server", "Windows Server"),
    ("windows", "Windows"),
    ("ubuntu", "Ubuntu"),
    ("debian", "Debian"),
    ("centos", "CentOS"),
    ("rocky", "Rocky Linux"),
    ("alma", "AlmaLinux"),
    ("red hat", "RHEL"),
    ("redhat", "RHEL"),
    ("rhel", "RHEL"),
    ("openeuler", "openEuler"),
    ("sles", "SLES"),
    ("suse linux enterprise", "SLES"),
    ("opensuse", "openSUSE"),
    ("fedora", "Fedora"),
    ("oracle linux", "Oracle Linux"),
    ("amazon linux", "Amazon Linux"),
    ("alpine", "Alpine Linux"),
    ("arch linux", "Arch Linux"),
    ("freebsd", "FreeBSD"),
    ("kylin", "Kylin"),
    ("uos", "UOS"),
)


def short_os_name(os_system: str | None, target_type: str) -> str:
    """把精确 OS 名归一化成发行版短名，用于 PDF 报告「系统」列。

    未识别/缺失时按 target_type 回退 'Windows' / 'Linux'——报告的读者关心的是
    哪一族系统，具体版本号不是这一列的职责。
    """
    default = "Windows" if target_type == "windows" else "Linux"
    if not os_system:
        return default
    lowered = os_system.strip().lower()
    if lowered in ("unknown", "linux", "windows"):
        return default
    for needle, label in _OS_SHORT_NAMES:
        if needle in lowered:
            return label
    return default


# 订阅这个事件的 webhook 才会收到巡检报告。
# 与 job.failed / inspection.report 合并后的唯一自动化通知事件。
REPORT_EVENT = "automation.notify"
# 摘要里每台主机最多列几个异常项，超出用「等 N 项」收尾，避免消息糊成一片
_MAX_ITEMS_PER_HOST = 3
# 摘要里单条异常描述的长度上限
_MAX_ITEM_TEXT = 60
# 取证失败原因在摘要里的长度上限（完整堆栈只在 PDF 与原始输出里看）
_MAX_REASON_TEXT = 60
# 同一台主机上有多少项共享同一个失败原因时，合并成一句而不逐项重复
_COLLAPSE_SAME_REASON_AT = 3


def _short_reason(message: str | None) -> str:
    """把异常堆栈压成一句人看得懂的话。

    真实错误信息往往是一整段 Python repr（包含
    ``<HTTPConnection(host=...) at 0x10d607a10>`` 这种东西），直接贴进飞书消息
    既读不懂又占满屏幕。只取第一行、压掉空白、截断。
    """
    raw = (message or "").strip()
    if not raw:
        return ""
    first_line = raw.splitlines()[0].strip()
    first_line = " ".join(first_line.split())
    if len(first_line) > _MAX_REASON_TEXT:
        first_line = f"{first_line[:_MAX_REASON_TEXT]}…"
    return first_line


_STATUS_LABEL = {
    "normal": "正常",
    "warning": "警告",
    "critical": "严重",
    "error": "错误",
}


def _item_label(item_type: str) -> str:
    return ITEM_LABELS.get(item_type, item_type)


def build_report_data(db: Session, job: AutomationJob) -> InspectionReportData | None:
    """从 automation job 的巡检记录装配报告数据。

    拿不到任何巡检记录时返回 None——比如任务全都在连接阶段就失败了，
    这时没有可报告的明细，调用方应降级成纯文字通知。
    """
    hosts: list[ReportHost] = []
    record_ids: list[int] = []
    for target in job.targets:
        result = target.result_json or {}
        record_id = result.get("record_id")
        if isinstance(record_id, int) and record_id > 0:
            record_ids.append(record_id)

    records = {}
    if record_ids:
        rows = (
            db.query(InspectionRecord).filter(InspectionRecord.id.in_(record_ids)).all()
        )
        records = {row.id: row for row in rows}

    # 批量取出所有目标设备的精确操作系统名(device.os_system),供报告「系统」列显示。
    # target_type 只是 windows/linux 这种粗粒度类型,精确版本名(如 'Microsoft Windows
    # Server 2019 Datacenter' / 'Ubuntu 22.04')存在 device.os_system 里,
    # 最终经 short_os_name 归一化成短名再进报告。
    from app.models.device import Device
    from app.models.pve_guest_binding import PveGuestBinding

    device_ids = [t.device_id for t in job.targets if t.device_id]
    os_names: dict[int, str] = {}
    if device_ids:
        for dev in db.query(Device).filter(Device.id.in_(device_ids)).all():
            if dev.os_system:
                os_names[dev.id] = dev.os_system
    # PVE 虚拟机没有 device 行,精确 OS 名从 guest binding 取(按 conn+type+vmid 键)。
    # 优先 os_name(凭据/QGA 探测到的精确名,迁移 0038),退到粗粒度 os_system。
    guest_os: dict[tuple, str] = {}
    pve_targets = [t for t in job.targets if t.pve_connection_id and t.pve_vmid]
    if pve_targets:
        # 批量取 binding(曾逐 target 单查,N 台虚机报告 = N 次查询):
        # 按 connection_id in(...) 一次取回后内存里按 (conn, type, vmid) 键匹配。
        # 不过滤 enabled——与旧逐条查询同口径(报告取的是历史目标的绑定信息,
        # 即使绑定已停用也应能取到留档的 OS 名)。
        binding_map = {
            (b.connection_id, b.guest_type, b.vmid): b
            for b in db.query(PveGuestBinding)
            .filter(
                PveGuestBinding.connection_id.in_(
                    {t.pve_connection_id for t in pve_targets}
                )
            )
            .all()
        }
        for t in pve_targets:
            b = binding_map.get(
                (t.pve_connection_id, t.pve_guest_type or "qemu", t.pve_vmid)
            )
            if b:
                precise = (b.os_name or "").strip() or (b.os_system or "").strip()
                if precise:
                    guest_os[
                        (t.pve_connection_id, t.pve_guest_type or "qemu", t.pve_vmid)
                    ] = precise

    for target in job.targets:
        result = target.result_json or {}
        record = records.get(result.get("record_id"))
        items: list[ReportItem] = []
        if record is not None:
            for item in record.items:
                items.append(
                    ReportItem(
                        label=_item_label(item.item_type),
                        value=item.value,
                        unit=item.unit,
                        status=item.status,
                        error_message=item.error_message,
                    )
                )
        ttype = record.target_type if record else "linux"
        # 优先用巡检时留档的精确 OS 名(迁移 0037);历史记录没有该列、或留的是
        # 粗粒度族类值(早期 _pve_runtime_device 只写 'linux'/'windows')时,
        # 回退到设备台账 / guest binding 查询(binding.os_name 可能已被后续
        # 凭据探测精化,比留档的旧值更准)。
        raw_os = None
        record_os = (record.os_name or "").strip() if record else ""
        if (
            record
            and record_os
            and record_os.lower() not in ("linux", "windows", "unknown")
        ):
            raw_os = record_os
        elif target.device_id:
            raw_os = os_names.get(target.device_id)
        else:
            raw_os = guest_os.get(
                (
                    target.pve_connection_id,
                    target.pve_guest_type or "qemu",
                    target.pve_vmid,
                )
            )
        hosts.append(
            ReportHost(
                name=target.device_name,
                # PVE 虚拟机的 target.device_ip 创建时为 None(IP 靠 QGA/binding
                # 运行时才解析);巡检记录里留的是真实解析到的 IP,回退读它。
                ip=target.device_ip or (record.device_ip if record else None),
                target_type=ttype,
                os_name=short_os_name(raw_os, ttype),
                status=(record.status if record else "failed"),
                duration_ms=(record.duration_ms if record else target.duration_ms),
                record_id=(record.id if record else None),
                items=items,
            )
        )

    if not hosts:
        return None
    return InspectionReportData(
        schedule_name=job.name,
        triggered_at=job.started_at or job.created_at,
        finished_at=job.finished_at or datetime.now(timezone.utc),
        hosts=hosts,
    )


def build_summary_text(data: InspectionReportData) -> str:
    """拼出飞书消息正文。

    结构就是需求里那句话：先总体说明有没有异常，有异常就直接点名哪台主机的
    什么异常。健康主机不进正文（PDF 概览表里有），否则十几台机器会把真正
    要看的那两行顶到屏幕外。
    """
    lines = ["【健康巡检报告】"]
    if data.schedule_name:
        lines.append(f"巡检计划：{data.schedule_name}")
    lines.append(
        f"完成时间：{format_local_time(data.finished_at or datetime.now(timezone.utc))}"
    )
    lines.append(f"覆盖主机：{len(data.hosts)} 台")
    lines.append("")

    if not data.has_anomaly:
        lines.append(f"✅ 本次巡检未发现异常，{len(data.hosts)} 台主机全部正常。")
    else:
        problems = data.problem_hosts
        lines.append(f"⚠️ 本次巡检发现 {len(problems)} 台主机存在异常：")
        for host in problems:
            head = f"· {host.name}"
            if host.ip:
                head += f"({host.ip})"
            lines.append(f"{head}：{_describe_host_problems(host)}")

    lines.append("")
    lines.append("详见附件 PDF 报告。")
    return "\n".join(lines)


def _describe_host_problems(host: ReportHost) -> str:
    """把一台主机的异常写成一句话。

    两种形态分开处理：

    * **取证失败**（status=error）——整台机器连不上时六项会全部 error 且原因
      完全相同。逐项列出来就是六行一样的 WinRM 超时堆栈，所以合并成
      「6 项取证失败：无法连接 WinRM」；
    * **真的超标**（warning/critical）——逐项列出指标与实测值，这才是
      「哪台主机的什么异常」。
    """
    problems = host.problems
    errors = [item for item in problems if item.status == "error"]
    exceeded = [item for item in problems if item.status != "error"]

    bits: list[str] = []
    if exceeded:
        for item in exceeded[:_MAX_ITEMS_PER_HOST]:
            label = _STATUS_LABEL.get(item.status, item.status)
            text = f"{item.label} {item.display}".strip()
            if len(text) > _MAX_ITEM_TEXT:
                text = f"{text[:_MAX_ITEM_TEXT]}…"
            bits.append(f"{text}（{label}）")
        extra = len(exceeded) - _MAX_ITEMS_PER_HOST
        if extra > 0:
            bits.append(f"等 {extra} 项")

    if errors:
        # 按原因分组；同一原因占多数时合并成一句
        reasons: dict[str, int] = {}
        for item in errors:
            key = _short_reason(item.error_message)
            reasons[key] = reasons.get(key, 0) + 1
        top_reason, top_count = max(reasons.items(), key=lambda kv: kv[1])
        if top_count >= _COLLAPSE_SAME_REASON_AT or top_count == len(errors):
            bits.append(f"{top_count} 项取证失败：{top_reason or '原因未知'}")
        else:
            for item in errors[:_MAX_ITEMS_PER_HOST]:
                reason = _short_reason(item.error_message) or "取证失败"
                bits.append(f"{item.label}：{reason}")

    return "；".join(bits) if bits else "存在异常项"


def _report_filename(data: InspectionReportData) -> str:
    moment = data.finished_at or datetime.now(timezone.utc)
    # 文件名用本地展示时区，和报告正文里的时间一致，避免差 8 小时对不上
    stamp = format_local_time(moment, "%Y%m%d-%H%M")
    return f"巡检报告-{stamp}.pdf"


def _deliver_to_webhook(
    webhook: Webhook, summary: str, pdf: bytes, filename: str
) -> dict:
    started = datetime.now(timezone.utc)
    base = {"webhook_id": webhook.id, "name": webhook.name}
    config = webhook.config or {}

    if webhook.provider != "feishu_app":
        # 群自定义机器人没有文件上传接口，只能发文字摘要。
        # 明确告知而不是静默不发——否则用户会以为报告功能坏了。
        try:
            result = feishu.deliver(
                {"event": REPORT_EVENT, "message": summary},
                base=webhook.url,
                app_id=config.get("app_id"),
                app_secret=decrypt(webhook.secret_enc or ""),
                receivers=config.get("receivers"),
                receive_id_type=config.get("receive_id_type")
                or feishu.AUTO_RECEIVE_ID_TYPE,
                style="text",
            )
        except (FeishuError, ValueError) as exc:
            return {
                **base,
                "ok": False,
                "error": str(exc)[:200],
                "attachment": "unsupported",
            }
        return {
            **base,
            "ok": bool(result.get("ok")),
            "error": result.get("error"),
            "attachment": "unsupported",
            "note": "该通道类型不支持文件附件，已只发送文字摘要",
        }

    try:
        if pdf:
            result = feishu.deliver_report(
                summary,
                pdf,
                filename,
                base=webhook.url,
                app_id=config.get("app_id"),
                app_secret=decrypt(webhook.secret_enc or ""),
                receivers=config.get("receivers"),
                receive_id_type=config.get("receive_id_type")
                or feishu.AUTO_RECEIVE_ID_TYPE,
            )
        else:
            # PDF 生成失败（或无报告）：只发文字摘要。
            # 摘要和附件不该同生共死——PDF 坏了结论也得送到。
            result = feishu.deliver_text(
                summary,
                base=webhook.url,
                app_id=config.get("app_id"),
                app_secret=decrypt(webhook.secret_enc or ""),
                receivers=config.get("receivers"),
                receive_id_type=config.get("receive_id_type")
                or feishu.AUTO_RECEIVE_ID_TYPE,
            )
            return {
                **base,
                "ok": bool(result.get("ok")),
                "status_code": result.get("status_code"),
                "error": result.get("error"),
                "attachment": "skipped",
                "note": "PDF 生成失败，已只发送文字摘要",
            }
    except (FeishuError, ValueError) as exc:
        return {
            **base,
            "ok": False,
            "error": str(exc)[:200],
            "duration_ms": int(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000
            ),
        }
    return {
        **base,
        "ok": bool(result.get("ok")),
        "status_code": result.get("status_code"),
        "error": result.get("error"),
        "file_ok": result.get("file_ok"),
        "file_error": result.get("file_error"),
        "delivered": result.get("delivered"),
        "attachment": "sent" if result.get("file_ok") else "failed",
        "duration_ms": int(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000
        ),
    }


def send_inspection_report(db: Session, job: AutomationJob) -> list[dict]:
    """生成并投递巡检报告。返回每个通道的投递结果（供审计与排查）。

    任何异常都在内部吞掉并记日志：报告发不出去不应该把已经跑完的巡检任务
    标成失败，更不能让调度循环中断。
    """
    webhooks = [
        w
        for w in db.query(Webhook).filter(Webhook.enabled.is_(True)).all()
        if is_automation_notify_subscribed(w.events)
    ]
    if not webhooks:
        return []

    try:
        data = build_report_data(db, job)
        if data is None:
            # 一条巡检记录都没有（例如全部目标连接失败），发个简短说明而不是空报告
            summary = (
                f"【健康巡检报告】\n巡检计划：{job.name}\n\n"
                f"❌ 本次巡检没有取得任何主机的巡检结果，请检查设备连通性与凭据配置。"
            )
            pdf, filename = b"", ""
        else:
            summary = build_summary_text(data)
            try:
                pdf = build_inspection_pdf(data)
                filename = _report_filename(data)
            except Exception:
                # PDF 生成失败（缺 reportlab / 字体 / 脏数据）时仍发文字摘要，
                # 绝不能因为附件把整条报告吞掉。
                logger.exception("Failed to build inspection PDF for job %s", job.id)
                pdf, filename = b"", ""
    except Exception:
        logger.exception("Failed to build inspection report for job %s", job.id)
        return [
            {
                "ok": False,
                "name": "report-builder",
                "error": "报告生成失败，详见后端日志",
            }
        ]

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(4, len(webhooks))) as pool:
        futures = [
            pool.submit(_deliver_to_webhook, hook, summary, pdf, filename)
            for hook in webhooks
        ]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # 单个通道炸了不影响其它通道
                logger.exception("Inspection report delivery crashed")
                results.append(
                    {"ok": False, "name": "unknown", "error": str(exc)[:200]}
                )
    return results
