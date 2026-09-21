"""巡检报告 PDF 生成。

设计目标是「一眼看懂」，不是「信息全」：

  1. 结论横幅放在最前面 —— 绿色「未发现异常」或红色「N 台主机存在异常」，
     打开就知道要不要往下看；
  2. 有异常时，异常清单紧跟横幅，直接列出「哪台主机 / 什么异常 / 多严重」，
     不需要翻到明细里自己找；
  3. 全部主机概览表用于确认覆盖面（有没有漏掉某台机器没跑到）；
  4. 逐项明细**只展开异常主机**。健康主机在概览表里已经说明「全部正常」，
     再逐台铺 6 行明细只会把真正要看的东西挤到第 3 页去。

── 中文字体策略（重要）──

基础镜像 python:3.12-slim-bookworm 不带任何中文字体，而报告正文全是中文。
``_cjk_font()`` 按优先级找一个能用的字体：

  1. 环境变量 ``REPORT_PDF_FONT`` 指定的文件；
  2. 仓库/镜像里 ``app/static/fonts/`` 下的字体；
  3. 常见系统字体目录（Debian 镜像里 apt 装的那份）。

找到就以 ``TTFont`` 注册，**字形内嵌进 PDF**（实测 wqy-microhei.ttc 生成的
报告约 23KB，带 FontFile），任何阅读器都能渲染。一个都找不到时退回
reportlab 内置的 ``STSong-Light``（Adobe CID 字体）：它不占体积（约 2.5KB），
但**字体不内嵌**，依赖阅读器自带 CJK 支持 —— Chromium 系的 pdfium（飞书
桌面端预览用的就是它）不带 Adobe CJK 字体包，有渲染成空白方块的风险，
所以退回时会打 WARNING。

⚠ 字体选型踩过的坑（已在真实 Debian bookworm 镜像里实测）：

  * ``fonts-wqy-microhei`` → **可用**。TrueType(glyf) 轮廓。
  * ``fonts-noto-cjk`` → **不可用**。NotoSansCJK-*.ttc 是 PostScript/CFF 轮廓，
    reportlab 的 TTFont 只认 glyf，注册时直接报
    “TTC file ...: postscript outlines are not supported”。

  所以 Dockerfile 里装的是 wqy-microhei。不要把它“升级”成 Noto CJK。
另外 ``.ttc``（TrueType Collection）需要传 ``subfontIndex=0``，否则 reportlab
会把整个集合当成单个字体解析失败。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.utils import format_local_time

logger = logging.getLogger(__name__)

# 内置 CID 字体名（reportlab 自带，无需字体文件）
_CID_FONT = "STSong-Light"
# 允许通过环境变量指定内嵌字体文件
_FONT_ENV = "REPORT_PDF_FONT"
# 也支持把字体直接放进仓库/镜像的这个目录
_BUNDLED_FONT_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"
# 系统字体目录（Debian 镜像里 apt 装的字体落在这里）。
# 顺序即优先级：wqy-microhei 已实测可被 reportlab 内嵌，排在最前。
_SYSTEM_FONT_CANDIDATES = (
    # Linux / Docker（镜像已装 fonts-wqy-microhei）
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    # macOS 开发机自带中文字体：仅本地渲染用（系统已授权本机使用），不随仓库/镜像分发。
    # 没有它时开发环境会退回 CID 不内嵌字体，手机/部分阅读器度量错误、字挤在一起。
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

# 状态 → (中文, 主色, CSS 十六进制字串)。
# 不用 Color.hexval()：它返回 "0xdc2626"，而 Paragraph 的 <font color> 要 CSS 形式。
_STATUS_STYLE = {
    "normal": ("正常", colors.HexColor("#16a34a"), "#16a34a"),
    "warning": ("警告", colors.HexColor("#d97706"), "#d97706"),
    "critical": ("严重", colors.HexColor("#dc2626"), "#dc2626"),
    "error": ("错误", colors.HexColor("#6b7280"), "#6b7280"),
}
# 巡检记录整体状态
_RECORD_STATUS = {
    "completed": ("正常", "#16a34a"),
    "partial": ("部分异常", "#d97706"),
    "failed": ("失败", "#dc2626"),
    "running": ("运行中", "#2563eb"),
}

_FONT_NAME: str | None = None


@dataclass
class ReportItem:
    """单个巡检项的结果。"""

    label: str
    value: str | None
    unit: str | None = None
    status: str = "normal"
    error_message: str | None = None

    @property
    def is_problem(self) -> bool:
        return self.status in {"warning", "critical", "error"}

    @property
    def display(self) -> str:
        """数值 + 单位；失败项没有数值，显示占位符而不是空白。"""
        if not self.value:
            return "—"
        return f"{self.value}{self.unit or ''}"


@dataclass
class ReportHost:
    """一台主机的巡检结果。"""

    name: str
    ip: str | None = None
    target_type: str = "linux"
    # 操作系统短名(已经 inspection_report.short_os_name 归一化:'Windows Server' /
    # 'Ubuntu' / 'Debian' 等;为空时报告里回退显示 target_type 类型)。
    os_name: str | None = None
    status: str = "completed"
    duration_ms: int | None = None
    record_id: int | None = None
    items: list[ReportItem] = field(default_factory=list)

    @property
    def problems(self) -> list[ReportItem]:
        return [item for item in self.items if item.is_problem]

    @property
    def has_problem(self) -> bool:
        # 主机级失败(连不上/记录缺失/执行报错)也是异常:此时 items 为空,
        # 单靠 problems 会把失败主机漏算,导致横幅误报「全部正常」。
        # status=failed 直接算异常;completed/partial 再看逐项 problems。
        if self.status == "failed":
            return True
        return bool(self.problems)


@dataclass
class InspectionReportData:
    """一次巡检（可能多台主机）的报告数据。"""

    schedule_name: str | None = None
    triggered_at: datetime | None = None
    finished_at: datetime | None = None
    hosts: list[ReportHost] = field(default_factory=list)

    @property
    def problem_hosts(self) -> list[ReportHost]:
        return [host for host in self.hosts if host.has_problem]

    @property
    def has_anomaly(self) -> bool:
        return bool(self.problem_hosts)


def _find_font_file() -> Path | None:
    """定位可用于内嵌的中文字体文件（环境变量 → 仓库内置 → 系统字体）。"""
    env_path = (os.getenv(_FONT_ENV) or "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.is_file():
            return candidate
        logger.warning("REPORT_PDF_FONT 指向的文件不存在: %s", env_path)
    if _BUNDLED_FONT_DIR.is_dir():
        for suffix in ("*.ttf", "*.otf", "*.ttc"):
            for candidate in sorted(_BUNDLED_FONT_DIR.glob(suffix)):
                return candidate
    for raw in _SYSTEM_FONT_CANDIDATES:
        candidate = Path(raw)
        if candidate.is_file():
            return candidate
    return None


def _cjk_font() -> str:
    """返回可用的中文字体名（进程内只注册一次）。"""
    global _FONT_NAME
    if _FONT_NAME:
        return _FONT_NAME

    font_file = _find_font_file()
    if font_file is not None:
        try:
            # .ttc 是字体集合，必须用 subfontIndex 指定取哪一个；
            # 对普通 .ttf 传 0 也无害。
            pdfmetrics.registerFont(TTFont("DCNCJK", str(font_file), subfontIndex=0))
            _FONT_NAME = "DCNCJK"
            logger.info("巡检报告 PDF 使用内嵌中文字体: %s", font_file.name)
            return _FONT_NAME
        except Exception as exc:  # 字体损坏/CFF 轮廓不支持时退回 CID，别让报告发不出去
            logger.warning(
                "内嵌字体 %s 注册失败(%s)，退回 %s", font_file, exc, _CID_FONT
            )

    try:
        pdfmetrics.registerFont(UnicodeCIDFont(_CID_FONT))
    except Exception:
        logger.exception("注册 CID 中文字体失败，PDF 中文可能无法渲染")
    else:
        logger.warning(
            "未找到可内嵌的中文字体，巡检报告 PDF 退回 %s（字体不内嵌，"
            "Chromium 系阅读器可能显示空白）。建议设置 %s 指向一个 OFL 中文字体。",
            _CID_FONT,
            _FONT_ENV,
        )
    _FONT_NAME = _CID_FONT
    return _FONT_NAME


def _styles(font: str) -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle(
            "title",
            fontName=font,
            fontSize=18,
            leading=26,
            wordWrap="CJK",
            textColor=colors.HexColor("#111827"),
        ),
        "meta": ParagraphStyle(
            "meta",
            fontName=font,
            fontSize=9,
            leading=15,
            wordWrap="CJK",
            textColor=colors.HexColor("#6b7280"),
        ),
        "banner": ParagraphStyle(
            "banner",
            fontName=font,
            fontSize=13,
            leading=20,
            wordWrap="CJK",
            alignment=TA_CENTER,
            textColor=colors.white,
        ),
        "h2": ParagraphStyle(
            "h2",
            fontName=font,
            fontSize=12,
            leading=18,
            spaceBefore=6,
            spaceAfter=4,
            wordWrap="CJK",
            textColor=colors.HexColor("#111827"),
        ),
        # wordWrap="CJK" 必须：Paragraph 默认按空格断行，中文无空格，
        # 长句会在单元格/横幅里溢出不换行、挤在一起。
        "cell": ParagraphStyle(
            "cell", fontName=font, fontSize=9, leading=14, wordWrap="CJK"
        ),
        "cell_center": ParagraphStyle(
            "cell_center",
            fontName=font,
            fontSize=9,
            leading=14,
            wordWrap="CJK",
            alignment=TA_CENTER,
        ),
        "note": ParagraphStyle(
            "note",
            fontName=font,
            fontSize=8.5,
            leading=14,
            wordWrap="CJK",
            textColor=colors.HexColor("#6b7280"),
        ),
    }


def _table_style(font: str, header_bg: colors.Color) -> TableStyle:
    return TableStyle(
        [
            ("FONTNAME", (0, 0), (-1, -1), font),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("LEADING", (0, 0), (-1, -1), 13),
            ("BACKGROUND", (0, 0), (-1, 0), header_bg),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e5e7eb")),
            (
                "ROWBACKGROUNDS",
                (0, 1),
                (-1, -1),
                [colors.white, colors.HexColor("#f9fafb")],
            ),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]
    )


def _fmt_time(moment: datetime | None) -> str:
    if not moment:
        return "—"
    return format_local_time(moment)


def _esc(text: Any, limit: int | None = None) -> str:
    """先截断、后转义，给 Paragraph 用。

    reportlab 的 Paragraph 把文本当 **XML 标记** 解析，而真实错误信息里到处是
    尖括号——WinRM 超时就产生
    ``<HTTPConnection(host='192.168.1.126', port=5985) at 0x10d607a10>``。
    不转义就报 “paraparser: syntax error: unclosed tags”，整份报告生成失败。

    顺序必须是先截断再转义：反过来会把 ``&amp;`` 切成 ``&am``，同样触发解析错误。
    """
    raw = "" if text is None else str(text)
    if limit is not None and len(raw) > limit:
        raw = f"{raw[:limit]}…"
    return escape(raw)


def _fmt_duration(ms: int | None) -> str:
    if ms is None:
        return "—"
    return f"{ms / 1000:.1f}s"


def _fit_font_size(
    text: str, font_name: str, base_size: float, max_width: float, floor: float = 7.0
) -> float:
    """按需缩字号，让 text 单行放进 max_width（列宽-左右 padding）。

    「系统」列的短 OS 名（'Windows Server' 等）必须一行显示：
    wordWrap="CJK" 会把放不下的行从任意字符处折断，实测 9pt 下
    'Windows Server' 在 wqy 里宽 65pt，超出 26mm 列的可用 63.5pt，
    尾部的 r 被折到第二行。缩到能放下为止（不低于 floor）。
    stringWidth 对 CID 回退字体（开发环境）可能不可靠，失败时返回
    原字号——退化为换行，仅影响开发环境预览。
    """
    size = base_size
    try:
        while (
            size > floor and pdfmetrics.stringWidth(text, font_name, size) > max_width
        ):
            size -= 0.5
    except Exception:
        return base_size
    return size


def _banner(data: InspectionReportData, styles: dict) -> Table:
    """结论横幅：整份报告最先被看到的东西。"""
    if data.has_anomaly:
        text = f"本次巡检发现 {len(data.problem_hosts)} 台主机存在异常"
        bg = colors.HexColor("#dc2626")
    else:
        text = f"本次巡检未发现异常（{len(data.hosts)} 台主机全部正常）"
        bg = colors.HexColor("#16a34a")
    table = Table([[Paragraph(text, styles["banner"])]], colWidths=[170 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROUNDEDCORNERS", [3, 3, 3, 3]),
            ]
        )
    )
    return table


def _problem_table(data: InspectionReportData, styles: dict) -> Table:
    """异常清单：哪台主机、什么异常、多严重。"""
    rows = [
        [
            Paragraph("主机", styles["cell_center"]),
            Paragraph("IP", styles["cell_center"]),
            Paragraph("异常项", styles["cell_center"]),
            Paragraph("实测", styles["cell_center"]),
            Paragraph("级别", styles["cell_center"]),
        ]
    ]
    for host in data.problem_hosts:
        # 主机级失败(未跑出任何 item):单独列一行「连接/执行失败」,否则明细表
        # 会漏掉这台——概览表显示失败、明细表却查无此机。
        if not host.items:
            rows.append(
                [
                    Paragraph(_esc(host.name, 40), styles["cell"]),
                    Paragraph(_esc(host.ip or "—", 45), styles["cell"]),
                    Paragraph("主机巡检失败：未能连接或执行巡检命令", styles["cell"]),
                    Paragraph("—", styles["cell_center"]),
                    Paragraph(
                        '<font color="#dc2626">失败</font>', styles["cell_center"]
                    ),
                ]
            )
            continue
        for item in host.problems:
            label, _color, hexcolor = _STATUS_STYLE.get(
                item.status, _STATUS_STYLE["error"]
            )
            detail = item.error_message or item.display
            rows.append(
                [
                    Paragraph(_esc(host.name, 40), styles["cell"]),
                    Paragraph(_esc(host.ip or "—", 45), styles["cell"]),
                    Paragraph(
                        f"{_esc(item.label, 20)}：{_esc(detail, 150)}", styles["cell"]
                    ),
                    Paragraph(_esc(item.display, 20), styles["cell_center"]),
                    Paragraph(
                        f'<font color="{hexcolor}">{label}</font>',
                        styles["cell_center"],
                    ),
                ]
            )
    table = Table(
        rows, colWidths=[34 * mm, 30 * mm, 66 * mm, 20 * mm, 20 * mm], repeatRows=1
    )
    table.setStyle(_table_style(styles["cell"].fontName, colors.HexColor("#dc2626")))
    return table


def _overview_table(data: InspectionReportData, styles: dict) -> Table:
    """全部主机概览：确认覆盖面，健康主机不展开明细。"""
    rows = [
        [
            Paragraph("主机", styles["cell_center"]),
            Paragraph("IP", styles["cell_center"]),
            Paragraph("系统", styles["cell_center"]),
            Paragraph("结论", styles["cell_center"]),
            Paragraph("正常", styles["cell_center"]),
            Paragraph("异常", styles["cell_center"]),
            Paragraph("耗时", styles["cell_center"]),
        ]
    ]
    for host in data.hosts:
        label, hexcolor = _RECORD_STATUS.get(host.status, ("未知", "#6b7280"))
        problems = len(host.problems)
        normal = max(len(host.items) - problems, 0)
        # 系统短名缩字号单行显示：9pt 放不下('Windows Server' 65pt >
        # 26mm 列可用 63.5pt)会把尾部字符折到第二行，很难看。
        os_text = (
            _esc(host.os_name, 30).replace(" ", "\xa0")
            if host.os_name
            else ("Windows" if host.target_type == "windows" else "Linux")
        )
        os_size = _fit_font_size(
            os_text,
            styles["cell_center"].fontName,
            styles["cell_center"].fontSize,
            26 * mm - 10,  # 列宽 - 左右 padding(5+5pt)
        )
        os_style = (
            styles["cell_center"].clone("cell_center_os", fontSize=os_size)
            if os_size < styles["cell_center"].fontSize
            else styles["cell_center"]
        )
        rows.append(
            [
                Paragraph(_esc(host.name, 40), styles["cell"]),
                Paragraph(_esc(host.ip or "—", 45), styles["cell"]),
                Paragraph(os_text, os_style),
                Paragraph(
                    f'<font color="{hexcolor}">{label}</font>', styles["cell_center"]
                ),
                Paragraph(str(normal), styles["cell_center"]),
                Paragraph(
                    f'<font color="#dc2626">{problems}</font>' if problems else "0",
                    styles["cell_center"],
                ),
                Paragraph(_fmt_duration(host.duration_ms), styles["cell_center"]),
            ]
        )
    table = Table(
        rows,
        # 「系统」列 26mm：短名放不下的按 _fit_font_size 缩字号单行显示。
        colWidths=[34 * mm, 28 * mm, 26 * mm, 22 * mm, 16 * mm, 16 * mm, 18 * mm],
        repeatRows=1,
    )
    table.setStyle(_table_style(styles["cell"].fontName, colors.HexColor("#374151")))
    return table


def build_inspection_pdf(data: InspectionReportData) -> bytes:
    """生成巡检报告 PDF 字节流。"""
    import io

    font = _cjk_font()
    styles = _styles(font)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=16 * mm,
        title="数据中心健康巡检报告",
        author="DCN",
    )

    story: list = []
    story.append(Paragraph("数据中心健康巡检报告", styles["title"]))
    meta_bits = [
        f"生成时间：{_fmt_time(data.finished_at or datetime.now(timezone.utc))}"
    ]
    if data.schedule_name:
        meta_bits.append(f"巡检计划：{_esc(data.schedule_name, 60)}")
    if data.triggered_at:
        meta_bits.append(f"开始时间：{_fmt_time(data.triggered_at)}")
    meta_bits.append(f"覆盖主机：{len(data.hosts)} 台")
    story.append(Paragraph("　|　".join(meta_bits), styles["meta"]))
    story.append(Spacer(1, 5 * mm))

    story.append(_banner(data, styles))
    story.append(Spacer(1, 5 * mm))

    if data.has_anomaly:
        story.append(Paragraph("异常清单", styles["h2"]))
        story.append(_problem_table(data, styles))
        story.append(Spacer(1, 4 * mm))

    story.append(Paragraph("全部主机概览", styles["h2"]))
    story.append(_overview_table(data, styles))

    story.append(Spacer(1, 3 * mm))
    story.append(
        Paragraph(
            "本报告由 DCN 数据中心网络管理平台自动生成。巡检为只读取证，"
            "不会对设备做任何变更。",
            styles["note"],
        )
    )

    doc.build(story)
    return buffer.getvalue()
