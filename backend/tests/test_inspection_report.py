"""巡检报告：摘要文案、PDF 生成与投递分发。

摘要文案是需求的核心 ——「先是总体说明是无任何异常还是有异常，如果有异常
就直接说明哪台主机的什么异常」，所以这里按真实场景逐条钉住。
"""

from datetime import datetime, timezone

import pytest

from app.services.inspection_report import (
    REPORT_EVENT,
    build_summary_text,
    short_os_name,
)
from app.services.report_pdf import (
    InspectionReportData,
    ReportHost,
    ReportItem,
    build_inspection_pdf,
)

# 2026-09-10 02:00 UTC = 10:00 Asia/Shanghai
FINISHED = datetime(2026, 9, 10, 2, 0, tzinfo=timezone.utc)


def _healthy_host(name="DB-01", ip="192.168.1.20"):
    return ReportHost(
        name=name,
        ip=ip,
        target_type="linux",
        status="completed",
        duration_ms=9100,
        items=[
            ReportItem("CPU 使用率", "12.5", "%", "normal"),
            ReportItem("内存使用", "43.2", "%", "normal"),
        ],
    )


def _bad_host(name="SRM-APP", ip="192.168.1.126"):
    return ReportHost(
        name=name,
        ip=ip,
        target_type="windows",
        status="partial",
        duration_ms=18200,
        items=[
            ReportItem("CPU 使用率", "25.0", "%", "normal"),
            ReportItem("磁盘使用", "96.2", "%", "critical"),
            ReportItem("服务异常", "1", "个", "warning"),
        ],
    )


def _data(hosts, name="每日健康巡检"):
    return InspectionReportData(
        schedule_name=name, triggered_at=FINISHED, finished_at=FINISHED, hosts=hosts
    )


# ── 摘要文案 ──


def test_summary_says_no_anomaly_when_all_healthy():
    text = build_summary_text(_data([_healthy_host(), _healthy_host("DB-02")]))
    assert "未发现异常" in text
    assert "2 台主机全部正常" in text
    # 无异常时不该出现任何主机名，否则消息变成噪音
    assert "DB-01" not in text


def test_summary_names_the_host_and_the_anomaly():
    """核心需求：有异常时直接说明哪台主机的什么异常。"""
    text = build_summary_text(_data([_healthy_host(), _bad_host()]))
    assert "发现 1 台主机存在异常" in text
    assert "SRM-APP" in text
    assert "192.168.1.126" in text
    assert "磁盘使用" in text
    assert "96.2%" in text
    assert "严重" in text


def test_summary_lists_every_problem_of_a_host():
    text = build_summary_text(_data([_bad_host()]))
    assert "磁盘使用" in text
    assert "服务异常" in text
    # 健康主机不进摘要
    assert "CPU 使用率" not in text


def test_summary_covers_multiple_bad_hosts():
    text = build_summary_text(_data([_bad_host(), _bad_host("WEB-01", "192.168.1.30")]))
    assert "发现 2 台主机存在异常" in text
    assert "SRM-APP" in text and "WEB-01" in text


def test_summary_truncates_long_problem_lists():
    """一台机器十几项异常时不能把消息糊成一片。"""
    host = ReportHost(
        name="NOISY",
        ip="10.0.0.1",
        items=[ReportItem(f"项{i}", "99", "%", "critical") for i in range(8)],
    )
    text = build_summary_text(_data([host]))
    assert "等 5 项" in text
    # 只列前 3 项
    assert "项0" in text and "项2" in text
    assert "项3" not in text


def test_summary_uses_error_message_for_failed_items():
    """取证失败的项没有数值，要显示原因而不是空的 —。"""
    host = ReportHost(
        name="SRM",
        ip="192.168.1.126",
        items=[ReportItem("磁盘使用", None, "%", "error", "WinRM 连接超时")],
    )
    text = build_summary_text(_data([host]))
    assert "WinRM 连接超时" in text


def test_summary_mentions_the_attachment():
    assert "PDF" in build_summary_text(_data([_healthy_host()]))


def test_summary_includes_schedule_and_time():
    text = build_summary_text(_data([_healthy_host()], name="工作日巡检"))
    assert "工作日巡检" in text
    assert "完成时间" in text
    assert "覆盖主机：1 台" in text


# ── PDF ──


def test_pdf_is_generated_for_healthy_run():
    pdf = build_inspection_pdf(_data([_healthy_host()]))
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1000


def test_pdf_is_generated_for_anomalous_run():
    pdf = build_inspection_pdf(_data([_healthy_host(), _bad_host()]))
    assert pdf.startswith(b"%PDF-")


def test_pdf_embeds_cjk_font_when_available(monkeypatch):
    """有可用字体时必须内嵌，否则 Chromium 系阅读器会显示空白方块。

    生产镜像靠 apt 装的 wqy-microhei（已在 bookworm 容器里实测 embedded=True）。
    开发机上那些路径不存在，所以退而在本机搜一个中文字体、用 REPORT_PDF_FONT
    指过去——测的是「找到字体就内嵌」这个机制本身，而不是某个特定字体。
    """
    import glob

    from app.services import report_pdf

    font_file = report_pdf._find_font_file()
    if font_file is None:
        # 开发机：找一个能用的中文字体来验证内嵌路径
        patterns = (
            "/System/Library/Fonts/Supplemental/Songti.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/usr/share/fonts/**/*.ttc",
            "/usr/share/fonts/**/*.ttf",
        )
        for pattern in patterns:
            matches = sorted(glob.glob(pattern, recursive=True))
            if matches:
                font_file = matches[0]
                monkeypatch.setenv("REPORT_PDF_FONT", str(font_file))
                break
    if font_file is None:
        pytest.skip("本机找不到任何可用于验证的中文字体")

    # _FONT_NAME 是进程级缓存，不清掉就会沿用上一个用例注册的字体
    monkeypatch.setattr(report_pdf, "_FONT_NAME", None)
    pdf = build_inspection_pdf(_data([_bad_host()]))
    assert b"FontFile" in pdf, f"使用 {font_file} 时字体未内嵌"
    # 用完恢复缓存，避免影响其它用例
    monkeypatch.setattr(report_pdf, "_FONT_NAME", None)


def test_report_item_display_falls_back_to_dash():
    """失败项没有数值时显示占位符，不能渲染成空白单元格。"""
    assert ReportItem("磁盘", None, "%", "error").display == "—"
    assert ReportItem("磁盘", "", "%", "error").display == "—"
    assert ReportItem("CPU", "12.5", "%", "normal").display == "12.5%"
    assert ReportItem("服务", "1", None, "warning").display == "1"


def test_host_problems_excludes_normal_items():
    host = _bad_host()
    assert [i.label for i in host.problems] == ["磁盘使用", "服务异常"]
    assert _healthy_host().problems == []


def test_empty_report_has_no_anomaly():
    data = _data([])
    assert data.has_anomaly is False
    # 0 台主机也要能出 PDF，不能抛异常
    assert build_inspection_pdf(data).startswith(b"%PDF-")


# ── 事件名契约 ──


def test_report_event_name_is_stable():
    """前端勾选项、后端标签表、派发处三边必须用同一个字符串。"""
    assert REPORT_EVENT == "automation.notify"

    from app.services.device_events import EVENT_AUTOMATION_NOTIFY
    from app.services.feishu import _EVENT_LABEL

    # 巡检报告与巡检失败/任务失败已合并为同一个「自动化通知」事件
    assert REPORT_EVENT == EVENT_AUTOMATION_NOTIFY
    assert REPORT_EVENT in _EVENT_LABEL


# ── 真实数据踩出来的坑 ──

# WinRM 超时的真实 stderr：Python 的 repr 里带尖括号，reportlab 的 Paragraph
# 把文本当 XML 解析，不转义就抛 "paraparser: syntax error: unclosed tags"，
# 整份报告生成失败。单元测试最初用的是干净数据，没抓到这个。
_WINRM_ERROR = (
    "无法连接 WinRM (192.168.1.126:5985): HTTPConnectionPool(host='192.168.1.126', "
    "port=5985): Max retries exceeded with url: /wsman (Caused by "
    "ConnectTimeoutError(<HTTPConnection(host='192.168.1.126', port=5985) at "
    "0x10d607a10>, 'Connection to 192.168.1.126 timed out. (connect timeout=25)'))"
)


def test_pdf_survives_angle_brackets_in_error_message():
    """回归：错误信息里的 <> 必须被转义，否则 paraparser 直接报错。"""
    host = ReportHost(
        name="SRM<test>",
        ip="192.168.1.126",
        status="failed",
        items=[
            ReportItem("磁盘使用", None, "%", "error", _WINRM_ERROR),
            ReportItem("CPU 使用率", None, "%", "error", _WINRM_ERROR),
        ],
    )
    pdf = build_inspection_pdf(_data([host]))
    assert pdf.startswith(b"%PDF-")


def test_pdf_survives_ampersand_in_text():
    """& 同样要转义（&amp; 被截断成 &am 也会炸）。"""
    host = ReportHost(
        name="A&B 服务器",
        items=[ReportItem("服务异常", None, None, "error", "x & y <z> 失败")],
    )
    assert build_inspection_pdf(_data([host])).startswith(b"%PDF-")


def test_summary_collapses_repeated_identical_failures():
    """整机连不上时六项会全部 error 且原因相同，合并成一句而不是六行堆栈。"""
    host = ReportHost(
        name="SRM",
        ip="192.168.1.126",
        status="failed",
        items=[
            ReportItem(label, None, "%", "error", _WINRM_ERROR)
            for label in (
                "CPU 使用率",
                "内存使用",
                "磁盘使用",
                "运行时间",
                "服务异常",
                "事件日志",
            )
        ],
    )
    text = build_summary_text(_data([host]))
    assert "6 项取证失败" in text
    assert "无法连接 WinRM" in text
    # 六行重复的堆栈不该出现
    assert text.count("HTTPConnectionPool") <= 1


def test_summary_reason_is_single_line_and_truncated():
    """异常堆栈压成一行，不能把飞书消息糊满。"""
    host = ReportHost(
        name="SRM",
        items=[ReportItem("磁盘使用", None, "%", "error", _WINRM_ERROR)],
    )
    text = build_summary_text(_data([host]))
    body = [line for line in text.splitlines() if line.startswith("· ")][0]
    assert "\n" not in body
    assert len(body) < 160


def test_summary_separates_real_thresholds_from_collection_failures():
    """真超标与取证失败要分开表述，不能混成一锅。"""
    host = ReportHost(
        name="SRM",
        items=[
            ReportItem("磁盘使用", "96.2", "%", "critical"),
            ReportItem("CPU 使用率", None, "%", "error", "连接超时"),
        ],
    )
    text = build_summary_text(_data([host]))
    assert "磁盘使用 96.2%（严重）" in text
    assert "取证失败" in text


# ── short_os_name：报告「系统」列只显示发行版短名 ──


@pytest.mark.parametrize(
    ("raw", "ttype", "expected"),
    [
        # 虚拟机 binding 里的全长名 → 短名
        ("Microsoft Windows Server 2022 Datacenter", "windows", "Windows Server"),
        ("Microsoft Windows Server 2019 Datacenter", "windows", "Windows Server"),
        ("Microsoft Windows 11 Pro", "windows", "Windows"),
        ("Windows 10 企业版", "windows", "Windows"),
        ("Debian GNU/Linux 12 (bookworm)", "linux", "Debian"),
        ("Ubuntu 22.04.3 LTS", "linux", "Ubuntu"),
        ("Ubuntu 3ubuntu0.17", "linux", "Ubuntu"),  # 内核版本串也能识别
        ("CentOS Linux 7 (Core)", "linux", "CentOS"),
        ("Rocky Linux 9.2", "linux", "Rocky Linux"),
        ("AlmaLinux 9.2", "linux", "AlmaLinux"),
        ("Red Hat Enterprise Linux 8.8", "linux", "RHEL"),
        ("openEuler 22.03 LTS", "linux", "openEuler"),
        ("Fedora Linux 38", "linux", "Fedora"),
        # 粗粒度/缺失值按 target_type 回退
        ("linux", "linux", "Linux"),
        ("windows", "windows", "Windows"),
        ("unknown", "linux", "Linux"),
        (None, "linux", "Linux"),
        (None, "windows", "Windows"),
        ("", "windows", "Windows"),
        # 未识别的发行版不硬猜，回退族类
        ("Gentoo Linux", "linux", "Linux"),
    ],
)
def test_short_os_name(raw, ttype, expected):
    assert short_os_name(raw, ttype) == expected
