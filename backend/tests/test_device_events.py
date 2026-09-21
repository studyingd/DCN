"""自动化通知(automation.notify)的订阅判定与存量订阅迁移。

这个事件合并了过去三个(inspection.report / inspection.failed / job.failed),
并接替了已撤销的 device.offline / device.online。派发已收敛为**只有健康巡检**
经 inspection_report 发射,本模块只保留订阅判定与迁移,这里钉住两件事:

  1. ``is_automation_notify_subscribed`` 认可新事件名与未迁移的旧订阅名;
  2. 启动时的 ``migrate_event_names`` 把存量旧事件名归一化(幂等),
     否则老订阅会指向已不存在的事件、永远收不到。
"""

import pytest

from app.services import device_events


@pytest.fixture(autouse=True)
def _clean_webhooks():
    """只清理本用例创建的 webhook 行。

    3307 开发库里有真实在用的 webhook(飞书通知等)，旧实现 teardown 全表
    delete，每跑一次门禁就把用户配置清掉(2026-09-15 真踩过:修完 bug 跑全
    量测试后飞书卡片停发)。自增 id 只增不减，测试前快照当前最大 id，
    清理时只删比它大的行。
    """
    from sqlalchemy import func

    from app.database import Base, engine

    Base.metadata.create_all(bind=engine)
    from app.database import SessionLocal
    from app.models.webhook import Webhook

    db = SessionLocal()
    try:
        cutoff = db.query(func.max(Webhook.id)).scalar() or 0
    finally:
        db.close()
    yield
    db = SessionLocal()
    try:
        db.query(Webhook).filter(Webhook.id > cutoff).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _make_webhook(events, name="hook", enabled=True):
    from app.database import SessionLocal
    from app.models.webhook import Webhook

    db = SessionLocal()
    hook = Webhook(
        name=name,
        url="https://example.com/hook",
        provider="generic",
        events=events,
        enabled=enabled,
    )
    db.add(hook)
    db.commit()
    db.refresh(hook)
    hook_id = hook.id
    db.close()
    return hook_id


# ── 订阅判定 ──


def test_subscription_matches_new_event_name():
    assert device_events.is_automation_notify_subscribed(["automation.notify"]) is True
    assert device_events.is_automation_notify_subscribed(["alert.created"]) is False
    assert device_events.is_automation_notify_subscribed([]) is False
    assert device_events.is_automation_notify_subscribed(None) is False


def test_subscription_matches_legacy_unmigrated_names():
    """迁移未跑时,存量旧订阅名(inspection.failed / job.failed 等)也要算数。"""
    assert device_events.is_automation_notify_subscribed(["inspection.failed"]) is True
    assert device_events.is_automation_notify_subscribed(["job.failed"]) is True
    assert device_events.is_automation_notify_subscribed(["inspection.report"]) is True


def test_subscription_ignores_removed_device_events():
    """device.offline/online 的发射点已撤,残留订阅不应再被认可。"""
    assert device_events.is_automation_notify_subscribed(["device.offline"]) is False
    assert device_events.is_automation_notify_subscribed(["device.online"]) is False


# ── 存量订阅迁移 ──


def test_migrate_merges_legacy_events_into_automation_notify():
    hook_id = _make_webhook(
        ["alert.created", "inspection.report", "inspection.failed", "job.failed"]
    )
    db = _session()
    try:
        changed = device_events.migrate_event_names(db)
        assert changed == 1
        db.expire_all()
        hook = _get(db, hook_id)
        # 三个旧事件合并成一个，alert.created 保留
        assert hook.events == ["alert.created", "automation.notify"]
    finally:
        db.close()


def test_migrate_drops_removed_device_events():
    hook_id = _make_webhook(["device.offline", "device.online", "alert.resolved"])
    db = _session()
    try:
        device_events.migrate_event_names(db)
        db.expire_all()
        hook = _get(db, hook_id)
        # 发射点已撤销，订阅一并清掉；alert.resolved 保留
        assert hook.events == ["alert.resolved"]
    finally:
        db.close()


def test_migrate_is_idempotent():
    _make_webhook(["inspection.report", "job.failed"])
    db = _session()
    try:
        assert device_events.migrate_event_names(db) == 1
        assert device_events.migrate_event_names(db) == 0  # 第二次无改动
    finally:
        db.close()


def test_migrate_leaves_unrelated_webhooks_alone():
    hook_id = _make_webhook(["alert.created", "alert.resolved"])
    db = _session()
    try:
        assert device_events.migrate_event_names(db) == 0
        db.expire_all()
        assert _get(db, hook_id).events == ["alert.created", "alert.resolved"]
    finally:
        db.close()


def _session():
    from app.database import SessionLocal

    return SessionLocal()


def _get(db, hook_id):
    from app.models.webhook import Webhook

    return db.query(Webhook).filter(Webhook.id == hook_id).first()
