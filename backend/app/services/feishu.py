"""飞书自建应用消息通道:把告警直接私聊发给指定的人。

三种飞书通道的区别(都在 webhooks.provider 上区分):
  * ``feishu``     — 多维表格/审批工作流的 Webhook，消息落到表格再靠流程转发;
  * ``feishu_bot`` — 群自定义机器人，只能发到它所在的群;
  * ``feishu_app`` — 本模块:企业自建应用用 app_id/app_secret 换 tenant_access_token,
                     再调 ``im/v1/messages`` 投递到任意 receive_id，因此可以给
                     "某个人"发单聊(open_id / user_id / union_id / email)，
                     也可以发到指定群(chat_id)。

安全边界与其它出站集成一致:base URL 过 validate_outbound_url、禁止重定向;
app_secret 复用 ``webhooks.secret_enc`` 的 Fernet 加密存储，不进日志。
接收人一次一个调用(飞书没有面向单聊的批量接口)，token 过期自动刷新一次。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

from app.utils import format_local_time
from app.validators import validate_outbound_url

logger = logging.getLogger(__name__)

FEISHU_DEFAULT_BASE = "https://open.feishu.cn"
LARK_BASE_HINT = "海外 Lark 用 https://open.larksuite.com"
# im/v1/messages 支持的 receive_id_type;email 最省事，不必额外申请通讯录权限
RECEIVE_ID_TYPES = ("open_id", "user_id", "union_id", "email", "chat_id")
# 界面上不再让运维挑 ID 类型:auto = 按接收人字符串的形态逐个自动识别
AUTO_RECEIVE_ID_TYPE = "auto"
STYLES = ("card", "text")

_TOKEN_ENDPOINT = "/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_ENDPOINT = "/open-apis/im/v1/messages"
# 上传文件拿 file_key，再用 msg_type=file 发送（巡检报告 PDF 走这条路）
_FILE_ENDPOINT = "/open-apis/im/v1/files"
# im/v1/files 支持的 file_type
FILE_TYPES = ("opus", "mp4", "pdf", "doc", "xls", "ppt", "stream")
# 飞书单文件上限 30MB；巡检报告远小于此，超限基本意味着生成逻辑出错
_MAX_FILE_BYTES = 30 * 1024 * 1024
# 通讯录只读接口:让告警通道直接从企业通讯录里挑人，不必手抄 open_id
_DEPARTMENT_ENDPOINT = "/open-apis/contact/v3/departments"
_USER_ENDPOINT = "/open-apis/contact/v3/users/find_by_department"
# 企业只授权了「指定部门 + 指定人」时，读根部门必然 40004;先查授权范围再批量取详情
_SCOPE_ENDPOINT = "/open-apis/contact/v3/scopes"
_DEPARTMENT_BATCH_ENDPOINT = "/open-apis/contact/v3/departments/batch"
_USER_BATCH_ENDPOINT = "/open-apis/contact/v3/users/batch"
# 提前 5 分钟过期，避免用着刚好失效的 token
_TOKEN_SAFETY_MARGIN = 300
# 飞书的 token 失效类业务码，命中就刷新后重试一次
_TOKEN_ERROR_CODES = {99991661, 99991663, 99991668}
# 40004 = 被查询的部门不在应用的「通讯录权限范围」内(查根部门要求范围为全部成员)
_SCOPE_ERROR_CODES = {40004}
_MAX_RECEIVERS = 50
_ANALYSIS_MAX_CHARS = 2000
# 飞书通讯录分页单页上限
MAX_PAGE_SIZE = 50
_DEPARTMENT_ID_RE = re.compile(r"^[0-9A-Za-z_\-]{1,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# 飞书各类 ID 都有固定前缀，据此推断该用哪个 receive_id_type
_ID_PREFIX_TYPES = (("ou_", "open_id"), ("on_", "union_id"), ("oc_", "chat_id"))

_SEVERITY_COLOR = {"critical": "red", "warning": "orange", "info": "blue"}
_SEVERITY_LABEL = {"critical": "严重", "warning": "警告", "info": "信息"}
_EVENT_LABEL = {
    "alert.created": "告警触发",
    "alert.resolved": "告警恢复",
    "alert.remediation": "自动处置进度",
    "alert.analysis": "AI 归因结论",
    "automation.notify": "自动化通知",
}
_PCT_METRICS = {"cpu_pct", "mem_pct", "disk_max_pct"}
# 归因类指标:与 app.services.remediation.ANALYSIS_METRICS 同源的渲染子集
# (不含 container_status,见下)。cpu/mem/disk 的卡片详情段只显示 AI 归因
# 结论,不铺基础文案(字段区已有规则/对象/指标/当前值,基础文案纯冗余);
# 没结论(未配置运维接入/归因未完成/失败)则不渲染详情段。
# container_status 例外(2026-09-17 用户定调):容器告警的基础消息携带
# 状态明细("Exited (0) About a minute ago")与自动处置话术,字段区表达
# 不了——没结论时照常铺详情段;有结论时升级为 AI 归因块(不重铺基础)。
# 此处复制一份避免 services 循环导入,与 _STATE_VALUE_LABELS 同一先例。
_ANALYSIS_METRICS = {
    "cpu_pct",
    "mem_pct",
    "disk_max_pct",
}
# 状态类指标内部用 1=异常、0=正常，卡片上直接显示数字没人看得懂
_STATE_VALUE_LABELS = {
    "host_status": ("离线", "在线"),
    "container_status": ("异常", "正常"),
    "business_status": ("异常", "正常"),
}

_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


class FeishuError(RuntimeError):
    """飞书接口不可达或返回业务错误;code 用于判断是不是 token 过期。"""

    def __init__(
        self, message: str, *, code: int | None = None, status: int | None = None
    ):
        super().__init__(message)
        self.code = code
        self.status = status


def is_scope_error(exc: FeishuError) -> bool:
    """是不是「通讯录权限范围没覆盖所查部门」这类错误(飞书 40004 no dept authority)。"""
    return exc.code in _SCOPE_ERROR_CODES or "no dept authority" in str(exc).lower()


# ── 配置解析 ──


def normalize_base(base: str | None) -> str:
    """开放平台域名，允许私有化部署改写;顺带完成 SSRF 校验。"""
    value = (base or "").strip().rstrip("/") or FEISHU_DEFAULT_BASE
    return validate_outbound_url(value)


def parse_receivers(raw: Any) -> list[str]:
    """接收人支持数组，也支持逗号/分号/空白/换行分隔的一串。"""
    if isinstance(raw, str):
        items: list[Any] = re.split(r"[,;，；\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = []
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if value and value not in result:
            result.append(value)
    return result[:_MAX_RECEIVERS]


def normalize_receive_id_type(value: Any) -> str:
    """显式指定的 ID 类型;空值一律当 auto(自动识别)。"""
    candidate = str(value or AUTO_RECEIVE_ID_TYPE).strip() or AUTO_RECEIVE_ID_TYPE
    if candidate not in (*RECEIVE_ID_TYPES, AUTO_RECEIVE_ID_TYPE):
        raise FeishuError(
            f"receive_id_type 只能是 {AUTO_RECEIVE_ID_TYPE}(自动识别) 或 {' / '.join(RECEIVE_ID_TYPES)}"
        )
    return candidate


def coerce_receive_id_type(value: Any) -> str:
    """落库用的宽松版:非法值不抛错，退回 auto。"""
    candidate = str(value or "").strip()
    return candidate if candidate in RECEIVE_ID_TYPES else AUTO_RECEIVE_ID_TYPE


def infer_receive_id_type(receive_id: str) -> str:
    """从接收人字符串本身判断 ID 类型:邮箱 / ou_ / on_ / oc_ / 其余按 user_id。"""
    text = str(receive_id or "").strip()
    if _EMAIL_RE.match(text):
        return "email"
    for prefix, kind in _ID_PREFIX_TYPES:
        if text.startswith(prefix):
            return kind
    return "user_id"


def resolve_receive_id_type(configured: Any, receive_id: str) -> str:
    """投递时逐个接收人求值:auto 走推断，显式类型则整条通道沿用。"""
    kind = normalize_receive_id_type(configured)
    return infer_receive_id_type(receive_id) if kind == AUTO_RECEIVE_ID_TYPE else kind


def normalize_style(value: Any) -> str:
    candidate = str(value or "card").strip()
    return candidate if candidate in STYLES else "card"


def normalize_department_id(value: Any) -> str:
    """部门 ID 进查询串前先过白名单字符;"0" 表示根部门。"""
    candidate = str(value or "0").strip() or "0"
    if not _DEPARTMENT_ID_RE.match(candidate):
        raise FeishuError("部门 ID 不合法")
    return candidate


def normalize_page_size(value: Any) -> int:
    try:
        size = int(value or MAX_PAGE_SIZE)
    except (TypeError, ValueError):
        size = MAX_PAGE_SIZE
    return max(1, min(size, MAX_PAGE_SIZE))


def receiver_of(user: dict, receive_id_type: str) -> str:
    """按通道配置的 ID 类型，从通讯录用户里取出该用的那个 receive_id。"""
    kind = receive_id_type or AUTO_RECEIVE_ID_TYPE
    # 自动识别时优先 open_id:通讯录接口一定返回，且不依赖邮箱是否对外可见
    if kind == AUTO_RECEIVE_ID_TYPE:
        kind = "open_id"
    if kind == "email":
        return str(user.get("enterprise_email") or user.get("email") or "")
    if kind in ("user_id", "union_id", "open_id"):
        return str(user.get(kind) or "")
    return ""


# ── tenant_access_token(进程内缓存) ──


def invalidate_token(base: str, app_id: str) -> None:
    with _token_lock:
        _token_cache.pop(f"{base}|{app_id}", None)


def _json_or_fail(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise FeishuError(
            f"飞书响应不是合法 JSON(HTTP {response.status_code}): {response.text[:160]}",
            status=response.status_code,
        ) from exc
    return data if isinstance(data, dict) else {}


def tenant_access_token(
    base: str, app_id: str, app_secret: str, timeout: int = 8
) -> str:
    """取(或复用缓存的)tenant_access_token。"""
    key = f"{base}|{app_id}"
    deadline = time.monotonic()
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and cached[1] > deadline:
            return cached[0]
    try:
        response = requests.post(
            f"{base}{_TOKEN_ENDPOINT}",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise FeishuError(f"飞书开放平台连接失败: {exc}") from exc
    data = _json_or_fail(response)
    code = data.get("code")
    token = data.get("tenant_access_token")
    if response.status_code != 200 or (code not in (0, None)) or not token:
        raise FeishuError(
            f"获取 tenant_access_token 失败: [{code}] {data.get('msg') or response.text[:160]}",
            code=code if isinstance(code, int) else None,
            status=response.status_code,
        )
    expire = data.get("expire")
    ttl = max(60, int(expire or 7200) - _TOKEN_SAFETY_MARGIN)
    with _token_lock:
        _token_cache[key] = (str(token), time.monotonic() + ttl)
    return str(token)


# ── 消息内容 ──


def _value_label(alert: dict, *, resolved: bool = False) -> str:
    """卡片上的「当前值」。

    性能指标显示百分比与阈值;状态类指标(主机/容器/业务)翻译成中文。事件里存的
    是触发时的值，恢复时不会回写 0，所以恢复通知一律显示正常态。
    """
    metric = str(alert.get("metric") or "")
    value, threshold = alert.get("value"), alert.get("threshold")
    labels = _STATE_VALUE_LABELS.get(metric)
    if labels:
        bad, good = labels
        if resolved:
            return good
        try:
            return bad if float(value) >= 1 else good
        except (TypeError, ValueError):
            return "-"
    if metric in _PCT_METRICS and value is not None:
        return f"{value}%（阈值 {threshold}%）"
    if value is None:
        return "-"
    return f"{value}（阈值 {threshold}）"


def _field(label: str, value: Any) -> dict:
    return {
        "is_short": True,
        "text": {"tag": "lark_md", "content": f"**{label}**\n{value or '-'}"},
    }


def _block(label: str, value: str) -> dict:
    return {
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"},
    }


def build_text(payload: dict) -> str:
    """纯文本样式:飞书会把换行原样展示，适合转发到其它系统。"""
    alert = payload.get("alert") or {}
    device = payload.get("device") or {}
    event = payload.get("event") or ""
    resolved = alert.get("status") == "resolved" or event == "alert.resolved"
    severity = _SEVERITY_LABEL.get(
        alert.get("severity"), alert.get("severity") or "警告"
    )
    lines = [
        f"[{severity}] {alert.get('rule_name') or 'DCN 告警'}",
        f"对象：{device.get('name') or '-'}（{device.get('ip_address') or '-'}）",
    ]
    detail = (payload.get("remediation") or {}).get("detail")
    if detail:
        lines.append(f"自动处置：{detail}")
    analysis = payload.get("analysis") or {}
    has_analysis = bool(analysis.get("state") == "completed" and analysis.get("text"))
    metric = alert.get("metric") or ""
    conclusion_style = metric in _ANALYSIS_METRICS or (
        metric == "container_status" and has_analysis
    )
    if conclusion_style:
        # 归因类告警:详情段就是归因结论,不铺基础文案(卡片口径一致)
        if has_analysis:
            lines.append(f"AI 归因：{str(analysis['text'])[:_ANALYSIS_MAX_CHARS]}")
    elif event == "alert.remediation" or (
        resolved and (detail or (alert.get("metric") or "") == "business_status")
    ):
        # 进度卡/有处置结论的恢复卡/业务恢复卡不铺历史消息,处置结论由
        # 「自动处置」行携带(业务的异常明细是触发时刻快照,恢复后过时)。
        pass
    else:
        lines.append(f"内容：{alert.get('message') or payload.get('message') or '-'}")
    lines.append(
        f"事件：{_EVENT_LABEL.get(event, event)} · "
        f"{format_local_time(alert.get('last_seen_at') or payload.get('timestamp'))}"
    )
    return "\n".join(lines)


def build_card(payload: dict) -> dict:
    """交互式卡片:标题色随级别变化，恢复变绿，正文分块展示处置与归因。"""
    alert = payload.get("alert") or {}
    device = payload.get("device") or {}
    event = payload.get("event") or ""
    severity = alert.get("severity") or "warning"
    resolved = alert.get("status") == "resolved" or event == "alert.resolved"
    color = "green" if resolved else _SEVERITY_COLOR.get(severity, "orange")
    title = f"[{_SEVERITY_LABEL.get(severity, severity)}] {alert.get('rule_name') or 'DCN 告警'}"
    elements: list[dict] = [
        {
            "tag": "div",
            "fields": [
                _field("对象", device.get("name")),
                _field("地址", device.get("ip_address")),
                _field("指标", alert.get("metric_label") or alert.get("metric")),
                _field("当前值", _value_label(alert, resolved=resolved)),
                _field("事件", _EVENT_LABEL.get(event, event)),
                _field(
                    "时间",
                    format_local_time(
                        alert.get("last_seen_at") or payload.get("timestamp")
                    ),
                ),
            ],
        }
    ]
    analysis = payload.get("analysis") or {}
    analysis_text = (
        str(analysis["text"])[:_ANALYSIS_MAX_CHARS]
        if analysis.get("state") == "completed" and analysis.get("text")
        else None
    )
    metric = alert.get("metric") or ""
    conclusion_style = metric in _ANALYSIS_METRICS or (
        metric == "container_status" and bool(analysis_text)
    )
    if conclusion_style:
        # 归因类告警:整生命周期一张卡,详情段就是归因结论本身;
        # 没结论(虚拟机未配置运维接入/归因未完成)不渲染详情段。
        # 容器告警(非 _ANALYSIS_METRICS)仅在结论已出时走这条;没结论时
        # 回到基础消息——状态明细与自动处置话术字段区表达不了。
        if analysis_text:
            elements.append({"tag": "hr"})
            elements.append(_block("AI 归因", analysis_text))
    else:
        # 自动处置进度卡/已恢复卡只展示处置结论本身:详情块是整段历史消息
        # (离线→拉起→结果层层叠加;恢复卡上的基础文案也已过时),有处置结论时
        # 不再渲染,避免同一张卡上「详情」与「自动处置」重复铺同样的话。
        # 业务状态恢复卡是例外(2026-09-18 用户定调):它没有自动处置,消息里
        # 的"异常明细"是触发时刻快照,恢复后原样重铺过时且误导——一律不渲染。
        remediation_detail = (payload.get("remediation") or {}).get("detail")
        skip_detail = event == "alert.remediation" or (
            resolved
            and (remediation_detail or (alert.get("metric") or "") == "business_status")
        )
        message = (
            None if skip_detail else (alert.get("message") or payload.get("message"))
        )
        if message:
            elements.append({"tag": "hr"})
            elements.append(_block("详情", str(message)))
    detail = (payload.get("remediation") or {}).get("detail")
    if detail:
        elements.append(_block("自动处置", str(detail)))
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "DCN 数据中心运维平台"}],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def build_content(payload: dict, style: str) -> tuple[str, str]:
    """返回 (msg_type, content);飞书要求 content 是 JSON 字符串。"""
    if style == "text":
        return "text", json.dumps({"text": build_text(payload)}, ensure_ascii=False)
    return "interactive", json.dumps(build_card(payload), ensure_ascii=False)


def sample_payload() -> dict:
    """「测试发送」用的示例告警，结构与真实 payload 一致。"""
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    return {
        "event": "alert.created",
        "alert": {
            "id": 0,
            "rule_id": 0,
            "rule_name": "Webhook 测试告警：CPU 高负载",
            "severity": "critical",
            "status": "open",
            "metric": "cpu_pct",
            "metric_label": "CPU 使用率",
            "value": 92.6,
            "threshold": 90.0,
            "message": "Webhook 测试告警：示例服务器 CPU 使用率 92.6% 已超过阈值 90.0%",
            "first_triggered_at": stamp,
            "last_seen_at": stamp,
            "resolved_at": None,
        },
        "device": {
            "id": 0,
            "name": "示例服务器-01",
            "ip_address": "192.0.2.10",
            "type": "server",
        },
        "resource": {"type": "device", "id": "0", "name": "示例服务器-01"},
        "remediation": {"state": "", "state_label": "未处置", "detail": None},
        "analysis": {"state": "", "state_label": "未分析", "text": None},
        "timestamp": stamp,
        "source": "DCN",
    }


# ── 通讯录(只读) ──


def _get(base: str, token: str, path: str, params: dict, timeout: int = 8) -> dict:
    """飞书只读 GET;业务码非 0 一律抛 FeishuError，让上层翻译成中文提示。"""
    try:
        response = requests.get(
            f"{base}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise FeishuError(f"飞书接口连接失败: {exc}") from exc
    data = _json_or_fail(response)
    code = data.get("code")
    if response.status_code != 200 or code not in (0, None):
        raise FeishuError(
            f"[{code}] {data.get('msg') or f'HTTP {response.status_code}'}",
            code=code if isinstance(code, int) else None,
            status=response.status_code,
        )
    payload = data.get("data")
    return payload if isinstance(payload, dict) else {}


def _items(data: dict, mapper) -> list[dict]:
    """data.items 过一遍 mapper，丢掉映射不出主键的脏数据。"""
    mapped = (
        mapper(item) for item in (data.get("items") or []) if isinstance(item, dict)
    )
    return [item for item in mapped if item]


def _display_name(item: dict, fallback: str) -> str:
    """取显示名。飞书按字段级权限裁剪返回体:没有 contact:user.base:readonly
    (部门对应 contact:department.base:readonly)时，name 字段整个不返回。

    这时依次退回英文名/别名、邮箱前缀，最后退回 ID，至少让运维能分辨出不同的人。
    """
    for key in ("name", "en_name", "nickname"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    email = str(item.get("enterprise_email") or item.get("email") or "").strip()
    return email.split("@", 1)[0] if "@" in email else fallback


def _name_scope_missing(data: dict, items: list[dict]) -> bool:
    """有内容却一个真实 name 都读不到 = 缺字段级权限，界面要提示去补。"""
    if not items:
        return False
    raw = [item for item in (data.get("items") or []) if isinstance(item, dict)]
    return not any(str(item.get("name") or "").strip() for item in raw)


def _batch(data: dict, mapper) -> dict:
    """批量接口(无分页)的结果，同样带上名称字段权限标记。"""
    items = _items(data, mapper)
    return {"items": items, "name_scope_missing": _name_scope_missing(data, items)}


def _id_list(raw: Any) -> list[str]:
    """飞书返回的 ID 数组:去空去重，保持原顺序。"""
    candidates = [raw] if isinstance(raw, str) else list(raw or [])
    result: list[str] = []
    for item in candidates:
        value = str(item or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def _page(data: dict, mapper) -> dict:
    items = _items(data, mapper)
    return {
        "items": items,
        "page_token": str(data.get("page_token") or ""),
        "has_more": bool(data.get("has_more")),
        "name_scope_missing": _name_scope_missing(data, items),
    }


def _department_view(item: dict) -> dict:
    department_id = str(
        item.get("open_department_id") or item.get("department_id") or ""
    )
    if not department_id:
        return {}
    return {
        "open_department_id": department_id,
        "name": _display_name(item, department_id),
        "parent_department_id": str(item.get("parent_department_id") or ""),
    }


def _user_view(item: dict) -> dict:
    open_id = str(item.get("open_id") or "")
    if not open_id:
        return {}
    avatar = item.get("avatar") or {}
    # 手机号属于敏感信息，选人场景用不到，直接不下发
    return {
        "open_id": open_id,
        "user_id": str(item.get("user_id") or ""),
        "union_id": str(item.get("union_id") or ""),
        "name": _display_name(item, open_id),
        "email": str(item.get("email") or ""),
        "enterprise_email": str(item.get("enterprise_email") or ""),
        "avatar": str(avatar.get("avatar_72") or "")
        if isinstance(avatar, dict)
        else "",
        "department_ids": [str(d) for d in (item.get("department_ids") or [])],
    }


def list_departments(
    *,
    base: str | None,
    app_id: str,
    app_secret: str,
    parent_department_id: Any = "0",
    page_token: str = "",
    page_size: Any = MAX_PAGE_SIZE,
    timeout: int = 8,
) -> dict:
    """列出某个部门下的子部门;parent_department_id="0" 表示根部门。"""
    host = normalize_base(base)
    token = tenant_access_token(host, app_id, app_secret, timeout)
    data = _get(
        host,
        token,
        _DEPARTMENT_ENDPOINT,
        {
            "parent_department_id": normalize_department_id(parent_department_id),
            "fetch_child": "false",
            "department_id_type": "open_department_id",
            "page_size": normalize_page_size(page_size),
            "page_token": str(page_token or ""),
        },
        timeout,
    )
    return _page(data, _department_view)


def list_users(
    *,
    base: str | None,
    app_id: str,
    app_secret: str,
    department_id: Any = "0",
    page_token: str = "",
    page_size: Any = MAX_PAGE_SIZE,
    timeout: int = 8,
) -> dict:
    """列出直属某个部门的成员(含 open_id / user_id / 邮箱，供选择接收人)。"""
    host = normalize_base(base)
    token = tenant_access_token(host, app_id, app_secret, timeout)
    data = _get(
        host,
        token,
        _USER_ENDPOINT,
        {
            "department_id": normalize_department_id(department_id),
            "user_id_type": "open_id",
            "page_size": normalize_page_size(page_size),
            "page_token": str(page_token or ""),
        },
        timeout,
    )
    return _page(data, _user_view)


def list_authorized_scope(
    *,
    base: str | None,
    app_id: str,
    app_secret: str,
    page_token: str = "",
    page_size: Any = MAX_PAGE_SIZE,
    timeout: int = 8,
) -> dict:
    """查应用被授予的通讯录范围(部门 ID + 成员 open_id)。

    全员授权时飞书返回根部门下的一级部门与直属成员;部分授权时只返回被勾选的
    部门(不含其子部门与成员)和被勾选的成员，正好用来兜底 40004。
    """
    host = normalize_base(base)
    token = tenant_access_token(host, app_id, app_secret, timeout)
    data = _get(
        host,
        token,
        _SCOPE_ENDPOINT,
        {
            "department_id_type": "open_department_id",
            "user_id_type": "open_id",
            "page_size": normalize_page_size(page_size),
            "page_token": str(page_token or ""),
        },
        timeout,
    )
    return {
        "department_ids": _id_list(data.get("department_ids")),
        "user_ids": _id_list(data.get("user_ids")),
        "page_token": str(data.get("page_token") or ""),
        "has_more": bool(data.get("has_more")),
    }


def get_departments_by_ids(
    *,
    base: str | None,
    app_id: str,
    app_secret: str,
    department_ids: Any,
    timeout: int = 8,
) -> dict:
    """批量把部门 ID 换成部门信息(名称)，单次上限 50。

    返回 ``{"items": [...], "name_scope_missing": bool}``。
    """
    ids = _id_list(department_ids)[:MAX_PAGE_SIZE]
    if not ids:
        return {"items": [], "name_scope_missing": False}
    host = normalize_base(base)
    token = tenant_access_token(host, app_id, app_secret, timeout)
    data = _get(
        host,
        token,
        _DEPARTMENT_BATCH_ENDPOINT,
        {
            # requests 会把列表展开成重复的同名查询参数，正是批量接口要的形式
            "department_ids": ids,
            "department_id_type": "open_department_id",
            "user_id_type": "open_id",
        },
        timeout,
    )
    return _batch(data, _department_view)


def get_users_by_ids(
    *,
    base: str | None,
    app_id: str,
    app_secret: str,
    user_ids: Any,
    timeout: int = 8,
) -> dict:
    """批量把 open_id 换成成员信息(授权范围里「指定人」的场景)，单次上限 50。

    返回 ``{"items": [...], "name_scope_missing": bool}``。
    """
    ids = _id_list(user_ids)[:MAX_PAGE_SIZE]
    if not ids:
        return {"items": [], "name_scope_missing": False}
    host = normalize_base(base)
    token = tenant_access_token(host, app_id, app_secret, timeout)
    data = _get(
        host,
        token,
        _USER_BATCH_ENDPOINT,
        {
            "user_ids": ids,
            "user_id_type": "open_id",
            "department_id_type": "open_department_id",
        },
        timeout,
    )
    return _batch(data, _user_view)


# ── 投递 ──


def _send_one(
    base: str,
    token: str,
    receive_id_type: str,
    receive_id: str,
    msg_type: str,
    content: str,
    timeout: int,
) -> dict:
    url = f"{base}{_MESSAGE_ENDPOINT}?receive_id_type={receive_id_type}"
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"receive_id": receive_id, "msg_type": msg_type, "content": content},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise FeishuError(f"飞书接口连接失败: {exc}") from exc
    data = _json_or_fail(response)
    code = data.get("code")
    if response.status_code != 200 or code not in (0, None):
        raise FeishuError(
            f"[{code}] {data.get('msg') or f'HTTP {response.status_code}'}",
            code=code if isinstance(code, int) else None,
            status=response.status_code,
        )
    result = data.get("data")
    return result if isinstance(result, dict) else {}


def upload_file(
    base: str,
    token: str,
    file_bytes: bytes,
    file_name: str,
    *,
    file_type: str = "pdf",
    timeout: int = 20,
) -> str:
    """上传文件到飞书，返回 ``file_key``（发 ``msg_type=file`` 消息时要用）。

    需要应用具备 ``im:resource`` 权限；权限缺失时飞书会返回权限类错误码，
    这里原样透出错误文本，便于在 webhook 测试结果里直接看到原因。

    只有 ``feishu_app``（自建应用）能上传文件；群自定义机器人的 webhook
    只接受 text/post/image/interactive，**根本没有上传接口**，所以 PDF 附件
    这条路只对自建应用开放。
    """
    if file_type not in FILE_TYPES:
        raise FeishuError(f"不支持的飞书文件类型: {file_type}")
    if not file_bytes:
        raise FeishuError("上传内容为空")
    if len(file_bytes) > _MAX_FILE_BYTES:
        raise FeishuError(f"文件超过飞书 30MB 上限({len(file_bytes)} bytes)")

    mime = "application/pdf" if file_type == "pdf" else "application/octet-stream"
    try:
        response = requests.post(
            f"{base}{_FILE_ENDPOINT}",
            # 不要手动设 Content-Type：requests 需要自己生成 multipart boundary
            headers={"Authorization": f"Bearer {token}"},
            data={"file_type": file_type, "file_name": file_name},
            files={"file": (file_name, file_bytes, mime)},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise FeishuError(f"飞书文件上传连接失败: {exc}") from exc

    data = _json_or_fail(response)
    code = data.get("code")
    if response.status_code != 200 or code not in (0, None):
        raise FeishuError(
            f"文件上传失败 [{code}] {data.get('msg') or f'HTTP {response.status_code}'}",
            code=code if isinstance(code, int) else None,
            status=response.status_code,
        )
    file_key = (data.get("data") or {}).get("file_key")
    if not file_key:
        raise FeishuError("飞书未返回 file_key")
    return str(file_key)


def deliver_text(
    summary_text: str,
    *,
    base: str | None,
    app_id: str | None,
    app_secret: str | None,
    receivers: Any,
    receive_id_type: Any = AUTO_RECEIVE_ID_TYPE,
    timeout: int = 8,
) -> dict:
    """只发一条纯文本摘要（不附文件）。

    与 ``deliver_report`` 的摘要阶段同构，抽出来是为了让「PDF 生成失败」
    时仍能单独把文字结论发出去——摘要和附件不该同生共死。
    """
    host = normalize_base(base)
    if not (app_id or "").strip() or not (app_secret or "").strip():
        raise FeishuError("缺少飞书自建应用的 App ID / App Secret")
    ids = parse_receivers(receivers)
    if not ids:
        raise FeishuError("没有配置飞书接收人")
    id_type = normalize_receive_id_type(receive_id_type)
    if not (summary_text or "").strip():
        raise FeishuError("报告摘要为空")

    token = tenant_access_token(host, app_id.strip(), app_secret.strip(), timeout)
    content = json.dumps({"text": summary_text.strip()}, ensure_ascii=False)
    failures: list[str] = []
    message_ids: list[str] = []
    refreshed = False
    for receive_id in ids:
        target_type = (
            infer_receive_id_type(receive_id)
            if id_type == AUTO_RECEIVE_ID_TYPE
            else id_type
        )
        try:
            data = _send_one(
                host, token, target_type, receive_id, "text", content, timeout
            )
        except FeishuError as exc:
            if refreshed or exc.code not in _TOKEN_ERROR_CODES:
                failures.append(f"{receive_id}: {exc}")
                continue
            refreshed = True
            invalidate_token(host, app_id.strip())
            try:
                token = tenant_access_token(
                    host, app_id.strip(), app_secret.strip(), timeout
                )
                data = _send_one(
                    host, token, target_type, receive_id, "text", content, timeout
                )
            except FeishuError as retry_exc:
                failures.append(f"{receive_id}: {retry_exc}")
                continue
        message_id = str(data.get("message_id") or "")
        if message_id:
            message_ids.append(message_id)
    ok = not failures
    if not ok:
        logger.warning(
            "Feishu text delivery partial failure: %s", "; ".join(failures)[:400]
        )
    return {
        "ok": ok,
        "status_code": 200 if ok else 400,
        "error": None if ok else "; ".join(failures)[:400],
        "receivers": len(ids),
        "delivered": len(message_ids),
        "message_ids": message_ids,
    }


def deliver_report(
    summary_text: str,
    file_bytes: bytes,
    file_name: str,
    *,
    base: str | None,
    app_id: str | None,
    app_secret: str | None,
    receivers: Any,
    receive_id_type: Any = AUTO_RECEIVE_ID_TYPE,
    timeout: int = 8,
) -> dict:
    """先发摘要文本，再把 PDF 当附件发出去。

    不复用 ``deliver()``：``build_card`` / ``build_text`` 都是**告警形状**的
    （读 ``payload["alert"]`` / ``payload["device"]``），巡检报告硬套进去会渲染成
    「[警告] DCN 告警」外加一行无意义的「对象：-（-）」。报告摘要由调用方拼好，
    这里直接以 ``msg_type=text`` 发出，语义才准。

    顺序是有意的：用户先看到「有没有异常 / 哪台主机什么异常」的文字结论，
    需要细节时再打开附件。文件只上传一次（file_key 与 app 绑定，可发给多个接收人）。

    摘要一个都没送达时不再上传文件：人都没通知到，附件也没意义，
    而且这样能把错误定位在第一步。**部分**接收人失败（如 230013
    "Bot has NO availability to this user"，不在应用可用范围）不拖垮其它人：
    摘要已送达的照常收附件，失败者记录在返回结果的 error 里——
    一个坏接收人吞掉所有人的 PDF 附件，比部分送达更难排查。
    """
    host = normalize_base(base)
    if not (app_id or "").strip() or not (app_secret or "").strip():
        raise FeishuError("缺少飞书自建应用的 App ID / App Secret")
    ids = parse_receivers(receivers)
    if not ids:
        raise FeishuError("没有配置飞书接收人")
    id_type = normalize_receive_id_type(receive_id_type)
    if not (summary_text or "").strip():
        raise FeishuError("报告摘要为空")

    app_id_s, app_secret_s = app_id.strip(), app_secret.strip()
    token = tenant_access_token(host, app_id_s, app_secret_s, timeout)

    def targets() -> list[tuple[str, str]]:
        return [
            (
                rid,
                infer_receive_id_type(rid)
                if id_type == AUTO_RECEIVE_ID_TYPE
                else id_type,
            )
            for rid in ids
        ]

    def send_with_retry(
        msg_type: str, content: str, *, pairs: list[tuple[str, str]] | None = None
    ) -> tuple[int, list[str], list[str], list[str]]:
        """逐个接收人发送；token 失效只刷新重试一次。

        返回 (成功数, message_ids, 失败描述, 成功接收人)。``pairs`` 缺省发全部；
        发附件阶段只传摘要已送达的接收人。
        """
        nonlocal token
        ok = 0
        message_ids: list[str] = []
        failures: list[str] = []
        delivered_to: list[str] = []
        refreshed = False
        for receive_id, target_type in pairs if pairs is not None else targets():
            try:
                data = _send_one(
                    host, token, target_type, receive_id, msg_type, content, timeout
                )
            except FeishuError as exc:
                if refreshed or exc.code not in _TOKEN_ERROR_CODES:
                    failures.append(f"{receive_id}: {exc}")
                    continue
                refreshed = True
                invalidate_token(host, app_id_s)
                try:
                    token = tenant_access_token(host, app_id_s, app_secret_s, timeout)
                    data = _send_one(
                        host, token, target_type, receive_id, msg_type, content, timeout
                    )
                except FeishuError as retry_exc:
                    failures.append(f"{receive_id}: {retry_exc}")
                    continue
            ok += 1
            delivered_to.append(receive_id)
            message_id = str(data.get("message_id") or "")
            if message_id:
                message_ids.append(message_id)
        return ok, message_ids, failures, delivered_to

    # 1) 摘要消息
    summary_text = summary_text.strip()
    _ok, summary_ids, summary_failures, summary_receivers = send_with_retry(
        "text", json.dumps({"text": summary_text}, ensure_ascii=False)
    )
    summary_error = "; ".join(summary_failures)[:400]
    if summary_failures:
        # 部分失败不拖垮附件（见 docstring）：送达的人照常收 PDF。
        logger.warning("Feishu report summary partial failure: %s", summary_error)
    if not summary_ids:
        # 一个都没送达：人都没通知到，附件也没意义，
        # 而且这样能把错误定位在第一步。
        logger.warning("Feishu report summary failed: %s", summary_error)
        return _report_result(
            ids,
            summary_ids,
            file_ok=False,
            error=summary_error,
            file_error="摘要消息未送达，已跳过附件",
        )

    # 2) 上传一次 PDF
    try:
        file_key = upload_file(host, token, file_bytes, file_name, timeout=timeout + 12)
    except FeishuError as exc:
        if exc.code not in _TOKEN_ERROR_CODES:
            return _report_result(
                ids, summary_ids, file_ok=False, file_error=str(exc)[:400]
            )
        invalidate_token(host, app_id_s)
        token = tenant_access_token(host, app_id_s, app_secret_s, timeout)
        try:
            file_key = upload_file(
                host, token, file_bytes, file_name, timeout=timeout + 12
            )
        except FeishuError as retry_exc:
            return _report_result(
                ids, summary_ids, file_ok=False, file_error=str(retry_exc)[:400]
            )

    # 3) 逐个接收人发 file 消息——只发摘要已送达的人：摘要都到不了的
    #    接收人（不在应用可用范围等）file 消息同样到不了，重发只会重复
    #    同一个失败并拖慢整轮投递。
    summary_set = set(summary_receivers)
    file_ok, _file_ids, file_failures, _ = send_with_retry(
        "file",
        json.dumps({"file_key": file_key}),
        pairs=[t for t in targets() if t[0] in summary_set],
    )
    if file_failures:
        logger.warning(
            "Feishu report attachment partial failure: %s",
            "; ".join(file_failures)[:400],
        )
    return _report_result(
        ids,
        summary_ids,
        file_ok=not file_failures,
        error=summary_error or None,
        file_error=None if not file_failures else "; ".join(file_failures)[:400],
        file_key=file_key,
        file_delivered=file_ok,
    )


def _report_result(
    ids: list[str],
    summary_ids: list[str],
    *,
    file_ok: bool,
    error: str | None = None,
    file_error: str | None = None,
    file_key: str | None = None,
    file_delivered: int = 0,
) -> dict:
    """汇总报告投递结果，形状与 ``deliver()`` 对齐，便于上层统一处理。"""
    result = {
        "ok": file_ok,
        "status_code": 200 if file_ok else 400,
        "error": error or file_error,
        "receivers": len(ids),
        "delivered": len(summary_ids),
        "message_ids": summary_ids,
        "file_ok": file_ok,
        "file_delivered": file_delivered,
    }
    if file_error:
        result["file_error"] = file_error
    if file_key:
        result["file_key"] = file_key
    return result


def deliver(
    payload: dict,
    *,
    base: str | None,
    app_id: str | None,
    app_secret: str | None,
    receivers: Any,
    receive_id_type: Any = AUTO_RECEIVE_ID_TYPE,
    style: Any = "card",
    timeout: int = 8,
) -> dict:
    """把一条告警逐个投递给配置的接收人，返回聚合结果。"""
    host = normalize_base(base)
    if not (app_id or "").strip() or not (app_secret or "").strip():
        raise FeishuError("缺少飞书自建应用的 App ID / App Secret")
    ids = parse_receivers(receivers)
    if not ids:
        raise FeishuError("没有配置飞书接收人")
    id_type = normalize_receive_id_type(receive_id_type)
    msg_type, content = build_content(payload, normalize_style(style))

    token = tenant_access_token(host, app_id.strip(), app_secret.strip(), timeout)
    failures: list[str] = []
    message_ids: list[str] = []
    refreshed = False
    for receive_id in ids:
        target_type = (
            infer_receive_id_type(receive_id)
            if id_type == AUTO_RECEIVE_ID_TYPE
            else id_type
        )
        try:
            data = _send_one(
                host, token, target_type, receive_id, msg_type, content, timeout
            )
        except FeishuError as exc:
            if refreshed or exc.code not in _TOKEN_ERROR_CODES:
                failures.append(f"{receive_id}: {exc}")
                continue
            # token 刚好失效:刷新后重发这一次，后续接收人复用新 token
            refreshed = True
            invalidate_token(host, app_id.strip())
            try:
                token = tenant_access_token(
                    host, app_id.strip(), app_secret.strip(), timeout
                )
                data = _send_one(
                    host, token, target_type, receive_id, msg_type, content, timeout
                )
            except FeishuError as retry_exc:
                failures.append(f"{receive_id}: {retry_exc}")
                continue
        message_id = str(data.get("message_id") or "")
        if message_id:
            message_ids.append(message_id)
    ok = not failures
    if not ok:
        logger.warning(
            "Feishu app delivery partial failure: %s", "; ".join(failures)[:400]
        )
    return {
        "ok": ok,
        "status_code": 200 if ok else 400,
        "error": None if ok else "; ".join(failures)[:400],
        "receivers": len(ids),
        "delivered": len(message_ids),
        "message_ids": message_ids,
    }
