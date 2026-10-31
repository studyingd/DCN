"""Webhook 出站适配与投递(从 alerts.py 拆出,2026-10-31 Phase-1)。

SEVERITY_LABELS/_webhook_body/_deliver_one 等经 alerts.py 门面 re-export,
测试对 app.services.alerts._deliver_one / _feishu_workflow_body 的 patch
语义不变(调用方 _dispatch_notifications 留在门面,按 alerts 全局名解析)。
"""

import base64
import hashlib
import hmac
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.models.webhook import Webhook
from app.services import feishu
from app.services.crypto import decrypt
from app.services.feishu import FeishuError
from app.utils import format_local_time
from app.validators import validate_outbound_url

SEVERITY_LABELS = {"info": "信息", "warning": "警告", "critical": "严重"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 3xx 视为不可达,防止 webhook 地址配了跳转后绕过校验


_webhook_opener = build_opener(_NoRedirect)


def _feishu_workflow_body(payload: dict) -> bytes:
    """Minimal Request Body for a Feishu Workflow webhook."""
    alert = payload["alert"]
    device = payload["device"]
    body = {
        "device_name": device.get("name", ""),
        "device_ip": device.get("ip_address") or "",
        "severity": SEVERITY_LABELS.get(
            alert.get("severity"), alert.get("severity", "警告")
        ),
        "alert_time": format_local_time(
            alert.get("last_seen_at") or payload.get("timestamp", "")
        ),
        "alert_content": alert.get("message", ""),
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _webhook_body(webhook: Webhook, payload: dict) -> bytes:
    """Adapt the canonical alert payload to the selected webhook provider."""
    # 飞书工作流 Webhook 接收自定义 Request Body，应直接发送完整的
    # DCN 告警业务 JSON，便于工作流映射到多维表格字段。
    if webhook.provider == "feishu":
        return _feishu_workflow_body(payload)
    # 可选的飞书群自定义机器人格式（不用于多维表格工作流）。
    if webhook.provider == "feishu_bot":
        timestamp = str(int(time.time()))
        secret = decrypt(webhook.secret_enc or "")
        body = {
            "msg_type": "text",
            "content": {
                "text": (
                    f"[{payload['alert']['severity'].upper()}] DCN 告警\n"
                    f"规则：{payload['alert']['rule_name']}\n"
                    f"设备：{payload['device']['name']} ({payload['device'].get('ip_address') or '-'})\n"
                    f"内容：{payload['alert']['message']}\n"
                    f"事件：{payload['event']}"
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
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _deliver_feishu_app(webhook: Webhook, payload: dict) -> dict:
    """飞书自建应用:直接私聊/群发给配置的 receive_id，不经过多维表格工作流。

    url 存的是开放平台域名(默认 https://open.feishu.cn)，app_secret 复用
    secret_enc;接收人与样式放在 webhooks.config 里。
    """
    config = webhook.config or {}
    started = time.monotonic()
    base_result = {"webhook_id": webhook.id, "name": webhook.name}
    try:
        result = feishu.deliver(
            payload,
            base=webhook.url,
            app_id=config.get("app_id"),
            app_secret=decrypt(webhook.secret_enc or ""),
            receivers=config.get("receivers"),
            receive_id_type=config.get("receive_id_type")
            or feishu.AUTO_RECEIVE_ID_TYPE,
            style=config.get("style", "card"),
        )
    except (FeishuError, ValueError) as exc:
        status = exc.status if isinstance(exc, FeishuError) else None
        return {
            **base_result,
            "ok": False,
            "status_code": status,
            "error": str(exc)[:200],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        **base_result,
        "ok": bool(result["ok"]),
        "status_code": result["status_code"],
        "error": result.get("error"),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "receivers": result.get("receivers"),
        "delivered": result.get("delivered"),
    }


def _deliver_one(webhook: Webhook, payload: dict) -> dict:
    if webhook.provider == "feishu_app":
        return _deliver_feishu_app(webhook, payload)
    headers = {"Content-Type": "application/json", "User-Agent": "DCN-Alert/1.0"}
    headers.update(webhook.headers or {})
    secret = decrypt(webhook.secret_enc or "")
    if secret:
        headers.setdefault("X-Webhook-Secret", secret)
    request = Request(
        webhook.url,
        data=_webhook_body(webhook, payload),
        headers=headers,
        method="POST",
    )
    started = time.monotonic()
    try:
        validate_outbound_url(webhook.url)
        with _webhook_opener.open(request, timeout=8) as response:
            status = response.status
            return {
                "webhook_id": webhook.id,
                "name": webhook.name,
                "ok": 200 <= status < 300,
                "status_code": status,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
    except HTTPError as exc:
        return {
            "webhook_id": webhook.id,
            "name": webhook.name,
            "ok": False,
            "status_code": exc.code,
            "error": f"HTTP {exc.code}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "webhook_id": webhook.id,
            "name": webhook.name,
            "ok": False,
            "status_code": None,
            "error": str(exc)[:200],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
