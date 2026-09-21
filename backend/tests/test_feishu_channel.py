"""飞书自建应用私聊通道测试:配置解析、消息渲染、投递与 token 刷新。"""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app import config
from app.routers.webhooks import _require_provider_config, _sanitized_config
from app.services import alerts, feishu
from app.services.crypto import encrypt
from app.services.feishu import FeishuError


class _Response:
    """requests.Response 的最小替身。"""

    def __init__(self, data, status_code=200, text=""):
        self._data = data
        self.status_code = status_code
        self.text = text or str(data)

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


@pytest.fixture(autouse=True)
def _clear_tokens():
    feishu._token_cache.clear()
    yield
    feishu._token_cache.clear()


def _payload(**overrides):
    base = feishu.sample_payload()
    base.update(overrides)
    return base


def _token_response(token="t-token"):
    return _Response(
        {"code": 0, "msg": "ok", "tenant_access_token": token, "expire": 7200}
    )


def _sent_response(message_id="om_1"):
    return _Response({"code": 0, "msg": "success", "data": {"message_id": message_id}})


# ── 配置解析 ──


def test_parse_receivers_splits_dedupes_and_caps():
    assert feishu.parse_receivers("a@x.com, b@x.com；a@x.com\nou_1") == [
        "a@x.com",
        "b@x.com",
        "ou_1",
    ]
    assert feishu.parse_receivers(["ou_1", " ou_1 ", ""]) == ["ou_1"]
    assert feishu.parse_receivers(None) == []
    assert len(feishu.parse_receivers([f"ou_{i}" for i in range(80)])) == 50


def test_normalize_receive_id_type_and_style():
    assert feishu.normalize_receive_id_type("email") == "email"
    # 界面不再暴露 ID 类型，缺省即"按接收人自动识别"
    assert feishu.normalize_receive_id_type(None) == "auto"
    assert feishu.normalize_receive_id_type("") == "auto"
    with pytest.raises(FeishuError):
        feishu.normalize_receive_id_type("mobile")
    assert feishu.normalize_style("text") == "text"
    assert feishu.normalize_style("bogus") == "card"


def test_receive_id_type_is_inferred_from_the_value_itself():
    assert feishu.infer_receive_id_type("ops@example.com") == "email"
    assert feishu.infer_receive_id_type("ou_abc123") == "open_id"
    assert feishu.infer_receive_id_type("on_abc123") == "union_id"
    assert feishu.infer_receive_id_type("oc_abc123") == "chat_id"
    assert feishu.infer_receive_id_type("e33abc") == "user_id"
    # 显式类型优先于推断;落库时非法值退回 auto 而不是抛错
    assert feishu.resolve_receive_id_type("email", "ou_1") == "email"
    assert feishu.resolve_receive_id_type("auto", "ops@example.com") == "email"
    assert feishu.coerce_receive_id_type("mobile") == "auto"
    assert feishu.coerce_receive_id_type("user_id") == "user_id"


def test_sanitize_config_drops_unknown_keys_for_other_providers():
    cfg = {
        "app_id": " cli_x ",
        "receivers": "a@x.com, b@x.com",
        "style": "text",
        "junk": 1,
    }
    assert _sanitized_config("feishu_app", cfg) == {
        "app_id": "cli_x",
        "receive_id_type": "auto",
        "receivers": ["a@x.com", "b@x.com"],
        "receiver_names": {},
        "style": "text",
    }
    assert _sanitized_config("generic", cfg) == {}


def test_sanitize_config_keeps_only_known_receiver_names():
    cfg = {
        "app_id": "cli_x",
        "receivers": ["ou_1", "ops@x.com"],
        "receiver_names": {
            "ou_1": "张三",
            "ops@x.com": " 值班号 ",
            "ou_gone": "已移除",
        },
    }
    sanitized = _sanitized_config("feishu_app", cfg)
    assert sanitized["receiver_names"] == {"ou_1": "张三", "ops@x.com": "值班号"}
    assert sanitized["receive_id_type"] == "auto"


def test_provider_config_validation_messages():
    with pytest.raises(HTTPException) as missing_app:
        _require_provider_config("feishu_app", {"receivers": ["a@x.com"]}, True)
    assert "App ID" in missing_app.value.detail

    with pytest.raises(HTTPException) as missing_receivers:
        _require_provider_config("feishu_app", {"app_id": "cli_x"}, True)
    assert "接收人" in missing_receivers.value.detail

    with pytest.raises(HTTPException) as missing_secret:
        _require_provider_config(
            "feishu_app", {"app_id": "cli_x", "receivers": ["a@x.com"]}, False
        )
    assert "App Secret" in missing_secret.value.detail

    # 其它 provider 不做额外要求
    _require_provider_config("generic", {}, False)


# ── 消息渲染 ──


def test_build_card_colors_and_blocks():
    critical = feishu.build_card(_payload())
    assert critical["header"]["template"] == "red"
    assert "严重" in critical["header"]["title"]["content"]

    resolved = feishu.build_card(_payload(event="alert.resolved"))
    assert resolved["header"]["template"] == "green"

    enriched = _payload(
        remediation={
            "state": "verifying",
            "detail": "启动指令已下发，正在确认目标是否恢复运行…",
        },
        analysis={"state": "completed", "text": "mysqld 占用 92% CPU"},
    )
    body = str(feishu.build_card(enriched))
    assert "自动处置" in body and "启动指令已下发" in body
    assert "AI 归因" in body and "mysqld" in body


def test_build_card_translates_state_metric_value():
    """状态类告警的 value 是 1/0 编码:卡片要显示中文，恢复时显示正常态。"""

    def _state_payload(event: str, metric: str = "host_status") -> dict:
        payload = _payload(event=event)
        payload["alert"].update(
            {
                "metric": metric,
                "metric_label": "主机状态异常",
                "value": 1.0,
                "threshold": 0.0,
                "message": "主机 web-01 当前已离线或无法连接",
                "status": "resolved" if event == "alert.resolved" else "open",
            }
        )
        return payload

    def _current_value(payload: dict) -> str:
        """取出卡片上「当前值」那一格的正文。"""
        fields = feishu.build_card(payload)["elements"][0]["fields"]
        field = next(f for f in fields if f["text"]["content"].startswith("**当前值**"))
        return field["text"]["content"].split("\n", 1)[1]

    assert _current_value(_state_payload("alert.created")) == "离线"
    # 事件里存的还是触发时的 1，恢复通知不能再说离线
    assert _current_value(_state_payload("alert.resolved")) == "在线"
    for metric in ("container_status", "business_status"):
        assert _current_value(_state_payload("alert.created", metric)) == "异常"

    # 性能指标不受影响，仍是百分比 + 阈值
    assert _current_value(_payload()) == "92.6%（阈值 90.0%）"


def _card_time(payload: dict) -> str:
    """取出卡片上「时间」那一格的正文。"""
    fields = feishu.build_card(payload)["elements"][0]["fields"]
    field = next(f for f in fields if f["text"]["content"].startswith("**时间**"))
    return field["text"]["content"].split("\n", 1)[1]


def test_alert_time_renders_in_display_timezone(monkeypatch):
    """事件时间来自 MySQL(naive UTC)，消息里要按运维时区显示，不能慢 8 小时。"""
    monkeypatch.setattr(config, "DISPLAY_TIMEZONE", "Asia/Shanghai")
    payload = _payload()

    payload["alert"]["last_seen_at"] = "2026-09-08T09:02:15"
    assert _card_time(payload) == "2026/09/08 17:02"
    assert "2026/09/08 17:02" in feishu.build_text(payload)
    # 多维表格工作流的 alert_time 走同一条渲染路径
    assert (
        json.loads(alerts._feishu_workflow_body(payload))["alert_time"]
        == "2026/09/08 17:02"
    )

    # 已经带时区的输入不能被二次偏移
    payload["alert"]["last_seen_at"] = "2026-09-08T17:02:15+08:00"
    assert _card_time(payload) == "2026/09/08 17:02"

    # 换个时区跟着变，说明不是硬编码 +8
    monkeypatch.setattr(config, "DISPLAY_TIMEZONE", "UTC")
    payload["alert"]["last_seen_at"] = "2026-09-08T09:02:15"
    assert _card_time(payload) == "2026/09/08 09:02"


def test_outbound_payload_timestamps_carry_utc_offset():
    """出站 JSON 的时间要带时区，接收方(工作流/第三方)才不会按本地时间误读。"""
    assert (
        alerts._utc_iso(datetime(2026, 9, 8, 9, 2, 15)) == "2026-09-08T09:02:15+00:00"
    )
    assert alerts._utc_iso(None) is None


def test_build_text_and_content_style():
    text = feishu.build_text(_payload())
    assert text.startswith("[严重] Webhook 测试告警")
    assert "示例服务器-01" in text

    msg_type, content = feishu.build_content(_payload(), "text")
    assert msg_type == "text" and '"text"' in content
    msg_type, content = feishu.build_content(_payload(), "card")
    assert msg_type == "interactive" and '"header"' in content


# ── 投递 ──


def test_deliver_sends_to_every_receiver():
    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _token_response() if "tenant_access_token" in url else _sent_response()

    with patch("app.services.feishu.requests.post", side_effect=fake_post):
        result = feishu.deliver(
            _payload(),
            base="https://open.feishu.cn",
            app_id="cli_x",
            app_secret="s3cret",
            receivers=["ou_1", "ou_2"],
            receive_id_type="open_id",
            style="card",
        )

    assert result["ok"] is True
    assert (result["receivers"], result["delivered"]) == (2, 2)
    assert result["message_ids"] == ["om_1", "om_1"]
    # token 只取一次，两个接收人复用;content 是 JSON 字符串
    assert sum("tenant_access_token" in c["url"] for c in calls) == 1
    sends = [c for c in calls if "im/v1/messages" in c["url"]]
    assert len(sends) == 2
    assert "receive_id_type=open_id" in sends[0]["url"]
    assert sends[0]["json"]["receive_id"] == "ou_1"
    assert isinstance(sends[0]["json"]["content"], str)
    assert sends[0]["headers"]["Authorization"] == "Bearer t-token"
    assert sends[0]["allow_redirects"] is False


def test_deliver_reports_partial_failure():
    responses = iter(
        [
            _token_response(),
            _sent_response("om_1"),
            _Response(
                {"code": 230002, "msg": "bot ability is not activated"}, status_code=400
            ),
        ]
    )
    with patch(
        "app.services.feishu.requests.post", side_effect=lambda *a, **k: next(responses)
    ):
        result = feishu.deliver(
            _payload(),
            base=None,
            app_id="cli_x",
            app_secret="s3cret",
            receivers=["ou_1", "ou_2"],
        )

    assert result["ok"] is False
    assert result["delivered"] == 1
    assert "ou_2" in result["error"] and "230002" in result["error"]


def _uploaded_response(file_key="file_v3_x"):
    return _Response({"code": 0, "msg": "success", "data": {"file_key": file_key}})


def _no_availability_response():
    # 230013: 机器人对该用户不可用（不在应用可用范围）
    return _Response(
        {"code": 230013, "msg": "Bot has NO availability to this user."},
        status_code=400,
    )


def test_deliver_report_partial_summary_failure_still_sends_attachment():
    """一个接收人不在应用可用范围时，另一位照常收到摘要和 PDF 附件。

    旧行为：任一接收人摘要失败就整单跳过附件，坏接收人吞掉所有人的
    PDF（实测：两人选一人 230013，可用范围内那位只剩文字摘要）。
    """
    responses = iter(
        [
            _token_response(),
            _sent_response("om_1"),  # ou_1 摘要送达
            _no_availability_response(),  # ou_2 摘要失败
            _uploaded_response(),
            _sent_response("om_1f"),  # 附件只发给 ou_1
        ]
    )
    with patch(
        "app.services.feishu.requests.post", side_effect=lambda *a, **k: next(responses)
    ):
        result = feishu.deliver_report(
            "摘要",
            b"%PDF-1.4",
            "report.pdf",
            base="https://open.feishu.cn",
            app_id="cli_x",
            app_secret="s3cret",
            receivers=["ou_1", "ou_2"],
        )

    assert result["ok"] is True
    assert result["file_ok"] is True
    assert result["delivered"] == 1
    assert result["message_ids"] == ["om_1"]
    assert result["file_key"] == "file_v3_x"
    assert "ou_2" in result["error"] and "230013" in result["error"]


def test_deliver_report_all_summary_failed_skips_attachment():
    """摘要一个都没送达才跳过附件（docstring 的原始意图）。"""
    responses = iter(
        [
            _token_response(),
            _no_availability_response(),
            _no_availability_response(),
        ]
    )
    with patch(
        "app.services.feishu.requests.post", side_effect=lambda *a, **k: next(responses)
    ):
        result = feishu.deliver_report(
            "摘要",
            b"%PDF-1.4",
            "report.pdf",
            base="https://open.feishu.cn",
            app_id="cli_x",
            app_secret="s3cret",
            receivers=["ou_1", "ou_2"],
        )

    assert result["ok"] is False
    assert result["file_ok"] is False
    assert result["delivered"] == 0
    assert "230013" in (result["error"] or "")
    assert "摘要" in (result["file_error"] or "")


def test_deliver_refreshes_expired_token_once():
    """token 刚好失效时刷新并重发，但不能对每个接收人都重刷。"""
    responses = iter(
        [
            _token_response("t-old"),
            _Response({"code": 99991661, "msg": "invalid token"}, status_code=401),
            _token_response("t-new"),
            _sent_response("om_1"),
            _sent_response("om_2"),
        ]
    )
    with patch(
        "app.services.feishu.requests.post", side_effect=lambda *a, **k: next(responses)
    ):
        result = feishu.deliver(
            _payload(),
            base="https://open.feishu.cn",
            app_id="cli_x",
            app_secret="s3cret",
            receivers=["ou_1", "ou_2"],
        )

    assert result["ok"] is True
    assert result["delivered"] == 2
    assert feishu._token_cache["https://open.feishu.cn|cli_x"][0] == "t-new"


def test_deliver_rejects_incomplete_config():
    with pytest.raises(FeishuError, match="App ID"):
        feishu.deliver(
            _payload(), base=None, app_id="", app_secret="x", receivers=["ou_1"]
        )
    with pytest.raises(FeishuError, match="接收人"):
        feishu.deliver(
            _payload(), base=None, app_id="cli_x", app_secret="x", receivers=[]
        )
    with pytest.raises(FeishuError, match="receive_id_type"):
        feishu.deliver(
            _payload(),
            base=None,
            app_id="cli_x",
            app_secret="x",
            receivers=["ou_1"],
            receive_id_type="mobile",
        )


def test_token_is_cached_until_expiry():
    posts: list[str] = []
    with patch(
        "app.services.feishu.requests.post",
        side_effect=lambda url, **kwargs: posts.append(url) or _token_response(),
    ):
        first = feishu.tenant_access_token("https://open.feishu.cn", "cli_x", "s")
        second = feishu.tenant_access_token("https://open.feishu.cn", "cli_x", "s")
    assert first == second == "t-token"
    assert len(posts) == 1


def test_token_error_is_wrapped():
    with patch(
        "app.services.feishu.requests.post",
        return_value=_Response(
            {"code": 10003, "msg": "invalid app_secret"}, status_code=400
        ),
    ):
        with pytest.raises(FeishuError) as exc:
            feishu.tenant_access_token("https://open.feishu.cn", "cli_x", "bad")
    assert exc.value.code == 10003 and "app_secret" in str(exc.value)


# ── 告警投递分支 ──


def test_alert_delivery_branch_uses_feishu_app():
    webhook = SimpleNamespace(
        id=7,
        name="飞书私聊",
        url="https://open.feishu.cn",
        provider="feishu_app",
        secret_enc=encrypt("app-secret"),
        headers={},
        config={
            "app_id": "cli_x",
            "receive_id_type": "email",
            "receivers": ["ops@example.com"],
            "style": "card",
        },
    )
    captured: dict = {}

    def fake_deliver(payload, **kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "status_code": 200,
            "error": None,
            "receivers": 1,
            "delivered": 1,
            "message_ids": ["om_9"],
        }

    with patch("app.services.feishu.deliver", side_effect=fake_deliver):
        result = alerts._deliver_one(webhook, _payload())

    assert result["ok"] is True and result["delivered"] == 1
    assert result["webhook_id"] == 7
    # App Secret 从加密列里解出来，配置从 webhooks.config 里读
    assert captured["app_secret"] == "app-secret"
    assert captured["app_id"] == "cli_x"
    assert captured["receivers"] == ["ops@example.com"]
    assert captured["receive_id_type"] == "email"
    assert captured["base"] == "https://open.feishu.cn"


def test_alert_delivery_branch_reports_feishu_error():
    webhook = SimpleNamespace(
        id=8,
        name="飞书私聊",
        url="https://open.feishu.cn",
        provider="feishu_app",
        secret_enc=None,
        headers={},
        config={"app_id": "", "receivers": []},
    )
    result = alerts._deliver_one(webhook, _payload())
    assert result["ok"] is False
    assert "App" in result["error"]
    assert result["duration_ms"] >= 0


# ── 通讯录读取 ──


def test_department_id_and_page_size_are_normalized():
    assert feishu.normalize_department_id(None) == "0"
    assert feishu.normalize_department_id(" od-abc_1 ") == "od-abc_1"
    with pytest.raises(FeishuError):
        feishu.normalize_department_id("od&x=1")
    assert feishu.normalize_page_size(999) == feishu.MAX_PAGE_SIZE
    assert feishu.normalize_page_size("20") == 20
    assert feishu.normalize_page_size(None) == feishu.MAX_PAGE_SIZE


def test_receiver_of_follows_configured_id_type():
    user = feishu._user_view(
        {
            "open_id": "ou_1",
            "user_id": "e33",
            "union_id": "on_1",
            "name": "张三",
            "email": "z@x.com",
            "enterprise_email": "z@ent.com",
        }
    )
    assert feishu.receiver_of(user, "email") == "z@ent.com"
    assert feishu.receiver_of(user, "open_id") == "ou_1"
    assert feishu.receiver_of(user, "user_id") == "e33"
    assert feishu.receiver_of(user, "union_id") == "on_1"
    # 自动识别:通讯录选人一律写 open_id，最稳且不受邮箱可见性影响
    assert feishu.receiver_of(user, "auto") == "ou_1"
    assert feishu.receiver_of(user, "") == "ou_1"
    # 邮箱缺失时不编造，交给前端置灰
    assert feishu.receiver_of({"open_id": "ou_2"}, "email") == ""


def test_deliver_infers_receive_id_type_per_receiver():
    """界面上没有 ID 类型可选，投递时按每个接收人自己的形态识别。"""
    sends: list[dict] = []

    def fake_post(url, **kwargs):
        if "tenant_access_token" in url:
            return _token_response()
        sends.append({"url": url, "json": kwargs.get("json")})
        return _sent_response()

    with patch("app.services.feishu.requests.post", side_effect=fake_post):
        result = feishu.deliver(
            _payload(),
            base="https://open.feishu.cn",
            app_id="cli_x",
            app_secret="s3cret",
            receivers=["ops@example.com", "ou_1", "oc_group"],
        )

    assert (result["receivers"], result["delivered"]) == (3, 3)
    assert [s["url"].split("receive_id_type=")[1] for s in sends] == [
        "email",
        "open_id",
        "chat_id",
    ]


def test_list_departments_maps_items_and_paging():
    captured: dict = {}

    def fake_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        if "tenant_access_token" in url:
            return _token_response()
        return _Response(
            {
                "code": 0,
                "data": {
                    "has_more": True,
                    "page_token": "pt-2",
                    "items": [
                        {
                            "open_department_id": "od_1",
                            "name": "运维部",
                            "member_count": 12,
                            "parent_department_id": "0",
                        },
                        {"name": "缺少 ID 的脏数据"},
                    ],
                },
            }
        )

    with (
        patch("app.services.feishu.requests.get", side_effect=fake_get),
        patch("app.services.feishu.requests.post", return_value=_token_response()),
    ):
        page = feishu.list_departments(
            base="https://open.feishu.cn", app_id="cli_x", app_secret="s", page_size=999
        )

    # 飞书返回的人数不再下发:界面不显示，也就不会误导成「0 人」
    assert page["items"] == [
        {"open_department_id": "od_1", "name": "运维部", "parent_department_id": "0"}
    ]
    assert (page["has_more"], page["page_token"]) == (True, "pt-2")
    assert captured["url"].endswith("/open-apis/contact/v3/departments")
    assert captured["params"]["parent_department_id"] == "0"
    assert captured["params"]["page_size"] == feishu.MAX_PAGE_SIZE
    assert captured["params"]["fetch_child"] == "false"
    assert captured["allow_redirects"] is False


def test_list_users_strips_mobile_and_keeps_pickable_ids():
    response = _Response(
        {
            "code": 0,
            "data": {
                "has_more": False,
                "items": [
                    {
                        "open_id": "ou_1",
                        "user_id": "e33",
                        "union_id": "on_1",
                        "name": "张三",
                        "email": "z@x.com",
                        "mobile": "+8613000000000",
                        "avatar": {"avatar_72": "https://avatar/72.png"},
                        "department_ids": ["od_1"],
                    }
                ],
            },
        }
    )
    with (
        patch("app.services.feishu.requests.post", return_value=_token_response()),
        patch("app.services.feishu.requests.get", return_value=response),
    ):
        page = feishu.list_users(
            base=None, app_id="cli_x", app_secret="s", department_id="od_1"
        )

    user = page["items"][0]
    assert user["open_id"] == "ou_1" and user["name"] == "张三"
    assert user["avatar"] == "https://avatar/72.png"
    assert "mobile" not in user
    # 姓名读得到，不该误报缺字段权限
    assert page["name_scope_missing"] is False


def test_list_users_falls_back_and_flags_missing_name_scope():
    """没有 contact:user.base:readonly 时飞书不返回 name:退回邮箱前缀/ID 并标记出来。"""
    anonymous = _Response(
        {
            "code": 0,
            "data": {
                "has_more": False,
                "items": [
                    {"open_id": "ou_1", "email": "zhang.san@x.com"},
                    {"open_id": "ou_2", "en_name": "  "},
                ],
            },
        }
    )
    with (
        patch("app.services.feishu.requests.post", return_value=_token_response()),
        patch("app.services.feishu.requests.get", return_value=anonymous),
    ):
        page = feishu.list_users(base=None, app_id="cli_x", app_secret="s")

    assert [user["name"] for user in page["items"]] == ["zhang.san", "ou_2"]
    assert page["name_scope_missing"] is True


def test_department_name_falls_back_to_id_when_not_authorized():
    """部门名受字段级权限裁剪(要 contact:department.base:readonly):读不到时用部门 ID。"""
    anonymous = _Response(
        {
            "code": 0,
            "data": {"items": [{"open_department_id": "od_1", "member_count": 3}]},
        }
    )
    with (
        patch("app.services.feishu.requests.post", return_value=_token_response()),
        patch("app.services.feishu.requests.get", return_value=anonymous),
    ):
        page = feishu.list_departments(base=None, app_id="cli_x", app_secret="s")

    assert page["items"] == [
        {"open_department_id": "od_1", "name": "od_1", "parent_department_id": ""}
    ]
    assert page["name_scope_missing"] is True


def test_directory_permission_error_becomes_actionable_hint():
    from app.routers.webhooks import _directory_hint

    denied = FeishuError("[99991672] no permission", code=99991672)
    hint = _directory_hint(denied)
    assert "通讯录授权范围" in hint and "99991672" in hint

    # 40004 = 部门不在「通讯录权限范围」内;连授权范围都读不到时才把这段提示抛给界面
    out_of_scope = _directory_hint(
        FeishuError("[40004] no dept authority error", code=40004)
    )
    assert "通讯录权限范围" in out_of_scope and "admin.feishu.cn" in out_of_scope
    assert "contact:contact.base:readonly" in out_of_scope and "40004" in out_of_scope
    assert feishu.is_scope_error(
        FeishuError("[40004] no dept authority error", code=40004)
    )
    assert not feishu.is_scope_error(
        FeishuError("[99991672] no permission", code=99991672)
    )

    # 非权限类错误原样透出，不误导用户去改权限
    assert _directory_hint(FeishuError("[230002] bot not activated", code=230002)) == (
        "[230002] bot not activated"
    )


def test_list_users_surfaces_feishu_business_error():
    with (
        patch("app.services.feishu.requests.post", return_value=_token_response()),
        patch(
            "app.services.feishu.requests.get",
            return_value=_Response(
                {"code": 99991672, "msg": "no permission"}, status_code=403
            ),
        ),
    ):
        with pytest.raises(FeishuError) as exc:
            feishu.list_users(base=None, app_id="cli_x", app_secret="s")
    assert exc.value.code == 99991672 and exc.value.status == 403


def test_list_authorized_scope_returns_ids_and_paging():
    captured: dict = {}

    def fake_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        if "tenant_access_token" in url:
            return _token_response()
        return _Response(
            {
                "code": 0,
                "data": {
                    "department_ids": ["od_1", "od_1", "  ", "od_2"],
                    "user_ids": ["ou_9"],
                    "group_ids": ["g_1"],
                    "has_more": True,
                    "page_token": "pt-2",
                },
            }
        )

    with (
        patch("app.services.feishu.requests.get", side_effect=fake_get),
        patch("app.services.feishu.requests.post", return_value=_token_response()),
    ):
        scope = feishu.list_authorized_scope(base=None, app_id="cli_x", app_secret="s")

    # 去空去重，用户组不用于选人所以不下发
    assert scope["department_ids"] == ["od_1", "od_2"] and scope["user_ids"] == ["ou_9"]
    assert (scope["has_more"], scope["page_token"]) == (True, "pt-2")
    assert captured["url"].endswith("/open-apis/contact/v3/scopes")
    assert captured["params"]["department_id_type"] == "open_department_id"
    assert captured["params"]["user_id_type"] == "open_id"


def test_get_departments_by_ids_repeats_param_and_maps_items():
    captured: dict = {}

    def fake_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        if "tenant_access_token" in url:
            return _token_response()
        return _Response(
            {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "open_department_id": "od_1",
                            "name": "运维部",
                            "member_count": 7,
                            "parent_department_id": "0",
                        },
                        {"name": "缺少 ID 的脏数据"},
                    ]
                },
            }
        )

    with (
        patch("app.services.feishu.requests.get", side_effect=fake_get),
        patch("app.services.feishu.requests.post", return_value=_token_response()),
    ):
        page = feishu.get_departments_by_ids(
            base=None,
            app_id="cli_x",
            app_secret="s",
            department_ids=["od_1", "od_2", "od_1"],
        )

    assert page["items"] == [
        {"open_department_id": "od_1", "name": "运维部", "parent_department_id": "0"}
    ]
    assert page["name_scope_missing"] is False
    assert captured["url"].endswith("/open-apis/contact/v3/departments/batch")
    # requests 会把列表展开成重复的同名参数，正是批量接口要的形式
    assert captured["params"]["department_ids"] == ["od_1", "od_2"]


def test_get_users_by_ids_caps_at_page_size_and_skips_empty_input():
    captured: dict = {}

    def fake_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        if "tenant_access_token" in url:
            return _token_response()
        return _Response(
            {
                "code": 0,
                # 没有 name 字段(缺 contact:user.base:readonly):退回邮箱前缀并标记出来
                "data": {
                    "items": [
                        {"open_id": "ou_1", "email": "zhang@x.com", "mobile": "+8613"}
                    ]
                },
            }
        )

    with (
        patch("app.services.feishu.requests.get", side_effect=fake_get),
        patch("app.services.feishu.requests.post", return_value=_token_response()),
    ):
        page = feishu.get_users_by_ids(
            base=None,
            app_id="cli_x",
            app_secret="s",
            user_ids=[f"ou_{i}" for i in range(60)],
        )
        # 空输入直接返回，不发请求
        assert feishu.get_users_by_ids(
            base=None, app_id="cli_x", app_secret="s", user_ids=[]
        ) == {
            "items": [],
            "name_scope_missing": False,
        }

    assert [user["open_id"] for user in page["items"]] == ["ou_1"]
    assert page["items"][0]["name"] == "zhang"
    assert "mobile" not in page["items"][0] and page["name_scope_missing"] is True
    assert captured["url"].endswith("/open-apis/contact/v3/users/batch")
    assert len(captured["params"]["user_ids"]) == feishu.MAX_PAGE_SIZE
    assert captured["params"]["user_id_type"] == "open_id"


# ── 归因类告警的卡片口径(2026-09-16):详情段=归因结论,无结论不渲染详情段 ──


def test_analysis_metric_card_shows_only_attribution():
    """cpu/mem/disk/container 告警:卡片不铺基础文案,详情段就是归因结论;
    未配置运维接入(无归因)时连详情段都不渲染——字段区已够表达告警本身。"""
    analysis = {"state": "completed", "text": "java 进程占用 91% 内存，建议下调 -Xmx"}
    # 有归因:唯一内容段是 AI 归因,基础文案(详情)不再出现
    done = _payload(analysis=analysis)
    done["alert"]["message"] = (
        "[pve] SRM 内存使用率 94.8% 已超过阈值 90.0%；AI 归因：java…"
    )
    body = str(feishu.build_card(done))
    assert "AI 归因" in body and "java 进程占用" in body
    assert "详情" not in body and "已超过阈值" not in body

    # 无归因(虚拟机未配置运维接入/归因未完成):不渲染任何详情段
    bare = _payload(analysis={"state": "", "text": None})
    bare_body = str(feishu.build_card(bare))
    assert "详情" not in bare_body and "AI 归因" not in bare_body
    assert "已超过阈值" not in bare_body

    # text 样式同口径:有归因只有 AI 归因行,无归因没有内容行
    text = feishu.build_text(_payload(analysis=analysis))
    assert "AI 归因：java" in text and "内容：" not in text
    text_bare = feishu.build_text(_payload(analysis={"state": "", "text": None}))
    assert "内容：" not in text_bare and "AI 归因" not in text_bare


def test_non_analysis_metric_card_keeps_detail_message():
    """host_status 等非归因告警:详情段照旧显示消息(它们没有 AI 归因可替代)。"""
    base = _payload(analysis={"state": "", "text": None})
    base["alert"]["metric"] = "host_status"
    base["alert"]["metric_label"] = "主机状态异常"
    base["alert"]["message"] = "主机 web-01 当前已离线或无法连接"
    body = str(feishu.build_card(base))
    assert "详情" in body and "已离线" in body
    text = feishu.build_text(base)
    assert "内容：主机 web-01 当前已离线" in text


def test_remediation_progress_card_skips_detail_block():
    """自动处置进度卡只展示处置结论:不再铺整段历史消息(详情块)——
    离线→拉起→结果层层叠加的话术在进度卡上纯属重复。"""
    payload = _payload(
        event="alert.remediation",
        analysis={"state": "", "text": None},
        message="虚拟机 CAD 当前离线或未运行；正在触发自动拉起操作；自动拉起操作成功，目标已恢复运行",
    )
    payload["remediation"] = {
        "state": "succeeded",
        "detail": "自动拉起操作成功，目标已恢复运行",
    }
    body = str(feishu.build_card(payload))
    assert "自动处置" in body and "自动拉起操作成功" in body
    assert "详情" not in body
    assert "正在触发自动拉起操作" not in body  # 历史话术不进进度卡
    # text 样式同口径
    text = feishu.build_text(payload)
    assert "自动处置：自动拉起操作成功" in text
    assert "内容：" not in text


def test_resolved_card_with_remediation_keeps_only_conclusion():
    """主机状态恢复卡(2026-09-17 用户定调):有自动处置结论时只保留
    「自动处置」块;详情块是 base+进度层层叠加的历史消息(离线话术已过时、
    结论与处置块重复),不再渲染。自然恢复(未处置)的恢复卡详情照旧。"""
    recovered = _payload(
        event="alert.resolved",
        analysis={"state": "", "text": None},
        message="虚拟机 CAD 当前离线或未运行；正在触发自动拉起操作；自动拉起操作成功，目标已恢复运行",
    )
    recovered["alert"]["status"] = "resolved"
    recovered["alert"]["metric"] = "host_status"
    recovered["alert"]["metric_label"] = "主机状态异常"
    recovered["remediation"] = {
        "state": "succeeded",
        "detail": "自动拉起操作成功，目标已恢复运行",
    }
    body = str(feishu.build_card(recovered))
    assert "自动处置" in body and "自动拉起操作成功，目标已恢复运行" in body
    assert "详情" not in body
    assert "正在触发自动拉起操作" not in body  # 历史话术不进恢复卡
    assert "当前离线或未运行" not in body  # 过时的离线文案不再重铺
    # text 样式同口径:只有「自动处置」行,没有「内容」行
    text = feishu.build_text(recovered)
    assert "自动处置：自动拉起操作成功" in text
    assert "内容：" not in text

    # 自然恢复(没有自动处置):详情块照旧——host_status 的口径只对
    # 有处置结论的恢复卡收紧,不要扩大到其它类型(2026-09-18 用户纠偏)
    natural = _payload(event="alert.resolved", analysis={"state": "", "text": None})
    natural["alert"]["status"] = "resolved"
    natural["alert"]["metric"] = "host_status"
    natural["alert"]["message"] = "主机 web-01 当前已离线或无法连接"
    natural_body = str(feishu.build_card(natural))
    assert "详情" in natural_body and "已离线" in natural_body
    assert "自动处置" not in natural_body


# ── 容器告警卡片口径(2026-09-17 用户定调):与主机同为两步卡片模型 ──


def _container_payload(**overrides) -> dict:
    payload = _payload(**overrides)
    payload["alert"].update(
        {
            "metric": "container_status",
            "metric_label": "容器状态异常",
            "value": 1.0,
            "threshold": 0.0,
            "status": "open",
            "message": "容器 docker-rustfs-1 当前状态异常：Exited (0) About a minute ago；正在触发自动拉起操作",
        }
    )
    return payload


def test_container_card_keeps_detail_message():
    """容器告警(无归因结论)的卡片详情段=状态明细+处置话术——
    "Exited (0) About a minute ago" 这类信息字段区表达不了,不能像
    cpu/mem/disk 那样一张光板卡(2026-09-17 用户定调)。"""
    body = str(feishu.build_card(_container_payload()))
    assert "详情" in body and "Exited (0) About a minute ago" in body
    assert "正在触发自动拉起操作" in body
    text = feishu.build_text(_container_payload())
    assert "内容：容器 docker-rustfs-1 当前状态异常" in text


def test_container_resolved_card_keeps_only_conclusion():
    """容器恢复卡(拉起成功):与主机恢复卡同口径——有自动处置结论时只保留
    「自动处置」块,详情块(离线话术已过时、结论重复)不再渲染。"""
    recovered = _container_payload(event="alert.resolved")
    recovered["alert"]["status"] = "resolved"
    recovered["alert"]["message"] = (
        "容器 docker-rustfs-1 当前状态异常：Exited (0) About a minute ago；正在触发自动拉起操作；自动拉起操作成功，目标已恢复运行"
    )
    recovered["remediation"] = {
        "state": "succeeded",
        "detail": "自动拉起操作成功，目标已恢复运行",
    }
    body = str(feishu.build_card(recovered))
    assert "自动处置" in body and "自动拉起操作成功，目标已恢复运行" in body
    assert "详情" not in body
    assert "正在触发自动拉起操作" not in body
    text = feishu.build_text(recovered)
    assert "自动处置：自动拉起操作成功" in text
    assert "内容：" not in text


def test_container_card_with_analysis_shows_only_conclusion():
    """容器归因结论已出时(补发卡/冷却重发):升级为 AI 归因块承载结论,
    不再重铺基础消息——与 cpu/mem/disk 的归因卡同口径。"""
    done = _container_payload(
        event="alert.analysis",
        analysis={"state": "completed", "text": "容器因 OOM 被杀死,建议下调内存限制"},
    )
    done["alert"]["message"] = (
        "容器 docker-rustfs-1 当前状态异常：Exited (0) About a minute ago；"
        "自动拉起操作成功，目标已恢复运行；AI 归因：容器因 OOM 被杀死…"
    )
    body = str(feishu.build_card(done))
    assert "AI 归因" in body and "OOM 被杀死" in body
    assert "详情" not in body and "Exited (0)" not in body
    text = feishu.build_text(done)
    assert "AI 归因：容器因 OOM" in text
    assert "内容：" not in text


def test_business_resolved_card_has_no_detail():
    """业务恢复卡(2026-09-18 用户定调):详情块一律不渲染——消息里的
    "异常明细:虚拟机 CAD 离线或未运行"是触发时刻的快照,恢复后原样重铺
    是过时且误导的信息;恢复由字段区(当前值=正常)表达。"""
    payload = _payload(event="alert.resolved", analysis={"state": "", "text": None})
    payload["alert"].update(
        {
            "metric": "business_status",
            "metric_label": "业务状态异常",
            "value": 1.0,
            "threshold": 0.0,
            "status": "resolved",
            "message": (
                "业务 cs 状态异常：服务器 0/1 在线，接口 1/1 正常\n"
                "异常明细：虚拟机 CAD 离线或未运行"
            ),
        }
    )
    payload["remediation"] = {"state": "", "state_label": "未处置", "detail": None}
    body = str(feishu.build_card(payload))
    assert "详情" not in body
    assert "异常明细" not in body and "离线或未运行" not in body
    # 当前值显示正常态
    fields = feishu.build_card(payload)["elements"][0]["fields"]
    value_field = next(
        f for f in fields if f["text"]["content"].startswith("**当前值**")
    )
    assert "正常" in value_field["text"]["content"]
    # text 样式同口径:无「内容」行
    text = feishu.build_text(payload)
    assert "内容：" not in text
