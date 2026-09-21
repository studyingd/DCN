"""Webhook 通道配置 API 测试(/api/webhooks)。

重点覆盖飞书自建应用的 config:界面上不再提供「接收人 ID 类型」与「消息样式」，
落库时应默认 auto + card，且 receiver_names 只保留仍在接收人列表里的条目。
另外覆盖通讯录选人的「部分授权」兜底:只勾选了指定部门/成员时飞书读根部门会返回
40004，接口要改用 /contact/v3/scopes 的授权范围返回可选项，而不是把错误抛给界面。
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.role import Role
from app.models.user import User
from app.models.webhook import Webhook
from app.services.auth import create_access_token
from app.services.crypto import decrypt

client = TestClient(app)

_FEISHU_CONFIG = {
    "app_id": " cli_smoke ",
    "receivers": ["ops@example.com", "ou_smoke1"],
    "receiver_names": {"ou_smoke1": "张三", "ou_gone": "已移除的人"},
    "junk": "不该落库",
}


def _make_token(db: Session) -> tuple[str, int, int]:
    """建一个临时角色 + 用户并签发 token，返回 (token, user_id, role_id)。

    id 要带出来给 fixture 回收：users/roles 不清理的话每跑一次测试就多一批
    ``hookuser_*``，测试库很快堆到上百行。
    """
    stamp = datetime.now(timezone.utc).timestamp()
    role = Role(
        name=f"webhook_role_{stamp}",
        permissions=json.dumps(["settings:manage"]),
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"hookuser_{stamp}",
        password="irrelevant",
        role="admin",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.flush()
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=["settings:manage"],
        device_scope="all",
    )
    return token, user.id, role.id


@pytest.fixture()
def api():
    """返回 (headers, created_ids, db);测试结束按 id 清理，避免污染其它用例。"""
    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    created: list[int] = []
    token, user_id, role_id = _make_token(db)
    db.commit()  # 让 get_current_user 的独立会话能查到该用户
    yield {"Authorization": f"Bearer {token}"}, created, db
    if created:
        db.query(Webhook).filter(Webhook.id.in_(created)).delete(
            synchronize_session=False
        )
    # 先删用户再删角色（users.role_id 指向 roles.id）
    db.query(User).filter(User.id == user_id).delete(synchronize_session=False)
    db.query(Role).filter(Role.id == role_id).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_create_feishu_app_defaults_to_auto_type_and_card_style(api):
    headers, created, db = api
    response = client.post(
        "/api/webhooks",
        headers=headers,
        json={
            "name": "API 冒烟-飞书私聊",
            "url": "https://open.feishu.cn",
            "provider": "feishu_app",
            "secret": "s3cret-smoke",
            "events": ["alert.created", "alert.remediation"],
            "config": _FEISHU_CONFIG,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    created.append(body["id"])

    # 界面不传 ID 类型与样式:落库默认 auto + card，未知键被丢弃，姓名只留有效项
    assert body["config"] == {
        "app_id": "cli_smoke",
        "receive_id_type": "auto",
        "receivers": ["ops@example.com", "ou_smoke1"],
        "receiver_names": {"ou_smoke1": "张三"},
        "style": "card",
    }
    # Secret 加密存储，接口只回显掩码
    assert body["secret_set"] is True and body["secret_preview"].endswith("oke")
    row = db.query(Webhook).filter(Webhook.id == body["id"]).first()
    assert decrypt(row.secret_enc) == "s3cret-smoke"


def test_create_feishu_app_requires_at_least_one_receiver(api):
    headers, _, _ = api
    response = client.post(
        "/api/webhooks",
        headers=headers,
        json={
            "name": "API 冒烟-缺接收人",
            "url": "https://open.feishu.cn",
            "provider": "feishu_app",
            "secret": "s3cret-smoke",
            "config": {"app_id": "cli_smoke", "receivers": []},
        },
    )
    assert response.status_code == 422
    assert "接收人" in response.json()["detail"]


def test_update_prunes_receiver_names_and_keeps_explicit_type(api):
    headers, created, _ = api
    body = client.post(
        "/api/webhooks",
        headers=headers,
        json={
            "name": "API 冒烟-更新",
            "url": "https://open.feishu.cn",
            "provider": "feishu_app",
            "secret": "s3cret-smoke",
            "config": _FEISHU_CONFIG,
        },
    ).json()
    created.append(body["id"])

    updated = client.put(
        f"/api/webhooks/{body['id']}",
        headers=headers,
        json={
            "config": {
                "app_id": "cli_smoke",
                "receivers": ["ou_smoke1"],
                "receiver_names": {"ou_smoke1": "张三", "ops@example.com": "值班号"},
                "receive_id_type": "open_id",
                "style": "text",
            }
        },
    ).json()

    # 邮箱接收人被移除后，它的姓名也一起清掉;API 显式指定的类型与样式仍然尊重
    assert updated["config"]["receivers"] == ["ou_smoke1"]
    assert updated["config"]["receiver_names"] == {"ou_smoke1": "张三"}
    assert updated["config"]["receive_id_type"] == "open_id"
    assert updated["config"]["style"] == "text"
    # Secret 未传时保持原值
    assert updated["secret_set"] is True


def test_non_feishu_provider_ignores_config(api):
    headers, created, _ = api
    body = client.post(
        "/api/webhooks",
        headers=headers,
        json={
            "name": "API 冒烟-通用",
            "url": "https://example.com/webhook",
            "provider": "generic",
            "events": ["alert.created"],
            "headers": {"X-Environment": "production"},
            "config": _FEISHU_CONFIG,
        },
    ).json()
    created.append(body["id"])
    assert body["config"] == {}
    assert body["headers"] == {"X-Environment": "production"}


_SCOPE_DENIED = "[40004] no dept authority error"


def _directory_payload(department_id="0", page_token=""):
    return {
        "app_id": "cli_smoke",
        "app_secret": "s3cret-smoke",
        "department_id": department_id,
        "page_token": page_token,
    }


def _denied():
    from app.services.feishu import FeishuError

    return FeishuError(_SCOPE_DENIED, code=40004, status=403)


def test_feishu_departments_falls_back_to_authorized_scope(api):
    """只授权了指定部门时(40004)，改用通讯录授权范围把被授权的部门列出来。"""
    headers, _, _ = api
    scope = {
        "department_ids": ["od_1"],
        "user_ids": ["ou_9"],
        "page_token": "pt-2",
        "has_more": True,
    }
    items = [
        {"open_department_id": "od_1", "name": "运维部", "parent_department_id": "0"}
    ]
    with (
        patch("app.services.feishu.list_departments", side_effect=_denied()),
        patch("app.services.feishu.list_authorized_scope", return_value=scope),
        patch(
            "app.services.feishu.get_departments_by_ids",
            return_value={"items": items, "name_scope_missing": True},
        ) as batch,
    ):
        response = client.post(
            "/api/webhooks/feishu/departments",
            headers=headers,
            json=_directory_payload(),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_limited"] is True and body["items"] == items
    # 字段级权限缺失的标记要透传，界面据此提示去补 contact:department.base:readonly
    assert body["name_scope_missing"] is True
    # 授权范围的分页透传给前端，方便继续翻
    assert (body["page_token"], body["has_more"]) == ("pt-2", True)
    assert batch.call_args.kwargs["department_ids"] == ["od_1"]


def test_feishu_users_falls_back_to_authorized_scope(api):
    """授权范围里「指定人」的场景:根层级直接列出被单独勾选的成员。"""
    headers, _, _ = api
    scope = {
        "department_ids": [],
        "user_ids": ["ou_9"],
        "page_token": "",
        "has_more": False,
    }
    users = [
        {
            "open_id": "ou_9",
            "user_id": "",
            "union_id": "",
            "name": "张三",
            "email": "z@x.com",
        }
    ]
    with (
        patch("app.services.feishu.list_users", side_effect=_denied()),
        patch("app.services.feishu.list_authorized_scope", return_value=scope),
        patch(
            "app.services.feishu.get_users_by_ids",
            return_value={"items": users, "name_scope_missing": False},
        ) as batch,
    ):
        response = client.post(
            "/api/webhooks/feishu/users", headers=headers, json=_directory_payload()
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_limited"] is True
    assert body["name_scope_missing"] is False
    assert [item["open_id"] for item in body["items"]] == ["ou_9"]
    assert body["items"][0]["name"] == "张三" and body["items"][0]["email"] == "z@x.com"
    assert batch.call_args.kwargs["user_ids"] == ["ou_9"]


def test_feishu_directory_unauthorized_child_returns_empty_page(api):
    """下钻进未授权的子部门时不报错，返回空列表让界面继续可用。"""
    headers, _, _ = api
    with (
        patch("app.services.feishu.list_departments", side_effect=_denied()),
        patch("app.services.feishu.list_authorized_scope") as scope,
    ):
        response = client.post(
            "/api/webhooks/feishu/departments",
            headers=headers,
            json=_directory_payload("od_1"),
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [],
        "page_token": "",
        "has_more": False,
        "scope_limited": True,
        "name_scope_missing": False,
    }
    scope.assert_not_called()


def test_feishu_departments_scope_fallback_failure_returns_hint(api):
    """连授权范围都读不到时，才把带操作路径的中文提示抛给界面。"""
    from app.services.feishu import FeishuError

    headers, _, _ = api
    with (
        patch("app.services.feishu.list_departments", side_effect=_denied()),
        patch(
            "app.services.feishu.list_authorized_scope",
            side_effect=FeishuError("[99991672] no permission", code=99991672),
        ),
    ):
        response = client.post(
            "/api/webhooks/feishu/departments",
            headers=headers,
            json=_directory_payload(),
        )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert (
        "40004" in detail and "通讯录权限范围" in detail and "admin.feishu.cn" in detail
    )
