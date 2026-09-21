"""Webhook configuration and connectivity test API."""

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.models.webhook import Webhook
from app.schemas.webhook import (
    FeishuDepartmentPage,
    FeishuDirectoryRequest,
    FeishuUserPage,
    WebhookCreate,
    WebhookResponse,
    WebhookTestResponse,
    WebhookUpdate,
)
from app.services import feishu
from app.services.alerts import SEVERITY_LABELS
from app.services.crypto import decrypt, encrypt
from app.services.feishu import FeishuError
from app.services.permissions import require_permission
from app.utils import format_local_time
from app.validators import validate_outbound_url

logger = logging.getLogger(__name__)


def _test_body(row: Webhook) -> bytes:
    now = datetime.now(timezone.utc)
    sample = {
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
            "first_triggered_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
            "resolved_at": None,
        },
        "device": {
            "id": 0,
            "name": "示例服务器-01",
            "ip_address": "192.0.2.10",
            "type": "server",
        },
        "timestamp": now.isoformat(),
        "source": "DCN",
    }
    sample.update(
        {
            "alert_id": 0,
            "rule_id": 0,
            "rule_name": "Webhook 测试告警：CPU 高负载",
            "severity": "critical",
            "status": "open",
            "metric": "cpu_pct",
            "metric_label": "CPU 使用率",
            "value": 92.6,
            "threshold": 90.0,
            "message": "Webhook 测试告警：示例服务器 CPU 使用率 92.6% 已超过阈值 90.0%",
            "device_id": 0,
            "device_name": "示例服务器-01",
            "device_ip": "192.0.2.10",
            "device_type": "server",
        }
    )
    if row.provider == "feishu":
        return json.dumps(
            {
                "device_name": sample["device"]["name"],
                "device_ip": sample["device"]["ip_address"],
                "severity": SEVERITY_LABELS.get(
                    sample["alert"]["severity"], sample["alert"]["severity"]
                ),
                "alert_time": format_local_time(sample["alert"]["last_seen_at"]),
                "alert_content": sample["alert"]["message"],
            },
            ensure_ascii=False,
        ).encode("utf-8")
    if row.provider == "feishu_bot":
        timestamp = str(int(time.time()))
        secret = decrypt(row.secret_enc or "")
        body = {
            "msg_type": "text",
            "content": {
                "text": (
                    "[CRITICAL] DCN Webhook 测试告警\n"
                    "规则：Webhook 测试告警：CPU 高负载\n"
                    "设备：示例服务器-01 (192.0.2.10)\n"
                    "指标：CPU 使用率\n"
                    "当前值：92.6%\n"
                    "阈值：90.0%\n"
                    "状态：未恢复\n"
                    "事件：alert.created\n"
                    "这是一条完整的告警样例，用于验证飞书工作流字段映射。"
                )
            },
        }
        if secret:
            signing = f"{timestamp}\n{secret}".encode("utf-8")
            body["timestamp"] = timestamp
            body["sign"] = base64.b64encode(
                hmac.new(secret.encode("utf-8"), signing, hashlib.sha256).digest()
            ).decode("utf-8")
        return json.dumps(body, ensure_ascii=False).encode("utf-8")
    return json.dumps(sample, ensure_ascii=False).encode("utf-8")


router = APIRouter(tags=["webhooks"])


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_webhook_opener = build_opener(_NoRedirect)


def _view(row: Webhook) -> dict:
    secret = decrypt(row.secret_enc or "")
    return {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "provider": row.provider,
        "events": row.events or [],
        "headers": row.headers or {},
        "config": row.config or {},
        "enabled": bool(row.enabled),
        "secret_set": bool(secret),
        "secret_preview": f"****{secret[-4:]}" if secret else "",
        "last_test_at": row.last_test_at,
        "last_test_status": row.last_test_status,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


# 接收人姓名只用于界面回显，长度与数量都收一收，别把通讯录整份塞进 config
_MAX_NAME_LEN = 64


def _sanitized_receiver_names(raw: Any, receivers: list[str]) -> dict[str, str]:
    """接收人 ID → 通讯录姓名的映射;只留仍在接收人列表里的条目。"""
    if not isinstance(raw, dict):
        return {}
    allowed = set(receivers)
    names: dict[str, str] = {}
    for key, value in raw.items():
        receiver = str(key or "").strip()
        name = str(value or "").strip()[:_MAX_NAME_LEN]
        if receiver in allowed and name:
            names[receiver] = name
    return names


def _sanitized_config(provider: str, config: dict | None) -> dict:
    """只保留该 provider 真正会读的键，避免往库里塞任意 JSON。"""
    raw = config or {}
    if provider != "feishu_app":
        return {}
    receivers = feishu.parse_receivers(raw.get("receivers"))
    return {
        "app_id": str(raw.get("app_id") or "").strip()[:128],
        # 默认 auto:投递时按每个接收人的形态自动识别 ID 类型
        "receive_id_type": feishu.coerce_receive_id_type(raw.get("receive_id_type")),
        "receivers": receivers,
        "receiver_names": _sanitized_receiver_names(
            raw.get("receiver_names"), receivers
        ),
        "style": feishu.normalize_style(raw.get("style")),
    }


def _require_provider_config(
    provider: str, config: dict | None, has_secret: bool
) -> None:
    """飞书私聊通道的必填项在保存时就校验，别等到告警真来了才失败。"""
    if provider != "feishu_app":
        return
    cfg = config or {}
    if not str(cfg.get("app_id") or "").strip():
        raise HTTPException(status_code=422, detail="飞书自建应用需要填写 App ID")
    if not feishu.parse_receivers(cfg.get("receivers")):
        raise HTTPException(
            status_code=422,
            detail="请至少选择一个飞书接收人(通讯录选人，或手填邮箱 / open_id / chat_id)",
        )
    try:
        feishu.normalize_receive_id_type(cfg.get("receive_id_type"))
    except FeishuError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not has_secret:
        raise HTTPException(status_code=422, detail="飞书自建应用需要填写 App Secret")


def _deliver_test_feishu_app(row: Webhook, db: Session) -> WebhookTestResponse:
    """测试发送走真实投递链路，能同时验证 App 凭据与接收人是否可用。"""
    config = row.config or {}
    started = time.monotonic()
    status_code: int | None = None
    preview: str | None = None
    try:
        result = feishu.deliver(
            feishu.sample_payload(),
            base=row.url,
            app_id=config.get("app_id"),
            app_secret=decrypt(row.secret_enc or ""),
            receivers=config.get("receivers"),
            receive_id_type=config.get("receive_id_type")
            or feishu.AUTO_RECEIVE_ID_TYPE,
            style=config.get("style", "card"),
        )
        ok = bool(result["ok"])
        status_code = result["status_code"]
        message = (
            f"已发送给 {result['delivered']}/{result['receivers']} 位飞书接收人"
            if ok
            else f"飞书发送失败：{result.get('error')}"
        )
        preview = json.dumps(
            {"message_ids": result.get("message_ids")}, ensure_ascii=False
        )[:500]
    except (FeishuError, ValueError) as exc:
        ok = False
        status_code = exc.status if isinstance(exc, FeishuError) else None
        message = str(exc)
    duration_ms = int((time.monotonic() - started) * 1000)
    row.last_test_at = datetime.now(timezone.utc)
    row.last_test_status = "success" if ok else "failed"
    db.commit()
    return WebhookTestResponse(
        ok=ok,
        status_code=status_code,
        message=message,
        duration_ms=duration_ms,
        response_preview=preview,
    )


@router.get("/api/webhooks", response_model=list[WebhookResponse])
def list_webhooks(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("settings:manage")),
):
    return [
        _view(row)
        for row in db.query(Webhook).order_by(Webhook.created_at.desc()).all()
    ]


def _feishu_credentials(
    body: FeishuDirectoryRequest, db: Session
) -> tuple[str, str, str]:
    """解析通讯录请求要用的 (base, app_id, app_secret)。

    编辑已保存通道时允许 Secret 留空，回落到库里加密存的那份;新建未保存时
    必须现填。base 同样过 validate_outbound_url，防止拿这个接口做内网探测。
    """
    base = (body.base or "").strip()
    app_id = (body.app_id or "").strip()
    secret = (body.app_secret or "").strip()
    if body.webhook_id:
        row = db.query(Webhook).filter(Webhook.id == body.webhook_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Webhook 不存在")
        config = row.config or {}
        base = base or row.url
        app_id = app_id or str(config.get("app_id") or "")
        secret = secret or decrypt(row.secret_enc or "")
    if not app_id or not secret:
        raise HTTPException(
            status_code=422, detail="请先填写飞书应用的 App ID 与 App Secret"
        )
    try:
        return feishu.normalize_base(base), app_id, secret
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _directory_hint(exc: FeishuError) -> str:
    """把飞书通讯录的权限类错误翻译成可操作的中文提示。"""
    text = str(exc)
    if feishu.is_scope_error(exc):
        return (
            f"{text}；飞书只允许应用读取「通讯录权限范围」内的部门，"
            "而且连授权范围本身也读不到。"
            "请让企业管理员登录飞书管理后台(admin.feishu.cn) → 工作台 → 应用管理 → "
            "选中该应用 → 应用权限 → 通讯录设置，确认已勾选目标部门/成员；"
            "并在飞书开放平台为该应用开通 contact:contact.base:readonly 后重新发布版本"
        )
    if (
        exc.code in (99991672, 99991661, 99991663, 99991668)
        or "permission" in text.lower()
    ):
        return (
            f"{text}；请在飞书开放平台为应用申请「获取通讯录基本信息」"
            "只读权限，并在「通讯录授权范围」里包含目标部门"
        )
    return text


def _scoped_departments(
    base: str, app_id: str, secret: str, body: FeishuDirectoryRequest
) -> dict:
    """40004 兜底:把应用被授予的部门当作「通讯录」根层级列出来。

    飞书规定只有权限范围是「全部成员」时才能读根部门;企业通常只授权指定部门 +
    指定人，这时改用 /contact/v3/scopes 拿授权范围，再批量换成部门名称。
    """
    if feishu.normalize_department_id(body.department_id) != "0":
        # 下钻进未授权的子部门:不算错误，返回空列表，右侧成员照常显示
        return {"items": [], "page_token": "", "has_more": False, "scope_limited": True}
    scope = feishu.list_authorized_scope(
        base=base, app_id=app_id, app_secret=secret, page_token=body.page_token
    )
    batch = feishu.get_departments_by_ids(
        base=base,
        app_id=app_id,
        app_secret=secret,
        department_ids=scope["department_ids"],
    )
    return {
        "items": batch["items"],
        "page_token": scope["page_token"],
        "has_more": scope["has_more"],
        "scope_limited": True,
        "name_scope_missing": batch["name_scope_missing"],
    }


def _scoped_users(
    base: str, app_id: str, secret: str, body: FeishuDirectoryRequest
) -> dict:
    """40004 兜底:根层级显示授权范围里「指定人」，未授权的子部门返回空列表。"""
    if feishu.normalize_department_id(body.department_id) != "0":
        return {"items": [], "page_token": "", "has_more": False, "scope_limited": True}
    scope = feishu.list_authorized_scope(
        base=base, app_id=app_id, app_secret=secret, page_token=body.page_token
    )
    batch = feishu.get_users_by_ids(
        base=base, app_id=app_id, app_secret=secret, user_ids=scope["user_ids"]
    )
    return {
        "items": batch["items"],
        "page_token": scope["page_token"],
        "has_more": scope["has_more"],
        "scope_limited": True,
        "name_scope_missing": batch["name_scope_missing"],
    }


@router.post("/api/webhooks/feishu/departments", response_model=FeishuDepartmentPage)
def feishu_departments(
    body: FeishuDirectoryRequest,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("settings:manage")),
):
    """列出飞书部门(department_id="0" 为根部门)，供告警接收人选择。"""
    base, app_id, secret = _feishu_credentials(body, db)
    try:
        return feishu.list_departments(
            base=base,
            app_id=app_id,
            app_secret=secret,
            parent_department_id=body.department_id,
            page_token=body.page_token,
            page_size=body.page_size,
        )
    except FeishuError as exc:
        if feishu.is_scope_error(exc):
            try:
                return _scoped_departments(base, app_id, secret, body)
            except FeishuError:
                logger.warning("按通讯录授权范围兜底部门失败，回落原始错误: %s", exc)
        raise HTTPException(status_code=502, detail=_directory_hint(exc)) from exc


@router.post("/api/webhooks/feishu/users", response_model=FeishuUserPage)
def feishu_users(
    body: FeishuDirectoryRequest,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("settings:manage")),
):
    """列出某个飞书部门下的直属成员(含 open_id / user_id / 邮箱)。"""
    base, app_id, secret = _feishu_credentials(body, db)
    try:
        return feishu.list_users(
            base=base,
            app_id=app_id,
            app_secret=secret,
            department_id=body.department_id,
            page_token=body.page_token,
            page_size=body.page_size,
        )
    except FeishuError as exc:
        if feishu.is_scope_error(exc):
            try:
                return _scoped_users(base, app_id, secret, body)
            except FeishuError:
                logger.warning("按通讯录授权范围兜底成员失败，回落原始错误: %s", exc)
        raise HTTPException(status_code=502, detail=_directory_hint(exc)) from exc


@router.post("/api/webhooks", response_model=WebhookResponse)
def create_webhook(
    body: WebhookCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("settings:manage")),
):
    _require_provider_config(body.provider, body.config, bool(body.secret))
    row = Webhook(
        name=body.name.strip(),
        url=body.url,
        provider=body.provider,
        secret_enc=encrypt(body.secret or "") or None,
        events=list(dict.fromkeys(body.events)),
        headers=body.headers,
        config=_sanitized_config(body.provider, body.config),
        enabled=body.enabled,
        created_by=current_user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _view(row)


@router.put("/api/webhooks/{webhook_id}", response_model=WebhookResponse)
def update_webhook(
    webhook_id: int,
    body: WebhookUpdate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("settings:manage")),
):
    row = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook 不存在")
    data = body.model_dump(exclude_unset=True)
    secret = data.pop("secret", None)
    if secret:
        row.secret_enc = encrypt(secret)
    for key in ("name", "url", "provider", "events", "headers", "enabled"):
        if key in data:
            setattr(
                row,
                key,
                list(dict.fromkeys(data[key])) if key == "events" else data[key],
            )
    if "provider" in data:
        # 切换通道类型后,旧 provider 的 config(如飞书的 app_id/接收人/通讯录
        # 姓名映射)必须清掉,否则继续留在库里并随 _view 暴露给 settings:manage。
        row.config = _sanitized_config(row.provider, data.get("config"))
    elif "config" in data:
        row.config = _sanitized_config(row.provider, data["config"])
    _require_provider_config(
        row.provider, row.config, bool(secret) or bool(row.secret_enc)
    )
    db.commit()
    db.refresh(row)
    return _view(row)


@router.delete("/api/webhooks/{webhook_id}")
def delete_webhook(
    webhook_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("settings:manage")),
):
    row = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook 不存在")
    db.delete(row)
    db.commit()
    return {"message": "Webhook 已删除"}


@router.post("/api/webhooks/{webhook_id}/test", response_model=WebhookTestResponse)
def test_webhook(
    webhook_id: int,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("settings:manage")),
):
    row = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook 不存在")
    if row.provider == "feishu_app":
        return _deliver_test_feishu_app(row, db)
    headers = {"Content-Type": "application/json", "User-Agent": "DCN-Webhook/1.0"}
    headers.update(row.headers or {})
    secret = decrypt(row.secret_enc or "")
    if secret:
        headers.setdefault("X-Webhook-Secret", secret)
    started = time.monotonic()
    ok = False
    status_code = None
    message = ""
    response_preview = None
    request = Request(row.url, data=_test_body(row), headers=headers, method="POST")
    try:
        validate_outbound_url(row.url)
        with _webhook_opener.open(request, timeout=8) as response:
            status_code = response.status
            response_preview = response.read(500).decode("utf-8", errors="replace")
            ok = 200 <= status_code < 300
            message = (
                "Webhook 测试发送成功" if ok else f"Webhook 返回 HTTP {status_code}"
            )
    except HTTPError as exc:
        status_code = exc.code
        message = f"Webhook 返回 HTTP {exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        message = f"Webhook 连接失败: {exc}"
    duration_ms = int((time.monotonic() - started) * 1000)
    row.last_test_at = datetime.now(timezone.utc)
    row.last_test_status = "success" if ok else "failed"
    db.commit()
    return WebhookTestResponse(
        ok=ok,
        status_code=status_code,
        message=message,
        duration_ms=duration_ms,
        response_preview=response_preview,
    )
