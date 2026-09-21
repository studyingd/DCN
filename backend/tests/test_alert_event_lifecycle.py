"""告警事件的生命周期收尾:失去评估来源的事件不能永远挂着"未恢复"。

评估器只会恢复"本轮还覆盖得到"的事件。规则被停用/删除、对象被移出规则的监控范围、
或对象(设备 / PVE 平台 / 业务 / 容器)整行被删掉之后，遗留的 open 事件再也不会被
任何一轮评估访问到:告警中心一直显示未恢复，概览的 open_events / active_rules 被
死数据顶着，订阅方也等不到 alert.resolved。

反过来，"这轮没采到"或"对象暂时不可达"绝不能当成对象没了——采集失败的设备恰恰是
最需要保持告警的，所以下面也固定了几条不许误关的用例。
"""

import json
import time
import warnings
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.alert import AlertEvent, AlertRule
from app.models.business import Business
from app.models.device import Device
from app.models.device_container import DeviceContainer, DeviceDockerStatus
from app.models.maintenance_window import MaintenanceWindow
from app.models.pve_connection import PveConnection
from app.models.rack import Rack
from app.models.role import Role
from app.models.role_pve_guest_access import RolePveGuestAccess
from app.models.room import Room
from app.models.user import User
from app.models.webhook import Webhook
from app.services.alerts import (
    _PVE_SNAPSHOT_MAX_AGE,
    RULE_DELETED_NOTE,
    RULE_DISABLED_NOTE,
    TARGET_DELETED_NOTE,
    TARGET_OUT_OF_SCOPE_NOTE,
    _active_mute_window,
    _payload,
    close_events_of_disabled_rule,
    close_orphaned_alert_events,
    evaluate_alerts,
    notify_event,
    snooze_event,
)
from app.services.auth import create_access_token
from app.services.remediation import hold_created_notification

client = TestClient(app)


def _reload(db: Session, event_id: int) -> AlertEvent | None:
    """结束当前事务快照后重新读取(收尾在别的会话里提交)。"""
    db.commit()
    db.expire_all()
    return db.query(AlertEvent).filter(AlertEvent.id == event_id).first()


def _int_or_none_test(value) -> int | None:
    """迁移 0043 回填同口径的容错 int 解析(测试造行用)。"""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


class _SyncPool:
    """把 notify_event 的后台投递变成同步执行,测试不必等线程池。

    与 test_alert_remediation 同一实现(本地副本避免跨文件 fixture 耦合)。
    """

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        fut = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as exc:
            fut.set_exception(exc)
        return fut


def _wait_notify_drained():
    """_SyncPool 同步执行,无需等待;保留空函数让用例语义明确。"""


@pytest.fixture()
def env():
    """机房/机柜/设备/内存规则/管理员各一份，测试后按外键顺序清理。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    stamp = datetime.now(timezone.utc).timestamp()

    room = Room(name=f"OffRoom{stamp}", location="F1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name=f"OffRack{stamp}", type="cabinet")
    db.add(rack)
    db.flush()
    device = Device(
        rack_id=rack.id,
        name=f"off-srv-{stamp}",
        type="server",
        ip_address="10.8.0.1",
        status="online",
    )
    db.add(device)
    db.flush()
    # 第二台设备:用于"把对象从规则范围里移出去"这类用例
    other_device = Device(
        rack_id=rack.id,
        name=f"off-srv2-{stamp}",
        type="server",
        ip_address="10.8.0.2",
        status="online",
    )
    db.add(other_device)
    db.flush()
    rule = AlertRule(
        name=f"off-rule-{stamp}",
        metric="mem_pct",
        operator="gt",
        threshold=20.0,
        severity="warning",
        sustain_seconds=60,
        cooldown_seconds=300,
        enabled=True,
        target_device_ids=[device.id],
    )
    db.add(rule)
    db.flush()
    role = Role(
        name=f"off_role_{stamp}",
        # pve:view 供 PVE 虚机事件使用:device_id 为空的事件走 PVE 权限域校验
        # (见 _event_or_404)，缺它会 404，与设备类事件的可见性无关。
        permissions=json.dumps(["automation:manage", "pve:view"]),
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"offuser_{stamp}",
        password="irrelevant",
        role="admin",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.commit()
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=["automation:manage", "pve:view"],
        device_scope="all",
    )

    event_ids: list[int] = []
    extra_ids: dict[str, list[int]] = {
        "containers": [],
        "docker_status": [],
        "businesses": [],
        "connections": [],
        "windows": [],
        "webhooks": [],
        "devices": [],
    }

    def make_event(**overrides) -> AlertEvent:
        fields = {
            "rule_id": rule.id,
            "rule_name": rule.name,
            "device_id": device.id,
            "resource_type": "device",
            "resource_id": str(device.id),
            "resource_name": device.name,
            "metric": "mem_pct",
            "value": 96.0,
            "threshold": 20.0,
            "severity": "warning",
            "status": "open",
            "message": f"{device.name} 内存使用率 96.0% 已超过阈值 20.0%",
            "first_triggered_at": datetime.now(timezone.utc),
            "last_seen_at": datetime.now(timezone.utc),
            "occurrence_count": 3,
            "notification_status": "sent",
        }
        fields.update(overrides)
        event = AlertEvent(**fields)
        # 与迁移 0043 的回填同口径:直接造的事件行也填上宿主身份冗余列,
        # 否则 ACL 过滤(走索引的 owner_target_id IN)会把它们滤掉。
        if overrides.get("owner_target_id", "unset") == "unset":
            resource = _int_or_none_test(fields.get("resource_id"))
            event.owner_target_id = (
                fields.get("device_id")
                if fields.get("device_id") is not None
                else (
                    resource
                    if resource is not None and resource < 0
                    else _int_or_none_test(
                        (fields.get("remediation_json") or {}).get("target_id")
                    )
                )
            )
        db.add(event)
        db.commit()
        db.refresh(event)
        event_ids.append(event.id)
        return event

    # 用例里可能通过接口把规则/设备删掉，先把 id 记下来，teardown 不再碰 ORM 实例
    ids = {
        "rule": rule.id,
        "user": user.id,
        "role": role.id,
        "device": device.id,
        "other": other_device.id,
        "rack": rack.id,
        "room": room.id,
    }

    try:
        yield SimpleNamespace(
            db=db,
            room=room,
            rack=rack,
            device=device,
            other=other_device,
            rule=rule,
            make_event=make_event,
            headers={"Authorization": f"Bearer {token}"},
            extra=extra_ids,
        )
    finally:
        # 先回滚任何失败的 flush 再按 id 清理:否则 ORM 去同步已消失的实例会抛
        # ObjectDeletedError，teardown 中断后连接带着未提交事务回池，把下一个用例
        # 的写入锁到超时。
        db.rollback()
        try:
            if event_ids:
                db.query(AlertEvent).filter(AlertEvent.id.in_(event_ids)).delete(
                    synchronize_session=False
                )
            db.query(AlertEvent).filter(AlertEvent.rule_id == ids["rule"]).delete(
                synchronize_session=False
            )
            for key, model in (
                ("containers", DeviceContainer),
                ("docker_status", DeviceDockerStatus),
                ("businesses", Business),
                ("connections", PveConnection),
                ("windows", MaintenanceWindow),
                ("webhooks", Webhook),
                ("devices", Device),
            ):
                if extra_ids[key]:
                    db.query(model).filter(model.id.in_(extra_ids[key])).delete(
                        synchronize_session=False
                    )
            for key, model in (
                ("rule", AlertRule),
                ("user", User),
                ("role", Role),
                ("other", Device),
                ("device", Device),
                ("rack", Rack),
                ("room", Room),
            ):
                db.query(model).filter(model.id == ids[key]).delete(
                    synchronize_session=False
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def test_disable_rule_api_resolves_open_events(env):
    """接口停用规则 → open 事件置为已恢复，并推一条 alert.resolved。"""
    event = env.make_event()
    pushed: list[tuple[int, str]] = []

    with patch(
        "app.services.alerts.notify_event",
        side_effect=lambda event_id, name, **_kw: pushed.append((event_id, name)),
    ):
        response = client.put(
            f"/api/alerts/rules/{env.rule.id}",
            headers=env.headers,
            json={"enabled": False},
        )

    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    # 停用后规则上的"进行中告警"计数必须归零，不能继续顶着死数据
    assert response.json()["active_event_count"] == 0
    # 前端据此提示"同时关闭了 N 条进行中告警"，停用不再是静默动作
    assert response.json()["closed_event_count"] == 1
    assert pushed == [(event.id, "alert.resolved")]

    row = _reload(env.db, event.id)
    assert row.status == "resolved"
    assert row.resolved_at is not None
    assert row.message == (
        f"{env.device.name} 内存使用率 96.0% 已超过阈值 20.0%；{RULE_DISABLED_NOTE}"
    )


def test_disable_rule_deletes_pending_events(env):
    """持续时长还没满的 pending 事件从没对外触发过，直接删掉不留历史。"""
    event = env.make_event(status="pending", notification_status="pending")

    with patch("app.services.alerts.notify_event") as notify:
        # 返回值只统计对外可见的 open 事件，pending 从没触发过不算"关闭"
        assert close_events_of_disabled_rule(env.db, env.rule) == 0
    notify.assert_not_called()
    assert _reload(env.db, event.id) is None


def test_disable_rule_drops_held_notification(env):
    """扣住等归因、还没发出去的通知直接丢弃，别在停用后补一对 created/resolved。"""
    event = env.make_event(notification_status="pending")
    assert hold_created_notification(event.id) is True
    # hold_created_notification 在别的会话里提交，先结束当前快照才读得到扣住标记
    env.db.commit()
    env.db.expire_all()

    pushed: list[str] = []
    with patch(
        "app.services.alerts.notify_event",
        side_effect=lambda _id, name, **_kw: pushed.append(name),
    ):
        close_events_of_disabled_rule(env.db, env.rule)

    assert pushed == []
    row = _reload(env.db, event.id)
    assert row.status == "resolved"
    assert row.notification_status == "skipped"
    # 标记必须清掉，否则看门狗会在事件已 resolved 之后又把 alert.created 发出去
    assert (row.remediation_json or {}).get("notification_held") is None


def test_disable_rule_marks_force_closed_for_quiet_metrics(env):
    """强制收尾写入 force_closed:容器(cpu/mem/disk 之外的归因类)的
    resolved 卡在通知出口据此静默——规则停用不是真实恢复,两步卡片模型
    不该为它发"恢复"卡;host_status 等非归因类不受影响照常投递。"""
    from app.services.alerts import close_events_of_disabled_rule

    container_event = env.make_event(metric="container_status")
    host_event = env.make_event(metric="host_status")

    delivered: list[tuple] = []

    def _fake_notify(db, event, rule, device, event_name, *, track=True):
        delivered.append((event.id, event_name))

    # _notify_event_in_background 在后台线程池里执行,这里直接 patch 它的
    # 出口(_notify)断言投递结果与 force_closed 判定。
    import app.services.alerts as alerts_mod

    with (
        patch.object(alerts_mod, "_notify", side_effect=_fake_notify),
        patch.object(alerts_mod, "notify_event") as notify_dispatch,
    ):
        closed = close_events_of_disabled_rule(env.db, env.rule)
        assert closed == 2
        # 两条事件都派发了 resolved(host 照常,容器由出口判定静默)
        dispatch_names = [c.args[1] for c in notify_dispatch.call_args_list]
        assert dispatch_names == ["alert.resolved", "alert.resolved"]
        # 出口逐条跑一遍:host 投递,容器因 force_closed 被吞
        for call in notify_dispatch.call_args_list:
            event_id = call.args[0]
            with patch.object(alerts_mod, "_notify", side_effect=_fake_notify):
                alerts_mod._notify_event_in_background(event_id, "alert.resolved")

    delivered_ids = [eid for eid, _ in delivered]
    assert host_event.id in delivered_ids
    assert container_event.id not in delivered_ids  # force_closed 静默
    row = _reload(env.db, container_event.id)
    assert (row.remediation_json or {}).get("force_closed") is True
    assert row.notification_status == "sent"


def test_disable_rule_aborts_inflight_remediation(env):
    """处置还没收尾就停用规则，不能让"自动处置中"跟着事件一起僵死。"""
    base = f"{env.device.name} 内存使用率 96.0% 已超过阈值 20.0%"
    event = env.make_event(
        remediation_state="running",
        remediation_detail="正在下发启动指令…",
        remediation_json={"base_message": base, "state": "running", "attempts": 1},
    )

    with patch("app.services.alerts.notify_event"):
        close_events_of_disabled_rule(env.db, env.rule)

    row = _reload(env.db, event.id)
    assert row.status == "resolved"
    assert row.remediation_state == "skipped"
    assert row.remediation_detail is None
    assert row.message == f"{base}；{RULE_DISABLED_NOTE}"


def test_enable_rule_leaves_events_alone(env):
    """启用规则不是收尾动作，历史事件不能被误改。"""
    event = env.make_event(status="resolved", resolved_at=datetime.now(timezone.utc))
    env.rule.enabled = False
    env.db.commit()

    with patch("app.services.alerts.notify_event") as notify:
        response = client.put(
            f"/api/alerts/rules/{env.rule.id}",
            headers=env.headers,
            json={"enabled": True},
        )

    assert response.status_code == 200, response.text
    notify.assert_not_called()
    row = _reload(env.db, event.id)
    assert row.status == "resolved"
    assert RULE_DISABLED_NOTE not in row.message


def test_list_events_pagination_and_filters(env):
    """分页模式:带 page 返回 {items,total,page,page_size}+筛选+搜索都生效。

    旧调用(不带 page)仍是纯数组;带 page 时前端能翻页看 90 天历史,
    metric/时间窗口/关键字把检索范围收窄。
    """
    # 造三条不同 metric/时间的事件:旧的已恢复、新的未恢复。
    # 消息里嵌入唯一标记(规则 id 带时间戳),后续断首都在 q=marker 下隔离,
    # 不受开发库里其它历史事件行的干扰。
    marker = f"pgtest{env.rule.id}"
    old = env.make_event(
        metric="cpu_pct",
        status="resolved",
        resolved_at=datetime.now(timezone.utc) - timedelta(days=2),
        first_triggered_at=datetime.now(timezone.utc) - timedelta(days=3),
        last_seen_at=datetime.now(timezone.utc) - timedelta(days=2),
        message=f"off-srv CPU 使用率 95.0% {marker}",
    )
    recent = env.make_event(message=f"off-srv 内存 {marker}")  # mem_pct, open, 现在

    # 1) 旧调用兼容:纯数组
    response = client.get("/api/alerts/events", headers=env.headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)

    # 2) 分页模式:结构正确,标记隔离后 total=2
    response = client.get(
        "/api/alerts/events",
        headers=env.headers,
        params={"page": 1, "page_size": 10, "q": marker},
    )
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1 and body["page_size"] == 10
    ids = [row["id"] for row in body["items"]]
    assert {old.id, recent.id} == set(ids)

    # 3) metric 筛选
    response = client.get(
        "/api/alerts/events",
        headers=env.headers,
        params={"page": 1, "metric": "cpu_pct", "q": marker},
    )
    body = response.json()
    assert body["total"] == 1 and body["items"][0]["id"] == old.id

    # 4) 时间窗口:只看最近 1 天 → 只剩 recent
    response = client.get(
        "/api/alerts/events",
        headers=env.headers,
        params={
            "page": 1,
            "q": marker,
            "since": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        },
    )
    body = response.json()
    assert body["total"] == 1 and body["items"][0]["id"] == recent.id

    # 5) 关键字搜索:按消息内容命中 CPU 事件(子串跨词也行)
    response = client.get(
        "/api/alerts/events",
        headers=env.headers,
        params={"page": 1, "q": f"95.0% {marker}"},
    )
    body = response.json()
    assert body["total"] == 1 and body["items"][0]["id"] == old.id

    # 6) 分页翻页:page_size=1 时第二页是较旧那条(last_seen 倒序)
    response = client.get(
        "/api/alerts/events",
        headers=env.headers,
        params={"page": 2, "page_size": 1, "q": marker},
    )
    body = response.json()
    assert body["total"] == 2 and [row["id"] for row in body["items"]] == [old.id]


def test_startup_sweep_closes_orphaned_disabled_rule_events(env):
    """兜底:本次修复之前就已停用的规则，重启后同样要收敛。"""
    event = env.make_event()
    env.rule.enabled = False
    env.db.commit()

    pushed: list[str] = []
    with patch(
        "app.services.alerts.notify_event",
        side_effect=lambda _id, name, **_kw: pushed.append(name),
    ):
        assert close_orphaned_alert_events() >= 1

    assert "alert.resolved" in pushed
    assert _reload(env.db, event.id).status == "resolved"


def test_startup_sweep_ignores_enabled_rules(env):
    """启用中的规则交给评估器，兜底扫描不能抢着把它的活动告警关掉。"""
    event = env.make_event()

    with patch("app.services.alerts.notify_event") as notify:
        close_orphaned_alert_events()

    notify.assert_not_called()
    assert _reload(env.db, event.id).status == "open"


# ── 删除规则:配置可以删，运维留痕不能删 ──


def test_delete_rule_keeps_event_history(env):
    """删规则不等于删历史:事件保留、rule_id 置空、规则名走快照。

    以前 alert_events.rule_id 是 NOT NULL + ON DELETE CASCADE，删掉一条配错的规则
    会连带销毁它产生过的全部告警历史(什么时候告过警、AI 归因得出过什么结论)。
    """
    # 规则行被接口删掉后 ORM 实例就过期了，先把要用的值取出来
    rule_id = env.rule.id
    rule_name = env.rule.name
    event = env.make_event()
    event_id = event.id
    pushed: list[tuple[int, str]] = []

    with patch(
        "app.services.alerts.notify_event",
        side_effect=lambda ev_id, name, **_kw: pushed.append((ev_id, name)),
    ):
        response = client.delete(f"/api/alerts/rules/{rule_id}", headers=env.headers)

    assert response.status_code == 200, response.text
    assert response.json()["closed_event_count"] == 1
    assert "历史事件已保留" in response.json()["message"]
    assert pushed == [(event_id, "alert.resolved")]

    row = _reload(env.db, event_id)
    assert row is not None, "历史事件被 CASCADE 掉了"
    assert row.rule_id is None
    assert row.rule_name == rule_name
    assert row.status == "resolved"
    assert RULE_DELETED_NOTE in row.message
    assert env.db.query(AlertRule).filter(AlertRule.id == rule_id).first() is None


def test_delete_rule_deletes_pending_events(env):
    """还没真正触发过的 pending 事件不留历史，跟着规则一起清掉。"""
    event = env.make_event(status="pending", notification_status="pending")

    with patch("app.services.alerts.notify_event") as notify:
        response = client.delete(
            f"/api/alerts/rules/{env.rule.id}", headers=env.headers
        )

    assert response.status_code == 200, response.text
    assert response.json()["closed_event_count"] == 0
    notify.assert_not_called()
    assert _reload(env.db, event.id) is None


def test_event_apis_survive_deleted_rule(env):
    """规则删掉后列表/详情仍要查得到这段历史(以前是内连接，会整段消失)。"""
    rule_name = env.rule.name
    event = env.make_event()
    with patch("app.services.alerts.notify_event"):
        assert (
            client.delete(
                f"/api/alerts/rules/{env.rule.id}", headers=env.headers
            ).status_code
            == 200
        )

    listing = client.get("/api/alerts/events", headers=env.headers)
    assert listing.status_code == 200, listing.text
    row = next(item for item in listing.json() if item["id"] == event.id)
    assert row["rule_id"] is None
    assert row["rule_name"] == rule_name

    detail = client.get(f"/api/alerts/events/{event.id}", headers=env.headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["rule_name"] == rule_name


def test_payload_uses_rule_name_snapshot_when_rule_missing(env):
    """规则行已经没了，Webhook 载荷也要能说清是哪条规则触发的。"""
    event = env.make_event()
    payload = _payload(event, None, env.device, "alert.resolved")
    assert payload["alert"]["rule_name"] == env.rule.name
    assert payload["alert"]["rule_id"] == env.rule.id
    assert payload["rule_name"] == env.rule.name


# ── 对象被移出规则的监控范围 ──


def test_sweep_closes_event_when_target_removed_from_rule(env):
    """设备被移出 target_device_ids 后，它遗留的告警再也没人评估了。"""
    event = env.make_event()
    env.rule.target_device_ids = [env.other.id]
    env.db.commit()

    pushed: list[tuple[int, str]] = []
    with patch(
        "app.services.alerts.notify_event",
        side_effect=lambda event_id, name, **_kw: pushed.append((event_id, name)),
    ):
        assert close_orphaned_alert_events() >= 1

    assert (event.id, "alert.resolved") in pushed
    row = _reload(env.db, event.id)
    assert row.status == "resolved"
    assert TARGET_OUT_OF_SCOPE_NOTE in row.message


def test_update_rule_api_closes_events_when_scope_narrowed(env):
    """改规则范围时当场收尾，不必等下一个采集周期。"""
    event = env.make_event()

    with patch("app.services.alerts.notify_event"):
        response = client.put(
            f"/api/alerts/rules/{env.rule.id}",
            headers=env.headers,
            json={"target_device_ids": [env.other.id]},
        )

    assert response.status_code == 200, response.text
    assert response.json()["closed_event_count"] == 1
    assert response.json()["active_event_count"] == 0
    row = _reload(env.db, event.id)
    assert row.status == "resolved"
    assert TARGET_OUT_OF_SCOPE_NOTE in row.message


def test_update_rule_api_keeps_events_when_scope_unchanged(env):
    """只改阈值不动范围时，进行中的告警要留着继续跟踪。"""
    event = env.make_event()

    with patch("app.services.alerts.notify_event"):
        response = client.put(
            f"/api/alerts/rules/{env.rule.id}",
            headers=env.headers,
            json={"threshold": 30.0},
        )

    assert response.status_code == 200, response.text
    assert response.json()["closed_event_count"] == 0
    assert _reload(env.db, event.id).status == "open"


# ── 对象整行被删除 ──


def test_sweep_closes_event_of_deleted_device(env):
    """设备删除后 device_id 走 SET NULL，事件脱离评估范围，必须收尾。"""
    event = env.make_event()
    env.db.query(Device).filter(Device.id == env.device.id).delete()
    env.db.commit()

    pushed: list[tuple[int, str]] = []
    with patch(
        "app.services.alerts.notify_event",
        side_effect=lambda event_id, name, **_kw: pushed.append((event_id, name)),
    ):
        assert close_orphaned_alert_events() >= 1

    assert (event.id, "alert.resolved") in pushed
    row = _reload(env.db, event.id)
    assert row.status == "resolved"
    assert row.device_id is None
    assert TARGET_DELETED_NOTE in row.message


def test_sweep_keeps_event_when_device_merely_unreachable(env):
    """设备还在、只是这轮没采到 → 绝不能关告警。

    采集失败的设备恰恰是最需要保持告警的;"本轮没访问到"不等于"对象没了"，
    所以判定只看数据库里对象是否还存在。
    """
    event = env.make_event()
    env.device.status = "offline"
    env.db.commit()

    close_orphaned_alert_events()

    assert _reload(env.db, event.id).status == "open"


def _container_env(env, *, available: int):
    """把规则切成容器类，并造一条 Docker 探测状态(不含任何容器清单)。"""
    status = DeviceDockerStatus(
        device_id=env.device.id,
        available=available,
        container_count=0,
    )
    env.db.add(status)
    env.rule.metric = "container_status"
    env.rule.severity = "critical"
    env.rule.threshold = 0
    env.rule.target_device_ids = None
    env.db.commit()
    env.extra["docker_status"].append(status.id)
    return env.make_event(
        metric="container_status",
        value=1.0,
        threshold=0.0,
        severity="critical",
        resource_type="container",
        resource_id="abc123def456",
        resource_name=f"{env.device.name}/web-01",
        message="容器 web-01 当前状态异常：Exited (137) 5 minutes ago",
    )


def test_sweep_closes_container_event_when_container_gone(env):
    """Docker 探测成功、清单里却没有它 → 容器确实被删了，收尾。"""
    event = _container_env(env, available=1)

    with patch("app.services.alerts.notify_event"):
        assert close_orphaned_alert_events() >= 1

    row = _reload(env.db, event.id)
    assert row.status == "resolved"
    assert TARGET_DELETED_NOTE in row.message


def test_sweep_keeps_container_event_when_probe_failed(env):
    """容器清单每轮整表重写，探测失败时也会写空——一次 SSH 抖动不能误关告警。"""
    event = _container_env(env, available=0)

    close_orphaned_alert_events()

    assert _reload(env.db, event.id).status == "open"


def test_sweep_keeps_container_event_while_container_listed(env):
    """清单里还有它，就继续交给容器评估器判断恢复。"""
    event = _container_env(env, available=1)
    container = DeviceContainer(
        device_id=env.device.id,
        container_id="abc123def456",
        name="web-01",
        state="exited",
    )
    env.db.add(container)
    env.db.commit()
    env.extra["containers"].append(container.id)

    close_orphaned_alert_events()

    assert _reload(env.db, event.id).status == "open"


def test_sweep_closes_pve_guest_event_when_connection_deleted(env):
    """PVE 平台被删除后，它名下虚拟机的离线告警再也没人评估了。"""
    conn = PveConnection(
        name=f"off-pve-{env.rule.id}",
        host="10.8.0.9",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    env.db.add(conn)
    env.db.commit()
    env.extra["connections"].append(conn.id)
    target_id = -(conn.id * 1_000_000 + 101)
    env.rule.metric = "host_status"
    env.rule.severity = "critical"
    env.rule.threshold = 0
    env.rule.target_device_ids = None
    env.db.commit()
    event = env.make_event(
        metric="host_status",
        value=1.0,
        threshold=0.0,
        severity="critical",
        device_id=None,
        resource_id=str(target_id),
        resource_name=f"{conn.name}/web-01",
        message="虚拟机 web-01 当前离线或未运行",
    )

    env.db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
    env.db.commit()
    with patch("app.services.alerts.notify_event"):
        assert close_orphaned_alert_events() >= 1

    row = _reload(env.db, event.id)
    assert row.status == "resolved"
    assert TARGET_DELETED_NOTE in row.message


def _pve_guest_env(env, *, vmid: int = 101):
    """把规则切成主机状态类，造一条 PVE 连接和它名下一台虚机的离线告警。

    虚机没有本地 devices 行，事件的 resource_id 用负数编码
    ``-(connection_id * 1_000_000 + vmid)``，device_id 为空。
    """
    conn = PveConnection(
        name=f"off-pve-{env.rule.id}",
        host="10.8.0.9",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    env.db.add(conn)
    env.db.commit()
    env.extra["connections"].append(conn.id)
    env.rule.metric = "host_status"
    env.rule.severity = "critical"
    env.rule.threshold = 0
    env.rule.target_device_ids = None
    env.db.commit()
    event = env.make_event(
        metric="host_status",
        value=1.0,
        threshold=0.0,
        severity="critical",
        device_id=None,
        resource_id=str(-(conn.id * 1_000_000 + vmid)),
        resource_name=f"{conn.name}/web-01",
        message="虚拟机 web-01 当前离线或未运行",
    )
    return conn, event


def _pve_snapshot(
    *, reachable: bool = True, age: float = 0.0, vmids: tuple[int, ...] = ()
):
    """伪造 pve_guest_status 的进程内快照(采集循环在测试里不跑)。"""
    state = {
        "name": "off-pve",
        "reachable": reachable,
        "error": None,
        "checked_at": time.time() - age,
    }
    return (
        patch("app.services.alerts.get_connection_state", return_value=state),
        patch(
            "app.services.alerts.get_guests_by_connection",
            return_value={vmid: {"vmid": vmid} for vmid in vmids},
        ),
    )


def test_sweep_closes_pve_guest_event_when_guest_deleted(env):
    """平台还在、虚机已从 PVE 上销毁 → 它的离线告警再也没人评估了。

    这轮探测确实成功(快照 reachable 且新鲜)、清单里却没有这个 vmid，才敢判定没了。
    """
    _conn, event = _pve_guest_env(env, vmid=101)
    state_patch, guests_patch = _pve_snapshot(reachable=True, vmids=(200, 201))

    with state_patch, guests_patch, patch("app.services.alerts.notify_event"):
        assert close_orphaned_alert_events() >= 1

    row = _reload(env.db, event.id)
    assert row.status == "resolved"
    assert TARGET_DELETED_NOTE in row.message


def test_sweep_keeps_pve_guest_event_while_guest_still_listed(env):
    """虚机还在清单里(只是没运行)，继续交给主机状态评估器判断恢复。"""
    _conn, event = _pve_guest_env(env, vmid=101)
    state_patch, guests_patch = _pve_snapshot(reachable=True, vmids=(101,))

    with state_patch, guests_patch:
        close_orphaned_alert_events()

    assert _reload(env.db, event.id).status == "open"


def test_sweep_keeps_pve_guest_event_when_pve_unreachable(env):
    """PVE 连不上时清单为空，但"采不到"绝不等于"虚机没了"。"""
    _conn, event = _pve_guest_env(env, vmid=101)
    state_patch, guests_patch = _pve_snapshot(reachable=False, vmids=())

    with state_patch, guests_patch:
        close_orphaned_alert_events()

    assert _reload(env.db, event.id).status == "open"


def test_sweep_keeps_pve_guest_event_when_snapshot_stale(env):
    """快照太旧(刷新循环卡住)时同样不能当作证据。"""
    _conn, event = _pve_guest_env(env, vmid=101)
    state_patch, guests_patch = _pve_snapshot(
        reachable=True, age=_PVE_SNAPSHOT_MAX_AGE + 1, vmids=()
    )

    with state_patch, guests_patch:
        close_orphaned_alert_events()

    assert _reload(env.db, event.id).status == "open"


def test_sweep_keeps_pve_guest_event_when_never_scanned(env):
    """进程刚启动或本实例不是 leader，快照还没建起来 → 一律不动。"""
    _conn, event = _pve_guest_env(env, vmid=101)

    with (
        patch("app.services.alerts.get_connection_state", return_value=None),
        patch("app.services.alerts.get_guests_by_connection", return_value={}),
    ):
        close_orphaned_alert_events()

    assert _reload(env.db, event.id).status == "open"


def test_sweep_closes_business_event_when_business_deleted(env):
    """业务被删除后，它的状态告警同样失去评估来源。"""
    business = Business(name=f"off-biz-{env.rule.id}")
    env.db.add(business)
    env.db.commit()
    env.extra["businesses"].append(business.id)
    env.rule.metric = "business_status"
    env.rule.severity = "critical"
    env.rule.threshold = 0
    env.rule.target_business_ids = None
    env.db.commit()
    event = env.make_event(
        metric="business_status",
        value=1.0,
        threshold=0.0,
        severity="critical",
        device_id=None,
        resource_type="business",
        resource_id=str(business.id),
        resource_name=business.name,
        message=f"业务 {business.name} 状态异常",
    )

    env.db.query(Business).filter(Business.id == business.id).delete()
    env.db.commit()
    with patch("app.services.alerts.notify_event"):
        assert close_orphaned_alert_events() >= 1

    row = _reload(env.db, event.id)
    assert row.status == "resolved"
    assert TARGET_DELETED_NOTE in row.message


def test_sweep_closes_event_whose_rule_row_is_gone(env):
    """绕过接口直接改库删掉规则行(外键 SET NULL)，事件也要收尾。"""
    event = env.make_event()
    env.db.query(AlertEvent).filter(AlertEvent.id == event.id).update(
        {AlertEvent.rule_id: None}, synchronize_session=False
    )
    env.db.commit()

    with patch("app.services.alerts.notify_event"):
        assert close_orphaned_alert_events() >= 1

    assert _reload(env.db, event.id).status == "resolved"


# ── 事件接口:查询条数与空 device_id ──


def _count_sql(action) -> list[str]:
    """执行 action 期间发出的 SQL 语句列表。"""
    statements: list[str] = []

    def _before(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", _before)
    return statements


def _extra_device(env, index: int) -> Device:
    device = Device(
        rack_id=env.rack.id,
        name=f"{env.device.name}-x{index}",
        type="server",
        ip_address=f"10.8.9.{index}",
        status="online",
    )
    env.db.add(device)
    env.db.commit()
    env.extra["devices"].append(device.id)
    return device


@pytest.mark.parametrize("device_count", [2, 6])
def test_list_events_batches_device_lookup(env, device_count):
    """列表接口必须批量取设备，不能逐行 `db.get` (N+1)。

    设备数从 2 涨到 6 时，查 devices 的语句数必须保持不变(恒为 1 条批量查询)。
    修复前它是"每台不同设备一条"，limit=500 时最多能发出 500 条额外 SQL。
    """
    for index in range(device_count):
        device = _extra_device(env, index)
        env.make_event(
            device_id=device.id, resource_id=str(device.id), resource_name=device.name
        )

    statements = _count_sql(
        lambda: client.get("/api/alerts/events?limit=500", headers=env.headers)
    )
    device_queries = [s for s in statements if "FROM DEVICES" in s.upper()]
    assert len(device_queries) == 1, (
        f"{device_count} 台设备发出了 {len(device_queries)} 条 devices 查询，"
        "说明又退化成逐行加载了"
    )


def test_event_detail_survives_null_device_id(env):
    """PVE 虚机事件没有本地设备行，详情接口不能炸、也不能触发 SQLAlchemy 警告。

    `db.get(Device, None)` 目前返回 None 但会告警 "fully NULL primary key
    identity"，官方已声明未来版本会改成抛错，所以这里连警告一起钉住。
    """
    _conn, event_row = _pve_guest_env(env, vmid=101)
    assert event_row.device_id is None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        response = client.get(f"/api/alerts/events/{event_row.id}", headers=env.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["device_id"] is None
    # 设备名为空时回落到资源名，前端不至于显示空白
    assert body["device_name"] == event_row.resource_name
    null_pk = [str(w.message) for w in caught if "NULL primary key" in str(w.message)]
    assert not null_pk, f"触发了 SQLAlchemy 空主键警告: {null_pk}"


# ── ACL 可见性:owner_target_id 索引列替换 remediation_json LIKE 匹配 ──


def test_list_events_acl_by_owner_target_id(env):
    """受限角色(勾选虚拟机授权)按 owner_target_id 看见三类虚机事件。

    旧版对 remediation_json 做 cast(Text).like 匹配虚机容器事件,无法走索引;
    换 owner_target_id 列后可见性口径必须不变:同连接同 vmid 的
    主机状态(resource_id 负数)/指标(resource_id 负数)/容器(处置记录
    target_id)事件可见,别的虚机、设备、业务事件不可见。
    """
    conn, _host_event = _pve_guest_env(env, vmid=101)
    target_id = -(conn.id * 1_000_000 + 101)
    other_target = -(conn.id * 1_000_000 + 202)
    # 同一台虚机的容器事件:device_id=None,resource_id=容器 id,宿主身份在处置记录
    container_event = env.make_event(
        metric="container_status",
        value=1.0,
        threshold=0.0,
        severity="critical",
        device_id=None,
        resource_type="container",
        resource_id="5a2342ba3004",
        resource_name="[pve] web-01/redis",
        message="容器 redis 当前状态异常：Restarting",
        remediation_json={"target_id": target_id},
    )
    # 另一台虚机的事件:不可见
    other_event = env.make_event(
        metric="host_status",
        value=1.0,
        threshold=0.0,
        severity="critical",
        device_id=None,
        resource_id=str(other_target),
        resource_name=f"{conn.name}/db-01",
        message="虚拟机 db-01 当前离线或未运行",
    )
    # 业务事件:无宿主,受限角色不可见
    biz = Business(name=f"biz-{env.rule.id}")
    env.db.add(biz)
    env.db.flush()
    env.extra["businesses"].append(biz.id)
    biz_event = env.make_event(
        metric="business_status",
        value=1.0,
        threshold=0.0,
        severity="critical",
        device_id=None,
        resource_type="business",
        resource_id=str(biz.id),
        resource_name=biz.name,
        message="业务状态异常",
    )

    role = Role(
        name=f"guest_acl_{env.rule.id}",
        permissions=json.dumps(["automation:manage", "pve:view"]),
        device_scope="selected",
    )
    env.db.add(role)
    env.db.flush()
    env.db.add(
        RolePveGuestAccess(
            role_id=role.id,
            connection_id=conn.id,
            guest_type="qemu",
            vmid=101,
            guest_name="web-01",
        )
    )
    user = User(
        username=f"guestacl{env.rule.id}",
        password="irrelevant",
        role="admin",
        is_active=1,
        role_id=role.id,
    )
    env.db.add(user)
    env.db.commit()
    # 额外创建的角色/用户/授权行随 env.rule 的外键链一起清理(角色名带 rule id,
    # 这里按 id 记录到 extra 里手动清)
    extra_role_ids = {"role": [role.id], "user": [user.id]}
    try:
        token = create_access_token(
            user_id=user.id,
            username=user.username,
            role=user.role,
            permissions=["automation:manage", "pve:view"],
            device_scope="selected",
        )
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/api/alerts/events", headers=headers)
        assert response.status_code == 200, response.text
        visible = {row["id"] for row in response.json()}
        assert visible == {_host_event.id, container_event.id}
        assert other_event.id not in visible
        assert biz_event.id not in visible
        # 设备事件(另一台设备上的告警)也不可见:角色没勾任何设备
        device_event = env.make_event()
        response = client.get("/api/alerts/events", headers=headers)
        visible = {row["id"] for row in response.json()}
        assert device_event.id not in visible
    finally:
        env.db.rollback()
        for uid in extra_role_ids["user"]:
            env.db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        for rid in extra_role_ids["role"]:
            env.db.query(RolePveGuestAccess).filter(
                RolePveGuestAccess.role_id == rid
            ).delete(synchronize_session=False)
            env.db.query(Role).filter(Role.id == rid).delete(synchronize_session=False)
        env.db.commit()


# ── 性能指标告警: PVE 虚拟机目标 ──


def _pve_metric_env(env):
    """规则切成内存指标并指向一台虚拟机(负数 target_id)。"""
    conn = PveConnection(
        name=f"metric-pve-{env.rule.id}",
        host="10.8.0.9",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    env.db.add(conn)
    env.db.commit()
    env.extra["connections"].append(conn.id)
    env.rule.metric = "mem_pct"
    env.rule.threshold = 80.0
    env.rule.sustain_seconds = 0
    env.rule.target_device_ids = [-(conn.id * 1_000_000 + 101)]
    env.db.commit()
    return conn


def _metric_snapshot(conn, *, reachable=True, guests=None):
    """伪造 pve_guest_status 快照:guests 为 {vmid: {...}}。"""
    state = {
        "name": "metric-pve",
        "reachable": reachable,
        "error": None,
        "checked_at": time.time(),
    }
    return (
        patch("app.services.alerts.get_connection_state", return_value=state),
        patch(
            "app.services.alerts.get_guests_by_connection",
            return_value={vmid: g for vmid, g in (guests or {}).items()},
        ),
        patch("app.services.alerts.notify_event"),
        patch("app.services.alerts.sweep_expired_notification_holds"),
    )


def test_metric_alert_fires_for_pve_guest(env):
    """虚拟机内存超阈值 → 负数 target_id 事件正常产生并 open。"""
    conn = _pve_metric_env(env)
    target_id = -(conn.id * 1_000_000 + 101)
    guests = {
        101: {
            "vmid": 101,
            "name": "web-01",
            "status": "running",
            "cpu": 0.55,
            "mem": 18 * 1024**3,
            "maxmem": 20 * 1024**3,
        }
    }
    state_p, guests_p, notify_p, sweep_p = _metric_snapshot(conn, guests=guests)
    with (
        state_p,
        guests_p,
        notify_p,
        sweep_p,
        patch("app.services.alerts.submit_analysis"),
    ):
        evaluate_alerts({})

    # evaluate_alerts 在独立会话里提交;env.db 的事务快照(REPEATABLE READ)
    # 停在旧状态,先结束本会话事务再读
    env.db.commit()
    events = env.db.query(AlertEvent).filter(AlertEvent.rule_id == env.rule.id).all()
    assert len(events) == 1
    ev = events[0]
    assert ev.status == "open"  # sustain=0 直接 open
    assert ev.device_id is None
    assert int(ev.resource_id) == target_id
    assert ev.value == 90.0  # 18G/20G
    assert "web-01" in ev.message and "[metric-pve-" in ev.message


def test_metric_alert_recovers_when_guest_value_drops(env):
    """指标回落后事件恢复——虚拟机事件不能被 sweep 的范围判定误清。"""
    conn = _pve_metric_env(env)
    guests = {
        101: {
            "vmid": 101,
            "name": "web-01",
            "status": "running",
            "cpu": 0.1,
            "mem": 5 * 1024**3,
            "maxmem": 20 * 1024**3,
        }
    }
    event = env.make_event(
        metric="mem_pct",
        value=90.0,
        threshold=80.0,
        device_id=None,
        resource_id=str(-(conn.id * 1_000_000 + 101)),
        resource_name="[pve] web-01",
        message="[pve] web-01 内存使用率 90.0% 已超过阈值 80.0%",
    )
    state_p, guests_p, notify_p, sweep_p = _metric_snapshot(conn, guests=guests)
    with (
        state_p,
        guests_p,
        notify_p,
        sweep_p,
        patch("app.services.alerts.submit_analysis"),
    ):
        evaluate_alerts({})

    row = _reload(env.db, event.id)
    assert row.status == "resolved"


def test_metric_alert_scope_sweep_keeps_guest_event(env):
    """负数目标的事件:范围判定按 resource_id,不能因 device_id=None 误判出范围。"""
    conn = _pve_metric_env(env)
    event = env.make_event(
        metric="mem_pct",
        value=90.0,
        threshold=80.0,
        device_id=None,
        resource_id=str(-(conn.id * 1_000_000 + 101)),
        resource_name="[pve] web-01",
        message="[pve] web-01 内存使用率 90.0% 已超过阈值 80.0%",
    )
    guests = {101: {"vmid": 101}}
    state_p, guests_p, notify_p, sweep_p = _metric_snapshot(conn, guests=guests)
    with (
        state_p,
        guests_p,
        notify_p,
        sweep_p,
        patch("app.services.alerts.close_events_of_disabled_rule"),
    ):
        close_orphaned_alert_events()

    assert _reload(env.db, event.id).status == "open"


def test_resolved_webhook_suppressed_for_analysis_metrics(env):
    """归因类告警(cpu/mem/disk)整生命周期只发一张卡:alert.resolved 不再
    投递(触发卡已带归因,恢复卡只是重铺一遍);container_status 例外
    (2026-09-17):容器与主机同为两步卡片模型,恢复卡携带自动处置结论;
    host_status 等其它告警的恢复闭环不受影响。"""
    from app.services.alerts import _notify_event_in_background

    delivered: list[tuple] = []

    def _fake_notify(db, event, rule, device, event_name, *, track=True):
        delivered.append((event.id, event_name))

    metric_event = env.make_event(notification_status="pending")
    with patch("app.services.alerts._notify", side_effect=_fake_notify):
        _notify_event_in_background(metric_event.id, "alert.resolved")

    assert delivered == []  # mem_pct 的恢复卡被吞掉
    row = _reload(env.db, metric_event.id)
    # 状态标记为 sent,告警中心不能挂着「待通知」
    assert row.notification_status == "sent"
    assert row.last_notified_at is not None

    host_event = env.make_event(metric="host_status", notification_status="pending")
    with patch("app.services.alerts._notify", side_effect=_fake_notify):
        _notify_event_in_background(host_event.id, "alert.resolved")
    assert delivered == [(host_event.id, "alert.resolved")]

    # 容器恢复正常投递(拉起成功的闭环靠这张卡)
    container_event = env.make_event(
        metric="container_status", notification_status="pending"
    )
    with patch("app.services.alerts._notify", side_effect=_fake_notify):
        _notify_event_in_background(container_event.id, "alert.resolved")
    assert delivered == [
        (host_event.id, "alert.resolved"),
        (container_event.id, "alert.resolved"),
    ]

    # 但强制收尾(规则停用/删除,_close_event 写入 force_closed)仍静默

    env.db.refresh(container_event)
    record = dict(container_event.remediation_json or {})
    record["force_closed"] = True
    container_event.remediation_json = record
    env.db.commit()
    with patch("app.services.alerts._notify", side_effect=_fake_notify):
        _notify_event_in_background(container_event.id, "alert.resolved")
    assert delivered == [
        (host_event.id, "alert.resolved"),
        (container_event.id, "alert.resolved"),
    ]  # force_closed 的恢复卡没有新投递


# ── 已知晓(snooze):单运维告警降噪 ──


def test_ack_event_api_marks_and_is_idempotent(env):
    """站内「已知晓」:置标记+记录时间与认领人,重复点击保持首次。"""
    event = env.make_event()
    response = client.post(f"/api/alerts/events/{event.id}/ack", headers=env.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["snoozed"] is True
    assert body["snoozed_at"] is not None
    first_at = body["snoozed_at"]
    import time as _time

    _time.sleep(1.1)  # 让时间戳可区分
    response = client.post(f"/api/alerts/events/{event.id}/ack", headers=env.headers)
    assert response.status_code == 200
    row = _reload(env.db, event.id)
    assert row.snoozed is True
    # 幂等:时间戳保持首次
    assert row.snoozed_at.isoformat().startswith(first_at[:19])


def test_snoozed_event_suppresses_cooldown_repeat(env):
    """已知晓的事件冷却到期不再重发 alert.created;恢复照常推 resolved。"""
    event = env.make_event()
    snooze_event(event.id)
    env.db.commit()
    env.db.expire_all()
    # 冷却已过(阈值很小),再评估:指标仍超但不应产生新通知
    env.rule.cooldown_seconds = 0
    env.rule.target_device_ids = [env.device.id]
    env.db.commit()
    entries = {env.device.id: {"available": True, "mem_pct": 96.0}}
    pushed: list[tuple[int, str]] = []
    with patch(
        "app.services.alerts.notify_event",
        side_effect=lambda event_id, name, **_kw: pushed.append((event_id, name)),
    ):
        evaluate_alerts(entries)
    assert pushed == []
    row = _reload(env.db, event.id)
    assert row.status == "open"  # 评估照常,事件仍在
    assert row.occurrence_count >= 2  # 值仍在刷新

    # 指标回落 → 恢复通知不受 snooze 影响(闭环必须保留)
    entries = {env.device.id: {"available": True, "mem_pct": 10.0}}
    with patch(
        "app.services.alerts.notify_event",
        side_effect=lambda event_id, name, **_kw: pushed.append((event_id, name)),
    ):
        evaluate_alerts(entries)
    assert (event.id, "alert.resolved") in pushed


def _make_window(env, *, targets=None, start=None, end=None, name="变更窗口"):
    from app.models.maintenance_window import MaintenanceWindow

    window = MaintenanceWindow(
        name=f"{name}-{env.rule.id}",
        target_ids=targets,
        start_at=start or datetime.now(timezone.utc) - timedelta(minutes=5),
        end_at=end or datetime.now(timezone.utc) + timedelta(minutes=30),
        enabled=True,
    )
    env.db.add(window)
    env.db.commit()
    env.extra.setdefault("windows", []).append(window.id)
    return window


def test_mute_window_suppresses_notifications(env):
    """窗口内对象的通知在出口被拦:不投递、状态标 skipped、窗口计数 +1。"""
    event = env.make_event()
    window = _make_window(env, targets=[env.device.id])
    env.db.commit()
    env.db.expire_all()

    delivered: list[str] = []
    with (
        patch("app.services.alerts._notification_pool", _SyncPool()),
        patch(
            "app.services.alerts._deliver_one",
            side_effect=lambda w, p: (
                delivered.append(p["event"]) or {"webhook_id": w.id, "ok": True}
            ),
        ),
    ):
        notify_event(event.id, "alert.created")
        notify_event(event.id, "alert.resolved")
    assert delivered == []  # 一条都不投(连 resolved 一起静默)
    row = _reload(env.db, event.id)
    assert row.notification_status == "skipped"
    # 窗口计数累计(两次通知各 +1)
    env.db.commit()
    env.db.expire_all()
    w = (
        env.db.query(MaintenanceWindow)
        .filter(MaintenanceWindow.id == window.id)
        .first()
    )
    assert w.muted_count == 2


def test_mute_window_respects_targets_and_time(env):
    """目标外对象不受影响;窗口过期后同对象通知恢复。"""
    other = env.make_event(
        device_id=env.other.id,
        resource_id=str(env.other.id),
        resource_name=env.other.name,
        notification_status="pending",
        last_notified_at=None,
    )
    _make_window(env, targets=[env.device.id])
    env.db.commit()
    env.db.expire_all()

    delivered: list[str] = []
    with (
        patch("app.services.alerts._notification_pool", _SyncPool()),
        patch(
            "app.services.alerts._deliver_one",
            side_effect=lambda w, p: (
                delivered.append(p["event"]) or {"webhook_id": w.id, "ok": True}
            ),
        ),
    ):
        notify_event(other.id, "alert.created")
    assert delivered == ["alert.created"]  # 目标外照常投

    # 窗口已过期 → 原对象恢复通知(用第二台设备避免命中第一个活动窗口)
    _make_window(
        env,
        targets=[env.other.id],
        start=datetime.now(timezone.utc) - timedelta(hours=2),
        end=datetime.now(timezone.utc) - timedelta(hours=1),
        name="过期窗口",
    )
    env.db.commit()
    env.db.expire_all()
    event = env.make_event(
        device_id=env.other.id,
        resource_id=str(env.other.id),
        resource_name=env.other.name,
        notification_status="pending",
        last_notified_at=None,
    )
    delivered.clear()
    with (
        patch("app.services.alerts._notification_pool", _SyncPool()),
        patch(
            "app.services.alerts._deliver_one",
            side_effect=lambda w, p: (
                delivered.append(p["event"]) or {"webhook_id": w.id, "ok": True}
            ),
        ),
    ):
        notify_event(event.id, "alert.created")
    assert delivered == ["alert.created"]


def test_maintenance_window_apis(env):
    """窗口 CRUD:创建校验时间、列表 state、提前结束、删除恢复通知。"""

    # 结束时间已过去 → 400
    response = client.post(
        "/api/alerts/maintenance-windows",
        headers=env.headers,
        json={
            "name": "坏窗口",
            "target_ids": [env.device.id],
            "start_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "end_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 400

    # 正常创建(全部对象;start 回溯 5s 避开 DATETIME 秒级舍入把 now 舍到未来)
    response = client.post(
        "/api/alerts/maintenance-windows",
        headers=env.headers,
        json={
            "name": f"all-{env.rule.id}",
            "target_ids": None,
            "start_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    window_id = response.json()["id"]
    assert response.json()["state"] == "active"
    assert response.json()["target_label"] == "全部对象"
    env.extra.setdefault("windows", []).append(window_id)

    # 全部对象模式:任何事件命中
    event = env.make_event()
    env.db.expire_all()
    event = env.db.query(AlertEvent).filter(AlertEvent.id == event.id).first()
    assert _active_mute_window(env.db, event) is not None

    # 提前结束
    response = client.post(
        f"/api/alerts/maintenance-windows/{window_id}/finish", headers=env.headers
    )
    assert response.status_code == 200
    assert response.json()["state"] == "ended"
    # API 在另一会话提交;先结束本会话事务快照再读
    env.db.commit()
    env.db.expire_all()
    event = env.db.query(AlertEvent).filter(AlertEvent.id == event.id).first()
    assert _active_mute_window(env.db, event) is None

    # 删除
    response = client.delete(
        f"/api/alerts/maintenance-windows/{window_id}", headers=env.headers
    )
    assert response.status_code == 200


def test_maintenance_summary_sweep(env):
    """窗口结束 → 汇总卡推给订阅 alert.created 的钩子,窗口标记 summarized。"""
    from app.models.maintenance_window import MaintenanceWindow
    from app.services.alerts import sweep_maintenance_summaries

    hook = Webhook(
        name=f"mw-{env.rule.id}",
        url="https://example.invalid/mw",
        provider="generic",
        events=["alert.created"],
        headers={},
        enabled=True,
    )
    env.db.add(hook)
    window = _make_window(
        env,
        start=datetime.now(timezone.utc) - timedelta(hours=2),
        end=datetime.now(timezone.utc) - timedelta(minutes=1),
        name="已完成窗口",
    )
    window.muted_count = 3
    env.db.commit()
    env.extra.setdefault("webhooks", []).append(hook.id)

    delivered: list[dict] = []

    def _capture(w, payload):
        if w.id == hook.id:
            delivered.append(payload)
        return {"webhook_id": w.id, "name": w.name, "ok": True, "status_code": 200}

    with (
        patch("app.services.alerts._notification_pool", _SyncPool()),
        patch("app.services.alerts._deliver_one", side_effect=_capture),
    ):
        count = sweep_maintenance_summaries()
    assert count >= 1
    ours = next(p for p in delivered if "已完成窗口" in p["alert"]["rule_name"])
    assert "抑制了 3 次告警通知" in ours["alert"]["message"]
    # sweep 在独立会话提交;先结束本会话快照
    env.db.commit()
    env.db.expire_all()
    row = (
        env.db.query(MaintenanceWindow)
        .filter(MaintenanceWindow.id == window.id)
        .first()
    )
    assert row.summary_state == "summarized"
    # 幂等:已汇总的不再推
    with (
        patch("app.services.alerts._notification_pool", _SyncPool()),
        patch("app.services.alerts._deliver_one", side_effect=_capture),
    ):
        assert sweep_maintenance_summaries() == 0


# ── 聚合降噪:同轮同规则多条通知合并成一张卡 ──


def test_dispatch_aggregates_same_rule_group(env):
    """同轮内同规则的多条通知(非归因类)只投一张合并卡;组内事件标 sent。"""
    from app.services.alerts import _dispatch_notifications

    # 造 3 台设备 + 3 条同规则 host_status 事件
    devices = [_extra_device(env, i) for i in range(3)]
    env.rule.metric = "host_status"
    env.rule.severity = "critical"
    env.rule.threshold = 0
    env.rule.target_device_ids = None
    env.db.commit()
    events = [
        env.make_event(
            metric="host_status",
            value=1.0,
            threshold=0.0,
            severity="critical",
            device_id=d.id,
            resource_id=str(d.id),
            resource_name=d.name,
            message=f"主机 {d.name} 当前已离线或无法连接",
            notification_status="pending",
            last_notified_at=None,
        )
        for d in devices
    ]
    env.db.commit()

    delivered: list[dict] = []
    webhooks: dict[int, list] = {}

    def _capture(w, payload):
        webhooks.setdefault(w.id, []).append(payload)
        return {"webhook_id": w.id, "name": w.name, "ok": True, "status_code": 200}

    notifications = [(e.id, "alert.created") for e in events]
    with (
        patch("app.services.alerts._notification_pool", _SyncPool()),
        patch("app.services.alerts._deliver_one", side_effect=_capture),
        patch("app.services.alerts.notify_event") as single_notify,
    ):
        _dispatch_notifications(env.db, notifications)

    # 单事件路径一次都没走(全部被聚合)
    single_notify.assert_not_called()
    # 每个订阅 alert.created 的钩子只收到 1 张卡(聚合卡),消息含 3 个对象
    for hook_id, payloads in webhooks.items():
        cards = [p for p in payloads if p["event"] == "alert.created"]
        assert len(cards) == 1, f"hook {hook_id} 收到 {len(cards)} 张卡"
        assert "3 个对象" in cards[0]["alert"]["message"]
        assert devices[0].name in cards[0]["alert"]["message"]
    # 组内事件全部标记已发送
    for e in events:
        env.db.refresh(e)
        assert e.notification_status == "sent"

    # payload 细节:severity 取最高、时间取范围
    card = [p for p in webhooks[next(iter(webhooks))] if p["event"] == "alert.created"][
        0
    ]
    assert card["alert"]["severity"] == "critical"
    assert card["device"]["name"] == "3 个对象"


def test_dispatch_keeps_analysis_metrics_single(env):
    """归因类(cpu/mem/disk/container)不聚合:逐条走 notify_event(单卡片带结论模型)。"""
    from app.services.alerts import _dispatch_notifications

    events = [env.make_event() for _ in range(3)]  # mem_pct 默认
    env.db.commit()
    notifications = [(e.id, "alert.created") for e in events]
    with patch("app.services.alerts.notify_event") as single_notify:
        _dispatch_notifications(env.db, notifications)
    assert single_notify.call_count == 3
    # 通知状态不被聚合路径改写(保持事件原有值,交给单事件路径管理)
    for e in events:
        env.db.refresh(e)
        assert e.notification_status == "sent"


def test_aggregate_marks_are_committed(env):
    """聚合标记必须落库:卡发完后事件状态从 pending 变 sent(新会话可见)。

    修复前 _dispatch_notifications 在评估器 commit 之后改标记又从不提交,
    会话关闭即回滚——事件永远挂「待发送」,冷却重发条件(status != pending)
    永不满足,聚合过的事件等于意外永久静音。旧用例没抓到是因为造的行初始
    恰好就是 sent,断言同值等于没测;这里初始造 pending(与评估器真实口径一致)。
    """
    from app.services.alerts import _dispatch_notifications

    devices = [_extra_device(env, i) for i in range(3)]
    env.rule.metric = "host_status"
    env.rule.severity = "critical"
    env.rule.threshold = 0
    env.rule.target_device_ids = None
    env.db.commit()
    events = [
        env.make_event(
            metric="host_status",
            value=1.0,
            threshold=0.0,
            severity="critical",
            notification_status="pending",
            device_id=d.id,
            resource_id=str(d.id),
            resource_name=d.name,
            message=f"主机 {d.name} 当前已离线或无法连接",
        )
        for d in devices
    ]
    env.db.commit()

    with (
        patch("app.services.alerts._notification_pool", _SyncPool()),
        patch(
            "app.services.alerts._deliver_one",
            side_effect=lambda w, p: {
                "webhook_id": w.id,
                "ok": True,
                "status_code": 200,
            },
        ),
        patch("app.services.alerts.notify_event"),
    ):
        _dispatch_notifications(env.db, [(e.id, "alert.created") for e in events])

    # 新会话读取(绕开本会话缓存),证明标记真的提交了
    db2 = SessionLocal()
    try:
        for e in events:
            row = db2.query(AlertEvent).filter(AlertEvent.id == e.id).first()
            assert row.notification_status == "sent", (
                f"事件 {e.id} 状态仍为 {row.notification_status},聚合标记未落库"
            )
            assert row.last_notified_at is not None
    finally:
        db2.close()


def test_aggregate_respects_mute_window(env):
    """静默窗口对聚合卡同样生效:命中的事件被拦(标 skipped+计数),不再参与聚合。

    修复前聚合路径直接走 _deliver_one、不经过单事件出口的静默检查——维护期
    多条同时触发的告警会聚成一张卡穿透静默,比不聚合更错。
    """
    from app.services.alerts import _dispatch_notifications

    devices = [_extra_device(env, i) for i in range(3)]
    env.rule.metric = "host_status"
    env.rule.severity = "critical"
    env.rule.threshold = 0
    env.rule.target_device_ids = None
    env.db.commit()
    # 全部对象的活动静默窗口
    _make_window(env, targets=None)
    events = [
        env.make_event(
            metric="host_status",
            value=1.0,
            threshold=0.0,
            severity="critical",
            notification_status="pending",
            device_id=d.id,
            resource_id=str(d.id),
            resource_name=d.name,
            message=f"主机 {d.name} 当前已离线或无法连接",
        )
        for d in devices
    ]
    env.db.commit()

    delivered: list = []
    with (
        patch("app.services.alerts._notification_pool", _SyncPool()),
        patch(
            "app.services.alerts._deliver_one",
            side_effect=lambda w, p: (
                delivered.append(p) or {"webhook_id": w.id, "ok": True}
            ),
        ),
        patch("app.services.alerts.notify_event"),
    ):
        _dispatch_notifications(env.db, [(e.id, "alert.created") for e in events])

    assert delivered == []  # 一张聚合卡也不发
    env.db.commit()
    env.db.expire_all()
    for e in events:
        row = env.db.query(AlertEvent).filter(AlertEvent.id == e.id).first()
        assert row.notification_status == "skipped"
    window = (
        env.db.query(MaintenanceWindow).order_by(MaintenanceWindow.id.desc()).first()
    )
    assert window.muted_count >= 3  # 每个被拦的事件都计数


def test_cross_instance_notification_dedup(env):
    """跨实例去重:双实例竞态时只有占位成功者投递,另一实例直接放弃。

    复现线上竞态:实例 A 通知后在后台线程写回状态之前,实例 B 的评估轮
    读到「待通知」再发一次(2026-09-17 实测连续两条相同告警卡)。占位用
    条件 UPDATE(pending→sent)原子完成,后到者必败。
    """
    from app.services.alerts import _claim_notification

    # 评估器入队前总把状态置回 pending(待通知)——这是占位的唯一信号
    event = env.make_event(notification_status="pending", last_notified_at=None)
    env.db.commit()
    # 模拟两个实例的发送线程先后抢占:先到者成功,后到者失败
    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        assert _claim_notification(db_a, event.id) is True
        assert _claim_notification(db_b, event.id) is False
        # 已占位(sent)后,后续任何占位都失败(等效于重发被拦)
        assert _claim_notification(db_a, event.id) is False
    finally:
        db_a.close()
        db_b.close()
    # 冷却到期允许重发:评估器把状态置回 pending 再入队 → 占位成功
    env.db.commit()
    env.db.query(AlertEvent).filter(AlertEvent.id == event.id).update(
        {AlertEvent.notification_status: "pending"}
    )
    env.db.commit()
    db_c = SessionLocal()
    try:
        assert _claim_notification(db_c, event.id) is True
    finally:
        db_c.close()

    # 端到端:notify_event 在另一实例「刚发过」(占位已写 sent)后到达 → 不投递
    delivered: list[str] = []
    with (
        patch("app.services.alerts._notification_pool", _SyncPool()),
        patch(
            "app.services.alerts._deliver_one",
            side_effect=lambda w, p: (
                delivered.append(p["event"]) or {"webhook_id": w.id, "ok": True}
            ),
        ),
    ):
        notify_event(event.id, "alert.created")
    assert delivered == []


def test_resolved_notification_not_blocked_by_created_cooldown(env):
    """回归(2026-09-18):alert.resolved 是一次性生命周期通知,不能被
    created 卡的冷却占位吞掉。线上实测:created 发出 4.5 分钟后事件恢复,
    规则冷却 5 分钟,恢复卡被旧的时间戳占位条件拦下,状态永远挂 pending,
    用户"拉起成功后收不到恢复消息"。"""
    from app.services.alerts import _notify_event_in_background

    # 模拟 created 卡刚投递完:状态 sent,4.5 分钟前通知过
    event = env.make_event(
        metric="host_status",
        notification_status="sent",
        last_notified_at=datetime.now(timezone.utc) - timedelta(seconds=270),
    )
    env.db.commit()
    # 评估器发现恢复:置 pending 入队 resolved(冷却 300s 远未满)
    env.db.query(AlertEvent).filter(AlertEvent.id == event.id).update(
        {"notification_status": "pending"}
    )
    env.db.commit()

    delivered: list[tuple] = []

    def _fake_notify(db, event, rule, device, event_name, *, track=True):
        delivered.append((event.id, event_name))

    with patch("app.services.alerts._notify", side_effect=_fake_notify):
        _notify_event_in_background(event.id, "alert.resolved")

    # 恢复卡必须投递,且状态收敛为 sent
    assert delivered == [(event.id, "alert.resolved")]
    row = _reload(env.db, event.id)
    assert row.notification_status == "sent"
    assert row.last_notified_at is not None


def test_business_rule_accepts_interface_only_business(env):
    """回归(2026-09-18):业务的合法成员有服务器/接口/PVE 虚机三类,保存
    business_status 规则的存在性校验按 Business 本体判定——只关联接口
    (或只关联虚机)的业务不再被误报「不存在的业务」(旧实现查
    business_servers 反查,纯接口/虚机业务 404)。"""
    from app.models.business import BusinessInterface
    from app.models.service_interface import ServiceInterface

    biz = Business(name=f"iface-biz-{env.rule.id}")
    env.db.add(biz)
    env.db.flush()
    iface = ServiceInterface(
        name=f"probe-{env.rule.id}", url="http://192.0.2.1/health", enabled=1
    )
    env.db.add(iface)
    env.db.flush()
    env.db.add(BusinessInterface(business_id=biz.id, interface_id=iface.id))
    env.db.commit()
    env.extra["businesses"].append(biz.id)
    env.extra.setdefault("interfaces", []).append(iface.id)

    # 管理员(全量范围)保存指向该业务的规则:应成功
    response = client.post(
        "/api/alerts/rules",
        headers=env.headers,
        json={
            "name": f"iface-rule-{env.rule.id}",
            "metric": "business_status",
            "severity": "critical",
            "threshold": 0,
            "operator": "gt",
            "sustain_seconds": 60,
            "cooldown_seconds": 300,
            "target_business_ids": [biz.id],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target_business_ids"] == [biz.id]
    env.db.query(AlertRule).filter(AlertRule.id == body["id"]).delete()
    env.db.commit()

    # 候选接口:纯接口业务也要出现在可选列表里(与业务列表可见性同口径)
    response = client.get("/api/alerts/businesses", headers=env.headers)
    assert response.status_code == 200
    assert biz.id in {item["business_id"] for item in response.json()}

    # 真不存在的业务仍要 404(存在性口径收紧为 Business 本体,不放松)
    response = client.post(
        "/api/alerts/rules",
        headers=env.headers,
        json={
            "name": f"ghost-rule-{env.rule.id}",
            "metric": "business_status",
            "severity": "critical",
            "threshold": 0,
            "operator": "gt",
            "sustain_seconds": 60,
            "cooldown_seconds": 300,
            "target_business_ids": [99999999],
        },
    )
    assert response.status_code == 404
    assert "不存在的业务" in response.json()["detail"]
