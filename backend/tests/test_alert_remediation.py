"""告警自愈测试:离线虚拟机/容器自动拉起 + 指标过高的 Agent 归因分析。"""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.config import (
    ALERT_ANALYSIS_HOLD_TIMEOUT,
    ALERT_REMEDIATION_MAX_ATTEMPTS,
)
from app.database import Base, SessionLocal, engine
from app.models.agent_run import AgentRun
from app.models.alert import AlertEvent, AlertRule
from app.models.device import Device
from app.models.device_container import ContainerAction, DeviceContainer
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.models.rack import Rack
from app.models.room import Room
from app.models.webhook import Webhook
from app.services import remediation
from app.services.agent import INTERRUPTED_RUN_ERROR, AgentTarget
from app.services.alerts import (
    _evaluate_resource_rules,
    _notify,
    evaluate_alerts,
)
from app.services.remediation import (
    analyze_event,
    apply_recovered,
    check_analysis,
    check_remediation,
    compose_message,
    recover_orphaned_analyses,
    release_held_notification,
    remediate_event,
    request_analysis,
    summarize,
    sweep_expired_notification_holds,
)

BASE_MESSAGE = "主机 web-01 当前已离线或无法连接"
PVE_ID_FACTOR = 1_000_000


class _FastTime:
    """把后台确认恢复的轮询 sleep 变成空操作，测试无需真等。"""

    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(lambda *_args: None)


def _reload(db: Session, event_id: int) -> AlertEvent:
    """结束当前事务快照后重新读取(后台线程在别的会话里提交)。"""
    db.commit()
    db.expire_all()
    return db.query(AlertEvent).filter(AlertEvent.id == event_id).first()


@pytest.fixture()
def env(monkeypatch):
    """机房/机柜/设备/PVE 平台/告警规则各一份，测试后按外键顺序清理。"""
    # 本文件用例全部按代码默认「自动拉起开启」编写;真实部署的 .env 可能
    # 为环境需要关掉它(ALERT_AUTO_REMEDIATION=false),不显式钉住会随
    # 部署配置漂红(同 get_agent_config 闸门用例的先例)。alerts 与
    # remediation 两处模块级各持一份副本,都要钉。
    monkeypatch.setattr("app.services.alerts.ALERT_AUTO_REMEDIATION", True)
    monkeypatch.setattr("app.services.remediation.ALERT_AUTO_REMEDIATION", True)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    stamp = datetime.now(timezone.utc).timestamp()

    room = Room(name=f"HealRoom{stamp}", location="F1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name=f"HealRack{stamp}", type="cabinet")
    db.add(rack)
    db.flush()
    device = Device(
        rack_id=rack.id,
        name=f"heal-srv-{stamp}",
        type="server",
        ip_address="10.7.0.1",
        status="offline",
    )
    db.add(device)
    db.flush()
    conn = PveConnection(
        name=f"heal-pve-{stamp}",
        host="10.7.0.9",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.flush()
    rule = AlertRule(
        name=f"heal-rule-{stamp}",
        metric="host_status",
        operator="gt",
        threshold=0,
        severity="critical",
        sustain_seconds=0,
        cooldown_seconds=0,
    )
    db.add(rule)
    db.flush()
    db.commit()

    event_ids: list[int] = []
    extra_ids: dict[str, list[int]] = {
        "bindings": [],
        "containers": [],
        "runs": [],
        "audits": [],
        "webhooks": [],
    }

    def make_event(**overrides) -> AlertEvent:
        fields = {
            "rule_id": rule.id,
            "device_id": None,
            "resource_type": "device",
            "resource_id": str(-(conn.id * PVE_ID_FACTOR + 101)),
            "resource_name": "web-01",
            "metric": "host_status",
            "value": 1.0,
            "threshold": 0.0,
            "severity": "critical",
            "status": "open",
            "message": BASE_MESSAGE,
            "first_triggered_at": datetime.now(timezone.utc),
            "last_seen_at": datetime.now(timezone.utc),
            "occurrence_count": 1,
            "notification_status": "skipped",
        }
        fields.update(overrides)
        event = AlertEvent(**fields)
        db.add(event)
        # 诊断钩子:FK 失败时打印事务现场(flaky 定位用,抓到现场后移除)
        try:
            db.commit()
        except Exception as exc:
            import sys

            _diag = (
                f"DIAG make_event failed: {exc}\n"
                f"  device_id={fields.get('device_id')} "
                f"env.device.id={getattr(device, 'id', '?')}\n"
                f"  session.new={[type(o).__name__ for o in db.new]}\n"
                f"  session.dirty={[type(o).__name__ for o in db.dirty]}\n"
                f"  device in session: {device in db}\n"
            )
            print(_diag, file=sys.stderr)
            raise
        db.refresh(event)
        event_ids.append(event.id)
        return event

    try:
        yield SimpleNamespace(
            db=db,
            room=room,
            rack=rack,
            device=device,
            conn=conn,
            rule=rule,
            make_event=make_event,
            extra=extra_ids,
            stamp=stamp,
        )
    finally:
        if extra_ids["webhooks"]:
            db.query(Webhook).filter(Webhook.id.in_(extra_ids["webhooks"])).delete(
                synchronize_session=False
            )
        if extra_ids["audits"]:
            db.query(ContainerAction).filter(
                ContainerAction.id.in_(extra_ids["audits"])
            ).delete(synchronize_session=False)
        if extra_ids["runs"]:
            db.query(AgentRun).filter(AgentRun.id.in_(extra_ids["runs"])).delete(
                synchronize_session=False
            )
        if extra_ids["containers"]:
            db.query(DeviceContainer).filter(
                DeviceContainer.id.in_(extra_ids["containers"])
            ).delete(synchronize_session=False)
        if extra_ids["bindings"]:
            db.query(PveGuestBinding).filter(
                PveGuestBinding.id.in_(extra_ids["bindings"])
            ).delete(synchronize_session=False)
        if event_ids:
            db.query(AlertEvent).filter(AlertEvent.id.in_(event_ids)).delete(
                synchronize_session=False
            )
        # 服务层(evaluate_host_status_alerts / evaluate_alerts)会自己开 session
        # 建事件，不会登记进 event_ids。必须在删规则**之前**按 rule_id 兜底清一遍：
        # alert_events.rule_id 是 ON DELETE SET NULL，规则一删这些事件就变成
        # 谁也找不到的孤儿，永远留在测试库里。
        db.query(AlertEvent).filter(AlertEvent.rule_id == rule.id).delete(
            synchronize_session=False
        )
        db.query(AlertRule).filter(AlertRule.id == rule.id).delete()
        db.query(PveConnection).filter(PveConnection.id == conn.id).delete()
        db.query(Device).filter(Device.id == device.id).delete()
        db.query(Rack).filter(Rack.id == rack.id).delete()
        db.query(Room).filter(Room.id == room.id).delete()
        db.commit()
        db.close()


def _guest(
    vmid=101, node="pve1", gtype="qemu", name="web-01", status="stopped", **extra
):
    return {
        "vmid": vmid,
        "node": node,
        "type": gtype,
        "name": name,
        "status": status,
        **extra,
    }


# ── 文案组装(纯函数) ──


AGENT_REPORT = """## 结论
内存占用主要来自 java(pid 1234) 常驻 88%，疑似堆未回收，置信度：高。

## 判断依据
- top_processes_mem: java RSS 14.2G / 16G

## 建议
人工确认后重启该服务(需人工执行)。"""


def test_summarize_takes_conclusion_section_body():
    """ "## 结论"是标题不是结论，摘要必须取小节正文。"""
    assert summarize(AGENT_REPORT) == (
        "内存占用主要来自 java(pid 1234) 常驻 88%，疑似堆未回收，置信度：高。"
    )
    assert (
        summarize("# 结论\n\nCPU 被 mysql 吃满\n第二行") == "CPU 被 mysql 吃满；第二行"
    )
    assert summarize("") == ""
    assert summarize(None) == ""
    assert len(summarize("x" * 400, limit=160)) == 161  # 160 + 省略号


def test_summarize_falls_back_to_first_body_line():
    """没有结论小节时退回第一段正文，并跳过标题与分隔线。"""
    assert summarize("## 排查过程\n---\n**mysql** 占用 92% CPU") == "mysql 占用 92% CPU"
    assert (
        summarize("纯文本报告，没有 Markdown 结构") == "纯文本报告，没有 Markdown 结构"
    )


def test_compose_message_joins_progress():
    assert compose_message("A", None, "", None) == "A"
    assert compose_message("A", "正在启动…", "", None) == "A；正在启动…"
    assert compose_message("A", None, "running", None) == "A"
    assert compose_message("A", "处置成功", "completed", "mysql 占用过高") == (
        "A；处置成功；AI 归因：mysql 占用过高"
    )


# ── PVE 虚拟机自动拉起 ──


def test_remediate_starts_offline_pve_guest(env):
    event = env.make_event()
    calls: list[tuple] = []
    client = SimpleNamespace(
        list_guest_resources=lambda force=False: [_guest()],
        power=lambda node, gtype, vmid, action: calls.append(
            (node, gtype, vmid, action)
        ),
        guest_status=lambda node, gtype, vmid: {"status": "running"},
    )
    with (
        patch("app.services.remediation.build_client", return_value=client),
        patch("app.services.remediation.time", _FastTime),
    ):
        result = remediate_event(event.id, manual=True)

    assert result["ok"] is True
    assert calls == [("pve1", "qemu", 101, "start")]
    row = _reload(env.db, event.id)
    assert row.remediation_state == "succeeded"
    assert row.message == f"{BASE_MESSAGE}；自动拉起操作成功，目标已恢复运行"
    assert row.remediation_json["target"].startswith("PVE 虚拟机")
    assert row.remediation_json["attempts"] == 1


def test_remediate_skips_pve_template(env):
    """模板机是克隆源，自愈不能去启动它(历史规则里可能残留这种目标)。"""
    event = env.make_event(resource_name="tpl-ubuntu")
    calls: list[tuple] = []
    client = SimpleNamespace(
        list_guest_resources=lambda force=False: [
            _guest(name="tpl-ubuntu", template=1)
        ],
        power=lambda node, gtype, vmid, action: calls.append(
            (node, gtype, vmid, action)
        ),
    )
    with (
        patch("app.services.remediation.build_client", return_value=client),
        patch("app.services.remediation.time", _FastTime),
    ):
        result = remediate_event(event.id, manual=True)

    assert result["ok"] is False and "模板机" in result["error"]
    assert calls == []
    row = _reload(env.db, event.id)
    assert row.remediation_state == "skipped"


def test_host_status_evaluation_skips_pve_templates(env):
    """模板机永远不是 running，进评估列表就会产生"永远离线"的假告警。

    虚机 host_status 已改由快照评估(evaluate_pve_guest_host_status):
    快照层(_normalize_guest)本身就跳过模板,这里验证评估入口拿到的
    清单确实不含模板机,且 PVE 不可达的平台整体跳过(不误触发)。
    """
    import time as _time

    from app.services.alerts import evaluate_pve_guest_host_status

    captured: list[dict] = []
    state = {
        "name": env.conn.name,
        "reachable": True,
        "error": None,
        "checked_at": _time.time(),
    }
    # 快照层(_normalize_guest)本身就不存模板机,模拟同一口径
    guests = {
        101: {"vmid": 101, "name": "web-01", "status": "running", "node": "pve1"},
    }
    with (
        patch("app.services.alerts.get_connection_state", return_value=state),
        patch("app.services.alerts.get_guests_by_connection", return_value=guests),
        patch(
            "app.services.alerts._evaluate_resource_rules", side_effect=captured.extend
        ),
    ):
        evaluate_pve_guest_host_status()

    # 测试库里还有别的 PVE 连接(快照被全局打桩),只挑本用例那条来断言
    ours = [
        item["name"]
        for item in captured
        if item["name"].startswith(f"{env.conn.name}/")
    ]
    assert ours == [f"{env.conn.name}/web-01"]
    assert not any("tpl-ubuntu" in item["name"] for item in captured)
    # 事件回溯用快照 checked_at(sustain 从真实状态时刻起算)
    ours_item = next(
        item for item in captured if item["name"].startswith(f"{env.conn.name}/")
    )
    assert ours_item["sampled_at"] is not None


def test_remediate_matches_guest_by_device_ip(env):
    """机房里的设备其实跑在 PVE 上时，按管理 IP 反查绑定再拉起。"""
    binding = PveGuestBinding(
        connection_id=env.conn.id,
        guest_type="lxc",
        vmid=200,
        ip_address=env.device.ip_address,
        os_system="linux",
        username="root",
    )
    env.db.add(binding)
    env.db.commit()
    env.extra["bindings"].append(binding.id)

    event = env.make_event(
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=f"主机 {env.device.name} 当前已离线或无法连接",
    )
    started: list[tuple] = []
    client = SimpleNamespace(
        list_guest_resources=lambda force=False: [_guest(200, "pve2", "lxc", "app-01")],
        power=lambda node, gtype, vmid, action: started.append(
            (node, gtype, vmid, action)
        ),
        guest_status=lambda node, gtype, vmid: {"status": "running"},
    )
    with (
        patch("app.services.remediation.build_client", return_value=client),
        patch("app.services.remediation.time", _FastTime),
    ):
        result = remediate_event(event.id, manual=True)

    assert result["ok"] is True
    assert started == [("pve2", "lxc", 200, "start")]
    row = _reload(env.db, event.id)
    assert row.remediation_state == "succeeded"
    # LXC 已移除:标签统一为 PVE 虚拟机(数据层的 guest_type 契约保留,值可为历史 lxc)
    assert "PVE 虚拟机" in row.remediation_json["target"]


def test_remediate_skips_host_without_virtual_target(env):
    """物理机(未匹配到 PVE 绑定)无法远程上电 → 明确标记为无法自动处置。"""
    event = env.make_event(
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
    )
    with patch("app.services.remediation.build_client") as build_client:
        result = remediate_event(event.id, manual=True)

    build_client.assert_not_called()
    assert result["ok"] is False
    row = _reload(env.db, event.id)
    assert row.remediation_state == "skipped"
    assert "人工检查电源与网络" in row.remediation_detail
    assert row.message == f"{BASE_MESSAGE}；{row.remediation_detail}"


def test_remediate_reports_start_failure(env):
    event = env.make_event()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("PVE 500: no such node")

    client = SimpleNamespace(
        list_guest_resources=lambda force=False: [_guest()],
        power=_boom,
        guest_status=lambda *a: {"status": "stopped"},
    )
    with (
        patch("app.services.remediation.build_client", return_value=client),
        patch("app.services.remediation.time", _FastTime),
    ):
        result = remediate_event(event.id, manual=True)

    assert result["ok"] is False
    assert "no such node" in result["error"]
    row = _reload(env.db, event.id)
    assert row.remediation_detail.startswith("自动启动失败")
    # 手动触发只算一次尝试，未达上限时保持 running 以便冷却后重试
    assert row.remediation_state == "running"
    assert row.remediation_json["attempts"] == 1


def test_auto_remediation_gives_up_after_max_attempts(env):
    event = env.make_event(
        remediation_state="failed",
        remediation_json={
            "base_message": BASE_MESSAGE,
            "attempts": ALERT_REMEDIATION_MAX_ATTEMPTS,
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert "最大自动尝试次数" in (check_remediation(event.id) or "")
    # 手动触发不受尝试次数与冷却限制
    assert check_remediation(event.id, manual=True) is None


# ── 容器自动拉起 ──


def test_remediate_starts_exited_container(env):
    container = DeviceContainer(
        device_id=env.device.id,
        container_id="abc123def",
        name="web",
        image="nginx",
        state="exited",
        status="Exited (1)",
    )
    env.db.add(container)
    env.db.commit()
    env.extra["containers"].append(container.id)

    event = env.make_event(
        metric="container_status",
        resource_type="container",
        resource_id=container.container_id,
        resource_name=f"{env.device.name}/web",
        device_id=env.device.id,
        message="容器 web 当前状态异常：Exited (1)",
    )
    host = SimpleNamespace(username="root", password="secret", ssh_key=None)
    actions: list[tuple] = []

    def _fake_collect(target_id: int) -> None:
        # 模拟采集刷新后容器已恢复运行
        row = (
            env.db.query(DeviceContainer)
            .filter(DeviceContainer.id == container.id)
            .first()
        )
        row.state = "running"
        row.status = "Up 2 seconds"
        env.db.commit()

    with (
        patch("app.services.remediation.load_container_target", return_value=host),
        patch(
            "app.services.remediation.run_container_action",
            side_effect=lambda target, action, name: (
                actions.append((action, name)) or (0, "web", "")
            ),
        ),
        patch(
            "app.services.containers_collector._collect_one", side_effect=_fake_collect
        ),
        patch("app.services.remediation.time", _FastTime),
    ):
        result = remediate_event(event.id, manual=True)

    assert result["ok"] is True
    assert actions == [("start", "web")]
    row = _reload(env.db, event.id)
    assert row.remediation_state == "succeeded"
    assert "容器 web" in row.remediation_json["target"]

    audits = (
        env.db.query(ContainerAction)
        .filter(
            ContainerAction.container_name == "web", ContainerAction.action == "start"
        )
        .all()
    )
    assert audits and audits[-1].user_id is None and audits[-1].success == 1
    env.extra["audits"].extend(audit.id for audit in audits)


def test_remediate_starts_exited_container_on_pve_guest(env):
    """虚拟机容器的自愈:device_id 为 None,宿主身份是负数 target_id。

    回归:旧实现把负数 id 丢给 _collect_one / load_container_target 时崩
    "'>' not supported between NoneType and int",启动与验证全部失败。
    """
    binding = PveGuestBinding(
        connection_id=env.conn.id,
        guest_type="qemu",
        vmid=101,
        ip_address="10.7.0.20",
        os_system="linux",
        username="root",
        enabled=1,
    )
    env.db.add(binding)
    env.db.flush()
    container = DeviceContainer(
        pve_guest_binding_id=binding.id,
        container_id="deadbeef0000",
        name="web-guest",
        image="nginx",
        state="exited",
        status="Exited (1)",
    )
    env.db.add(container)
    env.db.commit()
    env.extra["bindings"].append(binding.id)
    env.extra["containers"].append(container.id)

    target_id = -(env.conn.id * PVE_ID_FACTOR + 101)
    event = env.make_event(
        metric="container_status",
        resource_type="container",
        resource_id=container.container_id,
        resource_name="web-01/web-guest",
        device_id=None,
        remediation_json={"target_id": target_id},
        message="容器 web-guest 当前状态异常：Exited (1)",
    )
    host = SimpleNamespace(username="root", password="secret", ssh_key=None)
    actions: list[tuple] = []
    collect_targets: list[int] = []

    def _fake_collect(target: int) -> None:
        collect_targets.append(target)
        # 模拟采集刷新后容器已恢复运行
        row = (
            env.db.query(DeviceContainer)
            .filter(DeviceContainer.id == container.id)
            .first()
        )
        row.state = "running"
        row.status = "Up 2 seconds"
        env.db.commit()

    def _fake_load_target(db, tid):
        assert tid == target_id, f"宿主身份必须是负数 target_id,实际 {tid}"
        return host

    with (
        patch(
            "app.services.remediation.load_container_target",
            side_effect=_fake_load_target,
        ),
        patch(
            "app.services.remediation.run_container_action",
            side_effect=lambda target, action, name: (
                actions.append((action, name)) or (0, "web-guest", "")
            ),
        ),
        patch(
            "app.services.containers_collector._collect_one", side_effect=_fake_collect
        ),
        patch("app.services.remediation.time", _FastTime),
    ):
        result = remediate_event(event.id, manual=True)

    assert result["ok"] is True
    assert actions == [("start", "web-guest")]
    assert collect_targets == [target_id]
    row = _reload(env.db, event.id)
    assert row.remediation_state == "succeeded"

    audits = (
        env.db.query(ContainerAction)
        .filter(
            ContainerAction.container_name == "web-guest",
            ContainerAction.action == "start",
        )
        .all()
    )
    assert audits and audits[-1].success == 1
    env.extra["audits"].extend(audit.id for audit in audits)


# ── 指标过高的 Agent 归因 ──


def test_analysis_blocked_when_agent_not_ready(env):
    event = env.make_event(
        metric="cpu_pct",
        value=97.5,
        threshold=90.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=f"{env.device.name} CPU 使用率 97.5% 已超过阈值 90.0%",
    )
    # 开发库可能已配好 Agent/LLM(真环境在用)，闸门依赖必须显式钉成未就绪。
    with patch("app.services.remediation.get_agent_config") as get_cfg:
        get_cfg.return_value = SimpleNamespace(ready=False)
        reason = check_analysis(event.id, manual=True)
        assert reason is not None and "Agent" in reason
        result = analyze_event(event.id, manual=True)
        assert result["ok"] is False


def test_analysis_writes_agent_report_back(env):
    base = f"{env.device.name} CPU 使用率 97.5% 已超过阈值 90.0%"
    event = env.make_event(
        metric="cpu_pct",
        value=97.5,
        threshold=90.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=base,
    )
    report = "mysql 进程占用 92% CPU，存在慢查询堆积，建议排查未走索引的报表查询。"
    run = AgentRun(
        user_id=None,
        device_id=env.device.id,
        device_name=env.device.name,
        device_ip=env.device.ip_address,
        question="q",
        status="completed",
        report=report,
    )
    env.db.add(run)
    env.db.commit()
    env.extra["runs"].append(run.id)

    questions: list[str] = []
    focuses: list = []
    single_pass_flags: list = []

    def _fake_start(device, question, user_id, cfg, focus=None, single_pass=False):
        questions.append(question)
        focuses.append(focus)
        single_pass_flags.append(single_pass)
        return run.id

    with (
        patch("app.services.remediation.get_agent_config") as get_cfg,
        patch("app.services.remediation.start_diagnosis", side_effect=_fake_start),
        patch("app.services.remediation.time", _FastTime),
    ):
        get_cfg.return_value = SimpleNamespace(ready=True)
        result = analyze_event(event.id, manual=True)

    assert result["ok"] is True
    assert "CPU 使用率" in questions[0] and "97.5%" in questions[0]
    # CPU 告警必须把取证项锁定在进程/CPU/容器内进程快照上，不能让固定话术里的"异常""服务"
    # 把预算带到 journalctl / systemctl 这些又慢又无关的诊断项。
    assert focuses[0] == (
        "top_processes_cpu",
        "cpu",
        "docker_processes",
        "docker_stats",
    )
    # 告警归因必须走单-pass
    assert single_pass_flags == [True]
    row = _reload(env.db, event.id)
    assert row.analysis_state == "completed"
    assert row.agent_run_id == run.id
    assert row.analysis_text == report
    assert row.message == f"{base}；AI 归因：{report}"


def test_analysis_records_failure(env):
    event = env.make_event(
        metric="mem_pct",
        value=95.0,
        threshold=85.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=f"{env.device.name} 内存使用率 95.0% 已超过阈值 85.0%",
    )
    failed = AgentRun(
        user_id=None,
        device_id=env.device.id,
        device_name=env.device.name,
        question="q",
        status="failed",
        error="LLM 超时",
    )
    env.db.add(failed)
    env.db.commit()
    env.extra["runs"].append(failed.id)

    with (
        patch("app.services.remediation.get_agent_config") as get_cfg,
        patch("app.services.remediation.start_diagnosis", return_value=failed.id),
        patch("app.services.remediation.time", _FastTime),
    ):
        get_cfg.return_value = SimpleNamespace(ready=True)
        result = analyze_event(event.id, manual=True)

    assert result["ok"] is False
    row = _reload(env.db, event.id)
    assert row.analysis_state == "failed"
    assert "LLM 超时" in row.analysis_text


def test_check_analysis_accepts_pve_guest_metric_event(env):
    """虚拟机指标事件 device_id=None(宿主身份在 resource_id 负数 id)，
    归因闸门不能把它当「未关联设备」拒掉。"""
    binding = PveGuestBinding(
        connection_id=env.conn.id,
        guest_type="qemu",
        vmid=101,
        ip_address="10.7.0.21",
        os_system="linux",
        username="root",
        password_enc="encrypted",
        enabled=1,
    )
    env.db.add(binding)
    env.db.commit()
    env.extra["bindings"].append(binding.id)
    event = env.make_event(metric="mem_pct", value=95.9, threshold=90.0)

    with patch("app.services.remediation.get_agent_config") as get_cfg:
        get_cfg.return_value = SimpleNamespace(ready=True)
        assert check_analysis(event.id, manual=True) is None


def test_check_analysis_rejects_unlocatable_guest_event(env):
    """device_id=None 且 resource_id 解不出合成负数 id → 仍要拒绝。"""
    event = env.make_event(metric="mem_pct", value=95.9, resource_id="123")
    assert "设备或虚拟机" in (check_analysis(event.id, manual=True) or "")


def test_analysis_runs_inside_pve_guest_via_runtime_target(env):
    """虚拟机归因与自动化同口径:经 _pve_runtime_device 拿明文凭据/IP/OS
    后连进 guest 内部诊断，而不是拿不存在的 Device 行。"""
    binding = PveGuestBinding(
        connection_id=env.conn.id,
        guest_type="qemu",
        vmid=101,
        ip_address="10.7.0.21",
        os_system="linux",
        username="root",
        password_enc="encrypted",
        enabled=1,
    )
    env.db.add(binding)
    env.db.flush()
    run = AgentRun(
        user_id=None,
        device_id=None,
        device_name="web-01",
        device_ip="10.7.0.21",
        question="q",
        status="completed",
        report="java 进程占用 91% 内存，堆配置超过容器限额，建议下调 -Xmx。",
    )
    env.db.add(run)
    env.db.commit()
    env.extra["bindings"].append(binding.id)
    env.extra["runs"].append(run.id)
    event = env.make_event(metric="mem_pct", value=95.9, threshold=90.0)

    seen: dict = {}

    def _fake_start(device, question, user_id, cfg, focus=None, single_pass=False):
        seen["device"] = device
        seen["question"] = question
        seen["single_pass"] = single_pass
        return run.id

    with (
        patch("app.services.remediation.get_agent_config") as get_cfg,
        patch("app.services.remediation.start_diagnosis", side_effect=_fake_start),
        patch("app.services.remediation._pve_runtime_device") as runtime,
        patch("app.services.remediation.time", _FastTime),
    ):
        get_cfg.return_value = SimpleNamespace(ready=True)
        runtime.return_value = AgentTarget(
            id=None,
            name="web-01",
            ip_address="10.7.0.21",
            os_system="linux",
            username="root",
            password="secret",
        )
        result = analyze_event(event.id, manual=True)

    assert result["ok"] is True
    target = runtime.call_args.args[0]
    assert (target.pve_connection_id, target.pve_guest_type, target.pve_vmid) == (
        env.conn.id,
        "qemu",
        101,
    )
    assert target.device_name == "web-01"
    assert seen["device"].username == "root"
    assert seen["single_pass"] is True
    assert "95.9%" in seen["question"]
    row = _reload(env.db, event.id)
    assert row.analysis_state == "completed"
    assert "java" in row.analysis_text


def test_guest_analysis_run_executes_against_runtime_target(env):
    """start_diagnosis 的后台线程不能按 device_id 回查 Device 表:
    AgentTarget 无 ORM 身份，回查必然报「设备不存在」(2026-09-15 真环境踩到)。"""
    binding = PveGuestBinding(
        connection_id=env.conn.id,
        guest_type="qemu",
        vmid=101,
        ip_address="10.7.0.21",
        os_system="linux",
        username="root",
        password_enc="encrypted",
        enabled=1,
    )
    env.db.add(binding)
    env.db.commit()
    env.extra["bindings"].append(binding.id)
    event = env.make_event(metric="mem_pct", value=95.9, threshold=90.0)

    seen: dict = {}

    def _fake_focus(db, device, question, steps, cfg, focus, on_step=None):
        seen["device"] = device
        seen["focus"] = focus
        return "java 进程占用 91% 内存。"

    with (
        patch("app.services.remediation.get_agent_config") as get_cfg,
        patch("app.services.remediation._pve_runtime_device") as runtime,
        patch("app.services.agent.run_focus_diagnosis", side_effect=_fake_focus),
        patch("app.services.remediation.time", _FastTime),
    ):
        get_cfg.return_value = SimpleNamespace(ready=True)
        runtime.return_value = AgentTarget(
            id=None,
            name="web-01",
            ip_address="10.7.0.21",
            os_system="linux",
            username="root",
            password="secret",
        )
        result = analyze_event(event.id, manual=True)

    assert result["ok"] is True, result
    assert seen["device"].username == "root"
    # 告警归因走单-pass，取证项按指标锁定
    assert seen["focus"] == remediation._ANALYSIS_FOCUS["mem_pct"]
    row = _reload(env.db, event.id)
    assert row.analysis_state == "completed"
    assert "java" in (row.analysis_text or "")
    if row.agent_run_id:
        env.extra["runs"].append(row.agent_run_id)


def test_analysis_skips_guest_without_binding(env):
    """虚拟机没配运维接入绑定时给出明确原因，且不写成 failed。"""
    event = env.make_event(metric="mem_pct", value=95.9, threshold=90.0)
    with patch("app.services.remediation.get_agent_config") as get_cfg:
        get_cfg.return_value = SimpleNamespace(ready=True)
        result = analyze_event(event.id, manual=True)
    assert result["ok"] is False and "运维接入绑定" in result["error"]
    row = _reload(env.db, event.id)
    assert row.analysis_state == ""


# ── 恢复收尾与评估钩子 ──


def test_apply_recovered_marks_success(env):
    event = env.make_event(
        remediation_state="verifying",
        remediation_detail="启动指令已下发，正在确认目标是否恢复运行…",
        remediation_json={"base_message": BASE_MESSAGE, "attempts": 1},
    )
    apply_recovered(event)
    env.db.commit()

    row = _reload(env.db, event.id)
    assert row.status == "open"  # 收尾只改处置状态，恢复由评估器决定
    assert row.remediation_state == "succeeded"
    assert row.message == f"{BASE_MESSAGE}；自动拉起操作成功，目标已恢复运行"


def _my_rule_event_ids(env) -> set[int]:
    """本用例规则(rule_id)下的事件 id 集合(评估器已在自己会话里提交)。

    开发库与并行的容器评估循环共享(真实虚机离线告警等):任何跑
    ``_evaluate_resource_rules`` 的用例都会把库里遗留/并发的 open 事件
    顺手提交(submit 闸门是全库语义,这是生产正确行为)。断言只能限定
    本用例规则的事件——“测试不得依赖开发库的空状态”的同类要求。
    先 commit 刷新快照,否则 REPEATABLE READ 看不到其它会话提交的行。
    """
    env.db.commit()
    env.db.expire_all()
    return {
        event_id
        for (event_id,) in env.db.query(AlertEvent.id).filter(
            AlertEvent.rule_id == env.rule.id
        )
    }


def test_resource_evaluation_submits_remediation(env):
    """离线告警一旦 open，评估器就把事件交给自愈流程。"""
    resource = {
        "type": "device",
        "id": -(env.conn.id * PVE_ID_FACTOR + 101),
        "target_id": -(env.conn.id * PVE_ID_FACTOR + 101),
        "device_id": None,
        "name": "web-01",
        "metric": "host_status",
        "value": 1,
        "message": BASE_MESSAGE,
    }
    submitted: list[int] = []
    with (
        patch("app.services.alerts.submit_remediation", side_effect=submitted.append),
        patch("app.services.alerts._notify_event_in_background"),
    ):
        _evaluate_resource_rules([resource])

    my_ids = _my_rule_event_ids(env)
    fresh = [event_id for event_id in submitted if event_id in my_ids]
    assert len(fresh) == 1
    row = _reload(env.db, fresh[0])
    assert row.status == "open"
    # 两步卡片第一步:离线详情带「正在触发自动拉起操作」
    assert row.message == f"{BASE_MESSAGE}；正在触发自动拉起操作"

    # 自愈进度话术追加在同一条消息之后，基础文案不会被覆盖或叠加
    remediation._update(fresh[0], state="running", detail="正在下发启动指令…")
    remediation._update(fresh[0], state="verifying", detail="启动指令已下发，等待确认…")
    row = _reload(env.db, fresh[0])
    # base_message 固化的是「资源话术+两步追加」;后续进度话术在它之后追加,
    # 不会把「正在触发自动拉起操作」无限叠加。
    assert row.remediation_json["base_message"] == (
        f"{BASE_MESSAGE}；正在触发自动拉起操作"
    )
    assert row.message == (
        f"{BASE_MESSAGE}；正在触发自动拉起操作；启动指令已下发，等待确认…"
    )

    # 条件消失 → 评估器把事件置为 resolved 并给处置收尾
    with (
        patch("app.services.alerts.submit_remediation", side_effect=submitted.append),
        patch("app.services.alerts._notify_event_in_background"),
    ):
        _evaluate_resource_rules([{**resource, "value": 0}])
    row = _reload(env.db, fresh[0])
    assert row.status == "resolved"
    _ = resource


# ── 自愈进度通知 ──


def test_state_transition_pushes_remediation_webhook(env):
    """处置进度只推**终态**(succeeded/failed/skipped):running/verifying 这类
    瞬时中间态几秒内就被终态覆盖,逐个推卡只会让一次离线告警在两分钟里
    连发五六张卡(2026-09-16 用户打回);同一状态内改文案也不重复打扰。"""
    event = env.make_event()
    pushed: list[tuple] = []

    def _capture(event_id, event_name, **kwargs):
        pushed.append((event_id, event_name, kwargs))

    with patch("app.services.alerts.notify_event", side_effect=_capture):
        remediation._update(event.id, state="running", detail="正在下发启动指令…")
        remediation._update(event.id, state="verifying", detail="启动指令已下发…")
        remediation._update(event.id, state="running", detail="指令仍在下发…")
        remediation._update(
            event.id, state="succeeded", detail="自动处置成功：目标已恢复运行"
        )

    # 只有 succeeded 终态推一条;中间态不推,告警中心看 remediation_state
    assert [(item[0], item[1]) for item in pushed] == [
        (event.id, "alert.remediation"),
    ]
    # 进度通知是"附加推送"，不参与告警自身通知状态的统计
    assert all(item[2].get("track") is False for item in pushed)


def test_analysis_transition_pushes_analysis_webhook(env):
    """AI 归因跃迁推 alert.analysis(结论补发卡)，处置跃迁仍推 alert.remediation。"""
    event = env.make_event(metric="cpu_pct", value=97.0, threshold=90.0)
    pushed: list[str] = []
    with patch(
        "app.services.alerts.notify_event",
        side_effect=lambda _id, name, **_kw: pushed.append(name),
    ):
        remediation._update(event.id, analysis_state="running")
        remediation._update(
            event.id, analysis_state="completed", analysis_text="mysqld 占用 92% CPU"
        )
        remediation._update(event.id, state="verifying", detail="x")

    # running 是中间态不推卡(否则是一张「AI 归因中」空卡);只有终态推
    assert pushed == ["alert.analysis"]


def test_progress_notify_keeps_original_notification_status(env):
    """没有 Webhook 订阅 alert.remediation 时，原来的「已发送」不能被冲成 skipped。"""
    hook = Webhook(
        name=f"heal-hook-{env.stamp}",
        url="https://example.invalid/hook",
        provider="generic",
        events=["alert.created"],
        headers={},
        enabled=True,
    )
    env.db.add(hook)
    env.db.commit()
    env.extra["webhooks"].append(hook.id)

    event = env.make_event(notification_status="sent")
    _notify(env.db, event, env.rule, None, "alert.remediation", track=False)
    env.db.commit()

    row = _reload(env.db, event.id)
    assert row.notification_status == "sent"


# ── 重启后卡在"AI 归因中"的孤儿事件 ──


def _interrupted_run(env, error: str = INTERRUPTED_RUN_ERROR) -> AgentRun:
    run = AgentRun(
        user_id=None,
        device_id=env.device.id,
        device_name=env.device.name,
        question="q",
        status="failed",
        error=error,
    )
    env.db.add(run)
    env.db.commit()
    env.extra["runs"].append(run.id)
    return run


def test_recover_orphaned_analyses_resets_restart_interrupted(env):
    """被重启掐断的归因不是"分析失败"，要退回未分析让下一轮自动重跑。

    进程重启会让 AgentRun 落到 failed，但事件的 analysis_state 仍停在 running:
    界面永远显示"AI 归因中"，自动链路也因为"该告警已分析过"再也不重跑。
    以前这里统一收敛成 failed，于是告警消息永久挂着"AI 归因分析未得出结果，
    请人工排查"——可真正出问题的是平台自己重启，值班人对着服务器无从排查。
    """
    run = _interrupted_run(env)
    base = f"{env.device.name} 内存使用率 95.0% 已超过阈值 85.0%"
    event = env.make_event(
        metric="mem_pct",
        value=95.0,
        threshold=85.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=base,
        analysis_state="running",
        agent_run_id=run.id,
        remediation_json={"base_message": base},
    )

    assert recover_orphaned_analyses() >= 1

    row = _reload(env.db, event.id)
    assert row.analysis_state == ""
    assert row.analysis_text is None
    # 话术后缀要一起清掉，不能把"进行中…"留在基础文案后面。
    assert row.message == base
    # 退回未分析后自动链路会重新提交，不必人工去告警中心点一次。
    with patch("app.services.remediation.get_agent_config") as get_cfg:
        get_cfg.return_value = SimpleNamespace(ready=True)
        assert (
            check_analysis(event.id) != "该告警已分析过，如需重跑请在告警中心手动触发"
        )


def test_recover_orphaned_analyses_keeps_genuine_failure(env):
    """LLM 报错/超时这类真失败要保留 failed，并把原因写进告警消息。"""
    run = _interrupted_run(env, error="LLM 请求超时")
    base = f"{env.device.name} 内存使用率 95.0% 已超过阈值 85.0%"
    event = env.make_event(
        metric="mem_pct",
        value=95.0,
        threshold=85.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=base,
        analysis_state="running",
        agent_run_id=run.id,
        remediation_json={"base_message": base},
    )

    assert recover_orphaned_analyses() >= 1

    row = _reload(env.db, event.id)
    assert row.analysis_state == "failed"
    assert "LLM 请求超时" in row.analysis_text
    assert row.message == f"{base}；AI 归因未得出结果(LLM 请求超时)"
    # 真失败不自动重跑，避免每轮评估都白烧一次 LLM 调用。
    assert check_analysis(event.id) == "该告警已分析过，如需重跑请在告警中心手动触发"


def test_recover_orphaned_analyses_repairs_mislabeled_history(env):
    """历史上已经被误标成"重启中断"的 open 告警，重启后也要救回来。"""
    base = f"{env.device.name} 内存使用率 31.7% 已超过阈值 20.0%"
    event = env.make_event(
        metric="mem_pct",
        value=31.7,
        threshold=20.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=f"{base}；AI 归因分析未得出结果，请人工排查",
        analysis_state="failed",
        analysis_text=f"AI 归因分析失败：{INTERRUPTED_RUN_ERROR}",
        remediation_json={"base_message": base},
    )

    assert recover_orphaned_analyses() >= 1

    row = _reload(env.db, event.id)
    assert row.analysis_state == ""
    assert row.message == base


def test_recover_orphaned_analyses_leaves_resolved_history_alone(env):
    """已恢复的告警不必再为它烧一次 LLM 调用，历史失败原因保持原样。"""
    base = f"{env.device.name} 内存使用率 31.7% 已超过阈值 20.0%"
    event = env.make_event(
        metric="mem_pct",
        value=31.7,
        threshold=20.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=base,
        status="resolved",
        analysis_state="failed",
        analysis_text=f"AI 归因分析失败：{INTERRUPTED_RUN_ERROR}",
        remediation_json={"base_message": base},
    )

    recover_orphaned_analyses()

    assert _reload(env.db, event.id).analysis_state == "failed"


def test_recover_orphaned_analyses_keeps_live_run(env):
    """多实例部署下别的进程可能真的还在跑，AgentRun 仍为 running 时不能误伤。"""
    run = AgentRun(
        user_id=None,
        device_id=env.device.id,
        device_name=env.device.name,
        question="q",
        status="running",
    )
    env.db.add(run)
    env.db.commit()
    env.extra["runs"].append(run.id)

    event = env.make_event(
        metric="cpu_pct",
        value=97.0,
        threshold=90.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message="cpu high",
        analysis_state="running",
        agent_run_id=run.id,
    )

    recover_orphaned_analyses()

    row = _reload(env.db, event.id)
    assert row.analysis_state == "running"
    assert row.message == "cpu high"


def test_request_analysis_marks_running_before_returning(env):
    """手动触发后状态要立刻落成 running。

    以前要等后台线程排到队才写库，接口刚返回时前端读到的还是"未分析"，
    看起来像点了没反应——归因分析"慢"的第一段观感就来自这里。
    """
    base = f"{env.device.name} CPU 使用率 97.5% 已超过阈值 90.0%"
    event = env.make_event(
        metric="cpu_pct",
        value=97.5,
        threshold=90.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=base,
        first_triggered_at=datetime.now(timezone.utc) - timedelta(seconds=999),
    )

    with (
        patch("app.services.remediation.get_agent_config") as get_cfg,
        patch("app.services.remediation.submit_analysis", return_value=True) as submit,
    ):
        get_cfg.return_value = SimpleNamespace(ready=True)
        result = request_analysis(event.id)

    assert result == {"ok": True, "state": "running"}
    submit.assert_called_once_with(event.id, manual=True)
    row = _reload(env.db, event.id)
    assert row.analysis_state == "running"
    # running 不再往消息里写「进行中」:进度由 analysis_state 展示，
    # 结论由 alert.analysis 卡片补发。
    assert row.message == base


def test_request_analysis_keeps_running_when_already_inflight(env):
    """同一事件已在分析中(单飞)时不重复入队，也不重写文案。"""
    base = f"{env.device.name} 内存使用率 95.0% 已超过阈值 85.0%"
    event = env.make_event(
        metric="mem_pct",
        value=95.0,
        threshold=85.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=f"{base}；AI 归因分析进行中…",
        analysis_state="running",
    )

    with (
        patch("app.services.remediation.get_agent_config") as get_cfg,
        patch("app.services.remediation.submit_analysis", return_value=False) as submit,
        patch("app.services.remediation._update") as update,
    ):
        get_cfg.return_value = SimpleNamespace(ready=True)
        result = request_analysis(event.id)

    assert result == {"ok": True, "state": "running"}
    submit.assert_called_once()
    update.assert_not_called()
    assert _reload(env.db, event.id).message == f"{base}；AI 归因分析进行中…"


# ── 归因结论并入告警通知(扣住 → 放行) ──


def _metric_rule(env, metric: str = "mem_pct", threshold: float = 85.0) -> AlertRule:
    rule = AlertRule(
        name=f"heal-{metric}-{env.stamp}",
        metric=metric,
        operator="gt",
        threshold=threshold,
        severity="warning",
        sustain_seconds=0,
        cooldown_seconds=0,
    )
    env.db.add(rule)
    env.db.commit()
    return rule


def _drop_rule(env, rule: AlertRule) -> None:
    env.db.query(AlertEvent).filter(AlertEvent.rule_id == rule.id).delete(
        synchronize_session=False
    )
    env.db.query(AlertRule).filter(AlertRule.id == rule.id).delete()
    env.db.commit()


def test_metric_alert_holds_created_notification_until_analysis(env):
    """内存告警不能先发一条没有归因的消息，再补一条结论。

    alert.created 先扣住，等归因落到终态再放行，这样发出去的 payload 里
    message 与 analysis.text 已经带着结论，飞书卡片的「AI 归因」块直接有内容。
    """
    rule = _metric_rule(env, "mem_pct", 85.0)
    sent: list[tuple[int, str]] = []

    def _capture(event_id, name, **_kwargs):
        sent.append((event_id, name))

    try:
        with (
            patch("app.services.alerts.notify_event", side_effect=_capture),
            patch("app.services.alerts.submit_analysis") as submit,
        ):
            evaluate_alerts({env.device.id: {"available": True, "mem_pct": 96.0}})

        # 评估当轮不发 alert.created，只把它扣住并触发归因
        assert [name for _id, name in sent] == []
        assert submit.called
        # 开发库可能还有其它 open 的指标告警(同样进归因候选)，
        # 按规则找本用例自己的事件，不能赌 call_args 的顺序。
        env.db.commit()
        env.db.expire_all()
        row = (
            env.db.query(AlertEvent)
            .filter(AlertEvent.rule_id == rule.id)
            .order_by(AlertEvent.id.desc())
            .first()
        )
        assert row is not None
        event_id = row.id
        assert event_id in {c.args[0] for c in submit.call_args_list}
        row = _reload(env.db, event_id)
        assert row.remediation_json["notification_held"] == "alert.created"
        assert row.remediation_json["notification_held_at"]

        # 归因出结论 → 放行，这时才真正发 alert.created
        remediation._update(
            event_id,
            analysis_state="completed",
            analysis_text="java 进程常驻 88% 内存，疑似堆未回收",
        )
        with patch("app.services.alerts.notify_event", side_effect=_capture):
            assert release_held_notification(event_id) is True

        assert (event_id, "alert.created") in sent
        row = _reload(env.db, event_id)
        assert row.remediation_json.get("notification_held") is None
        assert "AI 归因" in row.message
        # 幂等：再放行一次不会重复发消息
        assert release_held_notification(event_id) is False
        assert sent.count((event_id, "alert.created")) == 1
    finally:
        _drop_rule(env, rule)


def test_held_notification_suppresses_duplicate_progress_push(env):
    """扣住期间不再另推 alert.remediation，否则用户会收到两条内容重复的消息。"""
    event = env.make_event(metric="mem_pct", value=96.0, threshold=85.0)
    assert remediation.hold_created_notification(event.id) is True

    pushed: list[str] = []
    with patch(
        "app.services.alerts.notify_event",
        side_effect=lambda _id, name, **_kw: pushed.append(name),
    ):
        remediation._update(event.id, analysis_state="running")
        remediation._update(event.id, analysis_state="completed", analysis_text="结论")
    assert pushed == []

    with patch(
        "app.services.alerts.notify_event",
        side_effect=lambda _id, name, **_kw: pushed.append(name),
    ):
        release_held_notification(event.id)
    assert pushed == ["alert.created"]


def test_hold_is_skipped_when_feature_disabled(env):
    """关掉开关就恢复旧行为:告警立刻发，不扣住。"""
    event = env.make_event(metric="mem_pct", value=96.0, threshold=85.0)
    with patch("app.services.remediation.ALERT_ANALYSIS_HOLD_NOTIFICATION", False):
        assert remediation.hold_created_notification(event.id) is False
        assert sweep_expired_notification_holds() == 0
    assert (_reload(env.db, event.id).remediation_json or {}).get(
        "notification_held"
    ) is None


def test_hold_is_skipped_when_auto_analysis_disabled(env):
    """自动归因整个关掉时也不扣住，否则每轮白写两次库再原样放行。"""
    event = env.make_event(metric="mem_pct", value=96.0, threshold=85.0)
    with patch("app.services.remediation.ALERT_AUTO_ANALYSIS", False):
        assert remediation.hold_created_notification(event.id) is False
    assert (_reload(env.db, event.id).remediation_json or {}).get(
        "notification_held"
    ) is None


def test_sweep_releases_expired_hold(env):
    """看门狗:归因卡死或进程重启后，扣住的通知必须在超时后放行。"""
    event = env.make_event(metric="cpu_pct", value=97.0, threshold=90.0)
    assert remediation.hold_created_notification(event.id) is True
    stale = datetime.now(timezone.utc) - timedelta(seconds=9999)
    row = _reload(env.db, event.id)
    record = dict(row.remediation_json)
    record["notification_held_at"] = stale.isoformat().replace("+00:00", "Z")
    row.remediation_json = record
    env.db.commit()

    released: list[int] = []

    def _record(event_id, **_kwargs):
        released.append(event_id)
        return True

    with patch(
        "app.services.remediation.release_held_notification", side_effect=_record
    ):
        assert sweep_expired_notification_holds() >= 1
    assert event.id in released


def test_sweep_keeps_hold_until_terminal_then_watchdog_fallback(env):
    """单卡片模型:归因未终态且看门狗未超时时，sweep 不得放行(保证告警卡
    带结论);只有看门狗超时才先放行干净卡片，结论产出后 alert.analysis
    仅作兜底补发。"""
    base = f"{env.device.name} 内存使用率 95.0% 已超过阈值 85.0%"
    event = env.make_event(
        metric="mem_pct",
        value=95.0,
        threshold=85.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=base,
        analysis_state="running",
    )
    row = _reload(env.db, event.id)
    record = dict(row.remediation_json or {})
    record["notification_held"] = "alert.created"
    record["notification_held_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=ALERT_ANALYSIS_HOLD_TIMEOUT // 2)
    ).isoformat()
    row.remediation_json = record
    env.db.commit()

    sent: list[tuple[int, str]] = []

    def _capture(event_id, name, **_kwargs):
        sent.append((event_id, name))

    # 归因仍在跑、未超看门狗:一条都不发(单卡片保证)
    with patch("app.services.alerts.notify_event", side_effect=_capture):
        assert sweep_expired_notification_holds() == 0
    assert sent == []

    # 看门狗超时:放行干净卡片(不带「进行中」话术)
    row = _reload(env.db, event.id)
    record = dict(row.remediation_json or {})
    record["notification_held_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=9999)
    ).isoformat()
    row.remediation_json = record
    env.db.commit()
    with patch("app.services.alerts.notify_event", side_effect=_capture):
        assert sweep_expired_notification_holds() >= 1
    assert (event.id, "alert.created") in sent
    row = _reload(env.db, event.id)
    assert row.remediation_json.get("notification_held") is None
    assert "进行中" not in (row.message or "")

    # 兜底:结论产出后补一张，且不走处置进度事件
    sent.clear()
    with patch("app.services.alerts.notify_event", side_effect=_capture):
        remediation._update(
            event.id,
            analysis_state="completed",
            analysis_text="java 进程常驻 88% 内存，疑似堆未回收。",
        )
    assert (event.id, "alert.analysis") in sent
    assert (event.id, "alert.remediation") not in sent
    row = _reload(env.db, event.id)
    assert "AI 归因" in row.message


def test_first_triggered_backdated_to_sample_time(env):
    """sustain 从样本真实越限时刻起算:条目携带的样本时间戳回写
    first_triggered_at，消掉一个评估周期的误差。"""
    rule = _metric_rule(env, "mem_pct", 85.0)
    rule.sustain_seconds = 90
    env.db.commit()
    try:
        sampled = datetime.now(timezone.utc) - timedelta(seconds=100)
        with patch("app.services.alerts.notify_event"):
            evaluate_alerts(
                {
                    env.device.id: {
                        "available": True,
                        "mem_pct": 96.0,
                        "sampled_at": sampled,
                    }
                }
            )
        env.db.commit()
        env.db.expire_all()
        event = (
            env.db.query(AlertEvent)
            .filter(AlertEvent.rule_id == rule.id)
            .order_by(AlertEvent.id.desc())
            .first()
        )
        assert event is not None
        triggered = event.first_triggered_at
        if triggered.tzinfo is None:
            triggered = triggered.replace(tzinfo=timezone.utc)
        assert abs((triggered - sampled).total_seconds()) < 5
        # 创建轮一律 pending;下一轮 elapsed(≈样本龄 100s) ≥ sustain 90s → open。
        # 不回溯样本时间的话 elapsed 只有评估间隔，这一轮还会是 pending。
        assert event.status == "pending"

        with patch("app.services.alerts.notify_event"):
            evaluate_alerts(
                {
                    env.device.id: {
                        "available": True,
                        "mem_pct": 96.0,
                        "sampled_at": sampled,
                    }
                }
            )
        env.db.commit()
        env.db.expire_all()
        assert event.status == "open"
    finally:
        _drop_rule(env, rule)


def test_sweep_releases_hold_when_event_resolved(env):
    """事件已恢复时立刻放行，且不会被超时时间挡住。"""
    event = env.make_event(metric="cpu_pct", value=97.0, threshold=90.0)
    assert remediation.hold_created_notification(event.id) is True
    row = _reload(env.db, event.id)
    row.status = "resolved"
    env.db.commit()

    released: list[int] = []

    def _record(event_id, **_kwargs):
        released.append(event_id)
        return True

    with patch(
        "app.services.remediation.release_held_notification", side_effect=_record
    ):
        sweep_expired_notification_holds()
    assert event.id in released


def test_sweep_releases_hold_missing_timestamp(env):
    """缺少扣住时间戳就无法判断超时，宁可放行也不能吞掉告警。"""
    event = env.make_event(metric="disk_max_pct", value=97.0, threshold=90.0)
    row = _reload(env.db, event.id)
    row.remediation_json = {"notification_held": "alert.created"}
    env.db.commit()

    released: list[int] = []

    def _record(event_id, **_kwargs):
        released.append(event_id)
        return True

    with patch(
        "app.services.remediation.release_held_notification", side_effect=_record
    ):
        sweep_expired_notification_holds()
    assert event.id in released


def test_analyze_event_releases_hold_when_gate_rejects(env):
    """Agent 未就绪导致归因被跳过时，扣住的告警仍必须发出去。"""
    event = env.make_event(
        metric="cpu_pct",
        value=97.0,
        threshold=90.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message="cpu high",
    )
    assert remediation.hold_created_notification(event.id) is True

    with (
        patch("app.services.remediation.get_agent_config") as get_cfg,
        patch("app.services.alerts.notify_event") as notify,
    ):
        get_cfg.return_value = SimpleNamespace(ready=False)
        result = analyze_event(event.id)

    assert result["ok"] is False
    notify.assert_called_once_with(event.id, "alert.created", track=True)
    assert _reload(env.db, event.id).remediation_json.get("notification_held") is None


def test_held_notification_waives_analysis_sustain_gate(env):
    """扣住通知时立刻归因，否则放行那一刻仍然没有结论。

    规则默认 sustain_seconds=60，而 ALERT_ANALYSIS_SUSTAIN_SECONDS=180:
    告警刚 open 就扣住通知，若还等满 180s 才归因，第一条消息注定是空的，
    用户依旧要等第二条 alert.remediation 才看得到结论。
    """
    event = env.make_event(
        metric="mem_pct",
        value=96.0,
        threshold=85.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message="内存使用率 96.0% 已超过阈值 85.0%",
    )
    with patch("app.services.remediation.get_agent_config") as get_cfg:
        get_cfg.return_value = SimpleNamespace(ready=True)
        assert "持续超阈值" in (check_analysis(event.id) or "")
        assert remediation.hold_created_notification(event.id) is True
        assert check_analysis(event.id) is None


def test_analysis_sustain_gate_still_applies_when_not_held(env):
    """关掉扣住(或不打算带结论发)时，防抖闸门照旧生效，不为毛刺烧 LLM 调用。"""
    event = env.make_event(
        metric="mem_pct",
        value=96.0,
        threshold=85.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message="内存使用率 96.0% 已超过阈值 85.0%",
    )
    with patch("app.services.remediation.get_agent_config") as get_cfg:
        get_cfg.return_value = SimpleNamespace(ready=True)
        assert check_analysis(event.id) == "指标未持续超阈值，暂不触发分析"


def test_repeat_notification_stays_held_until_analysis(env):
    """冷却到期的重复通知撞上还没放行的扣住，同样不能抢在结论前面发出去。"""
    event = env.make_event(metric="mem_pct", value=96.0, threshold=85.0)
    assert remediation.hold_created_notification(event.id) is True
    assert remediation.hold_created_notification(event.id) is True

    pushed: list[str] = []
    with patch(
        "app.services.alerts.notify_event",
        side_effect=lambda _id, name, **_kw: pushed.append(name),
    ):
        assert remediation.release_held_notification(event.id) is True
    assert pushed == ["alert.created"]


class _SyncPool:
    """把 notify_event 的后台投递变成同步执行，测试不必等线程池。

    submit 必须返回一个已完成的 Future:alerts._notify 用 as_completed 遍历
    结果,返回 None 会让 future._condition 访问炸掉。
    """

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        fut = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as exc:
            fut.set_exception(exc)
        return fut


def test_metric_alert_message_carries_analysis_end_to_end(env):
    """端到端:内存告警投递出去的第一条消息里就已经带着 AI 归因结论。"""
    rule = _metric_rule(env, "mem_pct", 85.0)
    rule.sustain_seconds = 0
    rule.target_device_ids = [env.device.id]
    env.db.commit()
    hook = Webhook(
        name=f"heal-e2e-{env.stamp}",
        url="https://example.invalid/hook",
        provider="generic",
        events=["alert.created"],
        headers={},
        enabled=True,
    )
    env.db.add(hook)
    run = AgentRun(
        user_id=None,
        device_id=env.device.id,
        device_name=env.device.name,
        device_ip=env.device.ip_address,
        question="q",
        status="completed",
        report=AGENT_REPORT,
    )
    env.db.add(run)
    env.db.commit()
    env.extra["webhooks"].append(hook.id)
    env.extra["runs"].append(run.id)

    delivered: list[dict] = []

    def _capture(webhook, payload):
        # 开发库里可能有真实启用的 webhook(如飞书通知)，只统计本用例自己的钩子。
        if webhook.id != hook.id:
            return {
                "webhook_id": webhook.id,
                "name": webhook.name,
                "ok": True,
                "status_code": 200,
            }
        delivered.append(payload)
        return {
            "webhook_id": webhook.id,
            "name": webhook.name,
            "ok": True,
            "status_code": 200,
        }

    try:
        with (
            patch("app.services.alerts._notification_pool", _SyncPool()),
            patch("app.services.alerts._deliver_one", side_effect=_capture),
            patch("app.services.alerts.submit_analysis") as submit,
        ):
            evaluate_alerts({env.device.id: {"available": True, "mem_pct": 96.0}})
            # 评估当轮只扣住并触发归因，一条消息都不发
            assert delivered == []
            assert submit.called

            # 后台线程在别的会话里提交，先结束当前快照再查，否则读不到新事件
            env.db.commit()
            env.db.expire_all()
            event = (
                env.db.query(AlertEvent)
                .filter(AlertEvent.rule_id == rule.id)
                .order_by(AlertEvent.id.desc())
                .first()
            )
            assert event is not None
            assert event.remediation_json["notification_held"] == "alert.created"

            with (
                patch("app.services.remediation.get_agent_config") as get_cfg,
                patch("app.services.remediation.start_diagnosis", return_value=run.id),
                patch("app.services.remediation.time", _FastTime),
            ):
                get_cfg.return_value = SimpleNamespace(ready=True)
                assert analyze_event(event.id)["ok"] is True

        # _drop_rule 会连带删掉事件，落库状态要在清理之前读出来
        row = _reload(env.db, event.id)
        notification_status = row.notification_status
        still_held = (row.remediation_json or {}).get("notification_held")
    finally:
        _drop_rule(env, rule)

    assert len(delivered) == 1
    payload = delivered[0]
    assert payload["event"] == "alert.created"
    assert payload["metric"] == "mem_pct"
    assert payload["analysis"]["state"] == "completed"
    assert payload["analysis"]["state_label"] == "AI 归因完成"
    assert payload["analysis"]["text"] == AGENT_REPORT
    assert payload["analysis_text"] == AGENT_REPORT
    # 正文里的归因是结论本身，而不是 "## 结论" 这个标题
    assert "内存占用主要来自 java(pid 1234)" in payload["message"]
    assert payload["message"].endswith("置信度：高。")

    assert notification_status == "sent"
    assert still_held is None


# ── 容器状态告警的 AI 归因(2026-09-15 纳入,曾长期是死配置) ──


def test_check_analysis_accepts_container_status_event(env):
    """container_status 归因闸门放行:宿主身份从处置记录 target_id 解析。"""
    binding = PveGuestBinding(
        connection_id=env.conn.id,
        guest_type="qemu",
        vmid=101,
        ip_address="10.7.0.21",
        os_system="linux",
        username="root",
        password_enc="encrypted",
        enabled=1,
    )
    env.db.add(binding)
    env.db.commit()
    env.extra["bindings"].append(binding.id)

    target_id = -(env.conn.id * PVE_ID_FACTOR + 101)
    event = env.make_event(
        metric="container_status",
        resource_type="container",
        resource_id="deadbeef0000",
        resource_name="web-01/web-guest",
        device_id=None,
        remediation_json={"target_id": target_id},
        message="容器 web-guest 当前状态异常：Exited (1)",
    )
    with patch("app.services.remediation.get_agent_config") as get_cfg:
        get_cfg.return_value = SimpleNamespace(ready=True)
        assert check_analysis(event.id, manual=True) is None

    # device 容器(device_id 直接可用)同样放行
    device_event = env.make_event(
        metric="container_status",
        resource_type="container",
        resource_id="abc123",
        resource_name=f"{env.device.name}/web",
        device_id=env.device.id,
        message="容器 web 当前状态异常：Exited (1)",
    )
    with patch("app.services.remediation.get_agent_config") as get_cfg:
        get_cfg.return_value = SimpleNamespace(ready=True)
        assert check_analysis(device_event.id, manual=True) is None


def test_container_status_analysis_targets_guest_host(env):
    """虚拟机容器归因:目标是虚拟机本身(不是容器 id),问题话术指向容器名,
    取证项锁定容器状态/进程/日志。"""
    binding = PveGuestBinding(
        connection_id=env.conn.id,
        guest_type="qemu",
        vmid=101,
        ip_address="10.7.0.21",
        os_system="linux",
        username="root",
        password_enc="encrypted",
        enabled=1,
    )
    env.db.add(binding)
    env.db.flush()
    run = AgentRun(
        user_id=None,
        device_id=None,
        device_name="web-01",
        device_ip="10.7.0.21",
        question="q",
        status="completed",
        report="容器 web-guest 因上游数据库不可达反复重启,建议检查依赖。",
    )
    env.db.add(run)
    env.db.commit()
    env.extra["bindings"].append(binding.id)
    env.extra["runs"].append(run.id)

    target_id = -(env.conn.id * PVE_ID_FACTOR + 101)
    event = env.make_event(
        metric="container_status",
        resource_type="container",
        resource_id="deadbeef0000",
        resource_name="web-01/web-guest",
        device_id=None,
        remediation_json={"target_id": target_id},
        message="容器 web-guest 当前状态异常：Exited (1)",
    )

    seen: dict = {}

    def _fake_start(device, question, user_id, cfg, focus=None, single_pass=False):
        seen["device"] = device
        seen["question"] = question
        seen["focus"] = focus
        seen["single_pass"] = single_pass
        return run.id

    with (
        patch("app.services.remediation.get_agent_config") as get_cfg,
        patch("app.services.remediation.start_diagnosis", side_effect=_fake_start),
        patch("app.services.remediation._pve_runtime_device") as runtime,
        patch("app.services.remediation.time", _FastTime),
    ):
        get_cfg.return_value = SimpleNamespace(ready=True)
        runtime.return_value = AgentTarget(
            id=None,
            name="web-01",
            ip_address="10.7.0.21",
            os_system="linux",
            username="root",
            password="secret",
        )
        result = analyze_event(event.id, manual=True)

    assert result["ok"] is True, result
    # 目标是虚拟机(AgentTarget),不是容器 id
    assert seen["device"].name == "web-01"
    assert seen["single_pass"] is True
    assert seen["focus"] == remediation._ANALYSIS_FOCUS["container_status"]
    # 问题话术指向容器,不出现百分比模板
    assert "容器 web-guest" in seen["question"]
    assert "已超过告警阈值" not in seen["question"]
    row = _reload(env.db, event.id)
    assert row.analysis_state == "completed"
    assert "web-guest" in (row.analysis_text or "")


def test_disk_focus_includes_directory_usage(env):
    """磁盘归因要能回答「哪个目录在涨」:focus 里必须有目录级 du 项。"""
    event = env.make_event(
        metric="disk_max_pct",
        value=93.0,
        threshold=85.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=f"{env.device.name} 磁盘使用率 93.0% 已超过阈值 85.0%",
    )
    seen: dict = {}

    def _fake_start(device, question, user_id, cfg, focus=None, single_pass=False):
        seen["focus"] = focus
        return 0

    with (
        patch("app.services.remediation.get_agent_config") as get_cfg,
        patch("app.services.remediation.start_diagnosis", side_effect=_fake_start),
        patch("app.services.remediation.time", _FastTime),
    ):
        get_cfg.return_value = SimpleNamespace(ready=True)
        analyze_event(event.id, manual=True)

    assert seen["focus"] == remediation._ANALYSIS_FOCUS["disk_max_pct"]
    assert "disk_usage_top" in seen["focus"]


def test_resource_evaluation_container_sends_card_immediately(env):
    """容器状态告警两步模型(2026-09-16 用户定调):alert.created **立刻**发出
    (详情带「正在触发自动拉起操作」),不再扣住等 AI 归因——归因结论改走
    alert.analysis 补发。处置提交照常。"""
    container = DeviceContainer(
        device_id=env.device.id,
        container_id="ab12cd34",
        name="api",
        image="api:1",
        state="exited",
        status="Exited (1)",
    )
    env.db.add(container)
    env.db.commit()
    env.extra["containers"].append(container.id)

    resource = {
        "type": "container",
        "id": container.container_id,
        "target_id": env.device.id,
        "device_id": env.device.id,
        "name": f"{env.device.name}/api",
        "metric": "container_status",
        "value": 1,
        "message": "容器 api 当前状态异常：Exited (1)",
    }
    submitted: list[int] = []
    notified: list[tuple] = []
    env.rule.metric = "container_status"
    env.rule.threshold = 0.0
    env.rule.sustain_seconds = 0
    env.rule.target_device_ids = [env.device.id]
    env.db.commit()

    def _notify_bg(event_id, event_name, **kwargs):
        notified.append((event_id, event_name))

    with (
        patch("app.services.alerts.submit_remediation"),
        patch("app.services.alerts.submit_analysis", side_effect=submitted.append),
        patch("app.services.alerts.sweep_expired_notification_holds"),
        patch(
            "app.services.alerts._notify_event_in_background", side_effect=_notify_bg
        ),
        patch("app.services.alerts.notify_event", side_effect=_notify_bg),
    ):
        _evaluate_resource_rules([resource])

    assert len(submitted) == 1
    row = _reload(env.db, submitted[0])
    assert row.status == "open"
    assert row.metric == "container_status"
    # 卡片立刻发出,没有被扣住;详情带自动拉起话术
    assert (row.remediation_json or {}).get("notification_held") is None
    assert notified == [(row.id, "alert.created")]
    assert row.message == "容器 api 当前状态异常：Exited (1)；正在触发自动拉起操作"


def test_rule_covers_keeps_container_branch_first(env):
    """container_status 已是归因指标,但范围判定必须仍走容器分支:
    按容器 id / 宿主 device_id 比较,不能被 ANALYSIS_METRICS 分支误判成出范围。"""
    target_id = -(env.conn.id * PVE_ID_FACTOR + 101)
    event = env.make_event(
        metric="container_status",
        resource_type="container",
        resource_id="deadbeef0000",
        resource_name="web-01/web-guest",
        device_id=None,
        remediation_json={"target_id": target_id},
        message="容器 web-guest 当前状态异常：Exited (1)",
    )
    from app.services.alerts import _rule_covers

    rule = SimpleNamespace(
        metric="container_status",
        target_device_ids=[target_id],
        target_container_ids=None,
        target_business_ids=None,
    )
    assert _rule_covers(rule, event) is True
    # 换个不在范围内的宿主 → 出范围
    rule_out = SimpleNamespace(
        metric="container_status",
        target_device_ids=[target_id + 999],
        target_container_ids=None,
        target_business_ids=None,
    )
    assert _rule_covers(rule_out, event) is False


# ── 虚拟机告警显示运维接入 IP(2026-09-16) ──


def test_notify_payload_carries_guest_binding_ip(env):
    """虚拟机事件(无本地设备行)的推送地址字段 = 运维绑定 IP(QGA 优先回填);
    未配置绑定的虚机保持 '-'(不阻塞推送)。"""
    from app.services.alerts import _payload

    binding = PveGuestBinding(
        connection_id=env.conn.id,
        guest_type="qemu",
        vmid=101,
        ip_address="10.7.0.21",
        os_system="linux",
        username="root",
        password_enc="encrypted",
        enabled=1,
    )
    env.db.add(binding)
    env.db.commit()
    env.extra["bindings"].append(binding.id)

    # 指标事件:宿主身份在 resource_id(负数)
    event = env.make_event(
        device_id=None,
        resource_id=str(-(env.conn.id * PVE_ID_FACTOR + 101)),
        resource_name="[pve] SRM",
    )
    # 容器事件:宿主身份在处置记录 target_id
    container_event = env.make_event(
        metric="container_status",
        resource_type="container",
        resource_id="deadbeef0000",
        resource_name="SRM/web",
        device_id=None,
        remediation_json={"target_id": -(env.conn.id * PVE_ID_FACTOR + 101)},
    )

    from app.services.alerts import _guest_binding_ip, _guest_binding_ips

    # 单条与批量解析同口径
    assert _guest_binding_ip(env.db, event) == "10.7.0.21"
    assert _guest_binding_ips(env.db, [event, container_event]) == {
        event.id: "10.7.0.21",
        container_event.id: "10.7.0.21",
    }
    # 无绑定的虚机(如 DolphinScheduler1):拿不到 IP,不报错
    orphan = env.make_event(
        device_id=None,
        resource_id=str(-(env.conn.id * PVE_ID_FACTOR + 999)),
        resource_name="[pve] no-binding",
    )
    assert _guest_binding_ip(env.db, orphan) is None
    # 物理设备事件不走绑定反查(地址由 Device 行提供)
    device_event = env.make_event(
        device_id=env.device.id, resource_id=str(env.device.id)
    )
    assert _guest_binding_ip(env.db, device_event) is None

    # 推送 payload 的地址字段带上虚机 IP
    data = _payload(event, None, None, "alert.created", guest_ip="10.7.0.21")
    assert data["device"]["ip_address"] == "10.7.0.21"
    assert data["device_ip"] == "10.7.0.21"
    bare = _payload(orphan, None, None, "alert.created", guest_ip=None)
    assert bare["device"]["ip_address"] is None


def test_notify_resolves_guest_ip_for_vm_event(env):
    """_notify 对虚拟机事件自动反查运维绑定,推送载荷地址字段带 IP
    (集成:_notify 内部完成解析,不需要调用方操心)。"""

    binding = PveGuestBinding(
        connection_id=env.conn.id,
        guest_type="qemu",
        vmid=101,
        ip_address="10.7.0.21",
        os_system="linux",
        username="root",
        password_enc="encrypted",
        enabled=1,
    )
    env.db.add(binding)
    env.db.commit()
    env.extra["bindings"].append(binding.id)

    hook = Webhook(
        name=f"guest-ip-hook-{env.stamp}",
        url="https://example.invalid/hook",
        provider="generic",
        events=["alert.created"],
        headers={},
        enabled=True,
    )
    env.db.add(hook)
    env.db.commit()
    env.extra["webhooks"].append(hook.id)

    event = env.make_event(
        device_id=None,
        resource_id=str(-(env.conn.id * PVE_ID_FACTOR + 101)),
        resource_name="[pve] SRM",
    )
    # 开发库里有真实在用的 webhook(订阅 alert.created),按 AGENTS.md 约定
    # 只计数本测试自建的 hook,不对总数做断言。
    captured: list[dict] = []

    def _fake_deliver(hook_row, payload):
        if hook_row.id == hook.id:
            captured.append(payload)
        return {"ok": True}

    import app.services.alerts as alerts_mod

    with patch.object(alerts_mod, "_deliver_one", side_effect=_fake_deliver):
        _notify(env.db, event, env.rule, None, "alert.created")

    env.db.commit()  # _notify(track=True) 改了通知状态,先落库再断言
    assert len(captured) == 1
    assert captured[0]["device"]["ip_address"] == "10.7.0.21"
    assert captured[0]["device_ip"] == "10.7.0.21"
    assert captured[0]["device"]["name"] == "[pve] SRM"


def test_concurrent_analysis_rejection_keeps_hold_until_owner_finishes(env):
    """回归(2026-09-16 实测两张卡):第一个归因还在跑时,第二个 analyze_event
    被闸门拒绝(「已分析过」),它绝不能在 finally 里把还没带结论的扣住通知
    提前放行——放行权归拥有归因的调用,结论落地后连结论一起发一张卡。"""
    import threading as _threading

    from app.services.alerts import hold_created_notification

    # 注意 metric:check_analysis 只放行 cpu/mem/disk/container,env.rule 默认的
    # host_status 会被闸门拒绝(归因根本不会启动)。
    event = env.make_event(
        metric="mem_pct",
        value=96.0,
        threshold=90.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        message=f"{env.device.name} 内存使用率 96.0% 已超过阈值 90.0%",
        notification_status="pending",
    )
    # hold/analyze/release 全部走自己的会话(与生产一致);env.db 快照立即过期,
    # 后续断言一律重读,避免 REPEATABLE READ 读到扣住之前的旧状态。
    assert hold_created_notification(event.id) is True

    report = "sqlservr 进程占用 79% 内存，建议限制最大服务器内存。"
    gate = _threading.Event()

    def _owner_wait(run_id):
        gate.wait(5)  # 模拟 _wait_for_run 等待归因完成
        return report, ""

    released: list[str] = []
    owner_result: list[dict] = []

    def _capture_release(event_id, *, reason=""):
        released.append(reason or "released")
        return True

    owner_trace: list[str] = []

    def _owner_call():
        try:
            with (
                patch("app.services.remediation.get_agent_config") as get_cfg,
                patch("app.services.remediation.start_diagnosis", return_value=0),
                patch(
                    "app.services.remediation._wait_for_run", side_effect=_owner_wait
                ),
                patch(
                    "app.services.remediation.release_held_notification",
                    side_effect=_capture_release,
                ),
            ):
                get_cfg.return_value = SimpleNamespace(ready=True)
                owner_trace.append("before")
                owner_result.append(remediation.analyze_event(event.id))
                owner_trace.append("after")
        except BaseException as exc:  # 诊断:线程内异常会被吞掉
            owner_trace.append(f"EXC {exc!r}")

    owner = _threading.Thread(target=_owner_call)
    owner.start()
    # 等第一个调用把状态写成 running(它拥有这次归因);用独立会话读,
    # 不碰 env.db 的事务快照(它可能正被其它用例遗留状态卡住)。
    deadline = time.time() + 5
    state = ""
    while time.time() < deadline:
        probe = SessionLocal()
        try:
            state = (
                probe.query(AlertEvent)
                .filter(AlertEvent.id == event.id)
                .first()
                .analysis_state
                or ""
            )
        finally:
            probe.close()
        if state == "running":
            break
        time.sleep(0.02)
    assert state == "running", f"owner_trace={owner_trace} result={owner_result}"

    # 第二个调用(并发/重复提交):闸门拒绝,且不得放行扣住的通知
    with patch(
        "app.services.remediation.release_held_notification",
        side_effect=_capture_release,
    ):
        second = remediation.analyze_event(event.id)
    assert second["ok"] is False and second["concurrent"] is True
    assert released == []  # 闸门拒绝的调用没有抢着放行

    row = _reload(env.db, event.id)
    assert (row.remediation_json or {}).get("notification_held") == "alert.created"

    # 第一个调用完成:结论落地 → 由它放行(带结论的唯一一次放行)
    gate.set()
    owner.join(timeout=5)
    env.db.expire_all()

    assert owner_result and owner_result[0]["ok"] is True
    row = _reload(env.db, event.id)
    assert row.analysis_state == "completed"
    assert "sqlservr" in (row.analysis_text or "")
    # 恰好一次放行:由拥有归因的调用在结论落地后执行
    assert released == ["released"]


def test_resource_evaluation_does_not_submit_foreign_metric_events(env):
    """资源评估路径(容器/主机状态)只提交**本轮通知过**的归因候选:
    全表 current_map 里的 mem_pct 事件归指标评估路径管,不许在这里被每轮重复提交。"""
    # 一个已 open 的 mem_pct 事件(不属于资源评估域,且本轮不会通知它)
    env.rule.metric = "mem_pct"
    env.rule.threshold = 20.0
    env.rule.sustain_seconds = 0
    env.db.commit()
    metric_event = env.make_event(status="open")

    resource = {
        "type": "device",
        "id": env.device.id,
        "target_id": env.device.id,
        "device_id": env.device.id,
        "name": env.device.name,
        "metric": "host_status",
        "value": 1,
        "message": BASE_MESSAGE,
    }
    submitted: list[int] = []
    with (
        patch("app.services.alerts.submit_remediation"),
        patch(
            "app.services.alerts.submit_analysis", side_effect=submitted.append
        ) as submit,
        patch("app.services.alerts._notify_event_in_background"),
    ):
        _evaluate_resource_rules([resource])

    # 主机状态事件本身不是归因指标;mem_pct 事件本轮没被通知 → 都不该提交
    assert submitted == []
    submit.assert_not_called()
    _ = metric_event


def test_cooldown_repeat_notification_reuses_completed_analysis(env):
    """冷却到期的重复通知不重跑归因(闸门拒绝),但必须**立即**放行——
    通知带着已有的归因结论发出,不能退化为看门狗 30~60s 后才放行。"""
    from app.services.alerts import hold_created_notification

    report = "sqlservr 占用 79% 内存，建议限制最大服务器内存。"
    event = env.make_event(
        metric="mem_pct",
        value=96.0,
        threshold=90.0,
        device_id=env.device.id,
        resource_id=str(env.device.id),
        resource_name=env.device.name,
        notification_status="pending",
        analysis_state="completed",
        analysis_text=report,
        agent_run_id=0,
    )
    assert hold_created_notification(event.id) is True

    released: list[int] = []
    with (
        patch("app.services.remediation.get_agent_config") as get_cfg,
        patch("app.services.remediation.start_diagnosis") as start,
        patch(
            "app.services.remediation.release_held_notification",
            side_effect=lambda event_id, **_kw: released.append(event_id) or True,
        ),
    ):
        get_cfg.return_value = SimpleNamespace(ready=True)
        result = remediation.analyze_event(event.id)

    # 不会重跑归因(没启动任何新诊断),拒绝原因是「已分析过」
    assert result["ok"] is False and result["concurrent"] is True
    start.assert_not_called()
    # 但放行立即发生:第二张卡片带着已有结论发出
    assert released == [event.id]


def test_host_status_offline_sends_immediate_card(env):
    """host_status 两步模型:离线**立刻**发卡,详情写明「正在触发自动拉起
    操作」;拉起成功的第二条卡由终态(remediate_event → succeeded)推送,
    文案为「自动拉起操作成功，目标已恢复运行」。中间不扣住通知。"""
    from app.services.alerts import hold_created_notification

    event = env.make_event(notification_status="pending")

    delivered: list[dict] = []

    def _capture(event_id, event_name, **kwargs):
        delivered.append({"name": event_name})

    target = remediation.Target(
        kind="pve_guest",
        label="PVE 虚拟机 [pve] QEMU 101 web-01",
        connection_id=env.conn.id,
        node="pve1",
        guest_type="qemu",
        vmid=101,
    )
    with (
        patch("app.services.alerts.notify_event", side_effect=_capture),
        patch("app.services.remediation.resolve_target", return_value=target),
        patch(
            "app.services.remediation._pve_client",
            return_value=SimpleNamespace(power=lambda *a, **k: None),
        ),
        patch("app.services.remediation.time", _FastTime),
        patch("app.services.remediation._verify_target", return_value=True),
    ):
        result = remediation.remediate_event(event.id, manual=True)

    assert result["ok"] is True
    row = _reload(env.db, event.id)
    assert (row.remediation_json or {}).get("notification_held") is None
    # 两步的第二条:拉起成功的终态卡,文案按用户口径
    assert row.message.endswith("自动拉起操作成功，目标已恢复运行")
    _ = hold_created_notification


def test_pve_guest_host_status_skips_unreachable_platform(env):
    """PVE 不可达时快照评估整体跳过该平台:不误触发离线,也不收敛既有事件。"""
    from app.services.alerts import evaluate_pve_guest_host_status

    captured: list[dict] = []
    with (
        patch(
            "app.services.alerts.get_connection_state",
            return_value={
                "name": env.conn.name,
                "reachable": False,
                "error": "x",
                "checked_at": None,
            },
        ),
        patch(
            "app.services.alerts._evaluate_resource_rules", side_effect=captured.extend
        ),
    ):
        evaluate_pve_guest_host_status()
    assert captured == []


def test_guest_ip_falls_back_to_qga(env):
    """无运维绑定的虚机:地址字段兜底 QGA 实时地址,停机(QGA 无应答)时
    回退「最近已知 IP」——QGA-only 虚机的地址列不至于退回 '-'。"""
    from app.services.alerts import _guest_binding_ip, _guest_binding_ips
    from app.services.pve_guest_status import _last_known_ip

    # 无绑定:批量解析拿不到,单条路径兜底 QGA
    event = env.make_event(
        device_id=None,
        resource_id=str(-(env.conn.id * PVE_ID_FACTOR + 101)),
        resource_name=f"[{env.conn.name}] qga-vm",
    )
    _last_known_ip.clear()
    assert _guest_binding_ips(env.db, [event]).get(event.id) is None
    snap = {
        "vmid": 101,
        "name": "qga-vm",
        "status": "running",
        "node": "pve1",
        "guest_type": "qemu",
    }
    with (
        patch("app.services.pve_guest_status.get_guest_snapshot", return_value=snap),
        patch("app.services.alerts.build_client", return_value=SimpleNamespace()),
        patch(
            "app.services.alerts.guest_agent_summary",
            return_value={"qga_available": True, "qga_ip_address": "10.7.0.99"},
        ),
    ):
        assert _guest_binding_ip(env.db, event) == "10.7.0.99"
        # 批量路径(告警中心列表)也回退最近已知 IP
        assert _guest_binding_ips(env.db, [event]).get(event.id) == "10.7.0.99"
    # QGA 结果带 5 分钟缓存,下一段断言前清掉
    from app.services import alerts as _alerts

    _alerts._qga_ip_cache.clear()
    # guest 停机:QGA 无应答,但运行期问到的地址已记入「最近已知」,
    # 地址列继续显示(主机状态告警恰好在停机时点触发,靠的就是这层兜底)
    with (
        patch("app.services.pve_guest_status.get_guest_snapshot", return_value=snap),
        patch("app.services.alerts.build_client", return_value=SimpleNamespace()),
        patch(
            "app.services.alerts.guest_agent_summary",
            return_value={"qga_available": False, "qga_ip_address": None},
        ),
    ):
        assert _guest_binding_ip(env.db, event) == "10.7.0.99"
    # QGA 不可用且没有任何已知 IP → None(卡片地址显示 '-')
    _last_known_ip.clear()
    _alerts._qga_ip_cache.clear()
    with (
        patch("app.services.pve_guest_status.get_guest_snapshot", return_value=snap),
        patch("app.services.alerts.build_client", return_value=SimpleNamespace()),
        patch(
            "app.services.alerts.guest_agent_summary",
            return_value={"qga_available": False, "qga_ip_address": None},
        ),
    ):
        assert _guest_binding_ip(env.db, event) is None
    _alerts._qga_ip_cache.clear()
    _last_known_ip.clear()


def test_container_payload_object_and_address_fields(env):
    """容器告警卡片口径(2026-09-16):「对象」只显示容器名,「地址」显示
    宿主(服务器/虚拟机)的名称或 IP。"""
    from app.services.alerts import _payload

    # 虚机容器:无 Device 行,地址用 guest_ip(运维接入/QGA)
    vm_event = env.make_event(
        metric="container_status",
        resource_type="container",
        resource_id="deadbeef0000",
        resource_name="SRM/web-guest",
        device_id=None,
        remediation_json={"target_id": -(env.conn.id * PVE_ID_FACTOR + 101)},
    )
    data = _payload(vm_event, None, None, "alert.created", guest_ip="10.7.0.21")
    assert data["device"]["name"] == "web-guest"
    assert data["device"]["ip_address"] == "10.7.0.21"
    assert data["device"]["type"] == "container"
    assert data["device_name"] == "web-guest" and data["device_ip"] == "10.7.0.21"

    # 虚机容器无 IP:地址回退宿主名称(resource_name 的宿主前缀)
    data = _payload(vm_event, None, None, "alert.created", guest_ip=None)
    assert data["device"]["name"] == "web-guest"
    assert data["device"]["ip_address"] == "SRM"

    # 设备容器:Device 行提供 IP,对象仍是容器名
    dev_event = env.make_event(
        metric="container_status",
        resource_type="container",
        resource_id="abc123",
        resource_name=f"{env.device.name}/web",
        device_id=env.device.id,
    )
    data = _payload(dev_event, None, env.device, "alert.created", guest_ip=None)
    assert data["device"]["name"] == "web"
    assert data["device"]["ip_address"] == env.device.ip_address


def test_host_status_message_carries_ip(env):
    """离线告警详情话术带 IP:虚机取运维接入/QGA(带缓存),设备取 Device 行。"""
    import time as _time

    from app.services.alerts import evaluate_pve_guest_host_status

    captured: list[dict] = []
    state = {
        "name": env.conn.name,
        "reachable": True,
        "error": None,
        "checked_at": _time.time(),
    }
    guests = {
        101: {"vmid": 101, "name": "web-01", "status": "stopped", "node": "pve1"},
    }
    with (
        patch("app.services.alerts.get_connection_state", return_value=state),
        patch("app.services.alerts.get_guests_by_connection", return_value=guests),
        patch(
            "app.services.alerts._guest_display_ip",
            side_effect=lambda db, conn_id, vmid: "10.7.0.21" if vmid == 101 else None,
        ),
        patch(
            "app.services.alerts._evaluate_resource_rules", side_effect=captured.extend
        ),
    ):
        evaluate_pve_guest_host_status()
    ours = next(item for item in captured if item["name"].startswith(env.conn.name))
    assert ours["message"] == "虚拟机 web-01(10.7.0.21) 当前离线或未运行"

    # 拿不到 IP(无绑定且 QGA 不可用)时退回不带括号的原话术
    captured.clear()
    with (
        patch("app.services.alerts.get_connection_state", return_value=state),
        patch("app.services.alerts.get_guests_by_connection", return_value=guests),
        patch("app.services.alerts._guest_display_ip", return_value=None),
        patch(
            "app.services.alerts._evaluate_resource_rules", side_effect=captured.extend
        ),
    ):
        evaluate_pve_guest_host_status()
    ours = next(item for item in captured if item["name"].startswith(env.conn.name))
    assert ours["message"] == "虚拟机 web-01 当前离线或未运行"


def test_notify_filters_by_webhook_severity_config(env):
    """级别路由:webhook 配了 config.severities 只收所选级别,没配不过滤。

    critical 告警 → 只配 [critical] 的钩子和未配置的钩子都投;
    warning 告警 → 只配 [critical] 的钩子被滤掉。旧钩子(无该键)行为不变。
    只统计本用例创建的钩子:开发库里有真实启用的 webhook(AGENTS.md 门禁约定)。
    """
    crit_only = Webhook(
        name=f"sev-crit-{env.stamp}",
        url="https://example.invalid/crit",
        provider="generic",
        events=["alert.created"],
        headers={},
        config={"severities": ["critical"]},
        enabled=True,
    )
    no_filter = Webhook(
        name=f"sev-any-{env.stamp}",
        url="https://example.invalid/any",
        provider="generic",
        events=["alert.created"],
        headers={},
        enabled=True,
    )
    env.db.add_all([crit_only, no_filter])
    env.db.commit()
    env.extra["webhooks"].extend([crit_only.id, no_filter.id])
    mine = {crit_only.id, no_filter.id}

    crit_event = env.make_event(severity="critical")
    warn_event = env.make_event(severity="warning")

    def _delivered(event) -> set[tuple[int, str]]:
        sent: set[tuple[int, str]] = set()

        def _capture(webhook, payload):
            if webhook.id in mine:
                sent.add((webhook.id, payload["alert"]["severity"]))
            return {
                "webhook_id": webhook.id,
                "name": webhook.name,
                "ok": True,
                "status_code": 200,
            }

        with (
            patch("app.services.alerts._notification_pool", _SyncPool()),
            patch("app.services.alerts._deliver_one", side_effect=_capture),
        ):
            # track=False:_notify 不改事件行,测试纯只读
            _notify(env.db, event, env.rule, None, "alert.created", track=False)
        return sent

    assert _delivered(crit_event) == {
        (crit_only.id, "critical"),
        (no_filter.id, "critical"),
    }
    assert _delivered(warn_event) == {(no_filter.id, "warning")}


def test_physical_device_host_status_skips_auto_remediation(env):
    """物理设备(host_status,device_id 非空)不进自动拉起,消息不拼拉起话术。

    机房设备无法远程上电:旧逻辑会白跑一遍闸门+解析,最后只留一条 skip 提示;
    消息里的「正在触发自动拉起操作」对物理设备纯属误导(2026-09-16 用户定调)。
    虚拟机(device_id=None)与容器不受影响——由同文件的既有用例钉住。
    """
    # 1) 物理设备离线:不提交拉起,消息干净
    resource = {
        "type": "device",
        "id": env.device.id,
        "target_id": env.device.id,
        "device_id": env.device.id,
        "name": env.device.name,
        "metric": "host_status",
        "value": 1,
        "message": f"主机 {env.device.name} 当前已离线或无法连接",
    }
    submitted: list[int] = []
    with (
        patch("app.services.alerts.submit_remediation", side_effect=submitted.append),
        patch("app.services.alerts._notify_event_in_background"),
    ):
        _evaluate_resource_rules([resource])
    # 库里遗留/并发的 open 事件(与容器评估循环共享开发库)会被顺手提交,
    # 那是全库语义的正确行为;本用例只钉「本用例规则的物理设备事件不进拉起」。
    my_ids = _my_rule_event_ids(env)
    assert [event_id for event_id in submitted if event_id in my_ids] == []
    # 评估器在独立会话里提交;先结束本会话快照再读
    env.db.commit()
    env.db.expire_all()
    rows = env.db.query(AlertEvent).filter(AlertEvent.device_id == env.device.id).all()
    assert rows and all("正在触发自动拉起操作" not in (r.message or "") for r in rows)
    # 也不留任何处置状态(从未进过流程)
    assert all((r.remediation_state or "") == "" for r in rows)

    # 2) 同一台设备下一轮恢复:事件正常收敛(排除拉起不影响恢复闭环)
    resource["value"] = 0
    with (
        patch("app.services.alerts.submit_remediation", side_effect=submitted.append),
        patch("app.services.alerts._notify_event_in_background"),
    ):
        _evaluate_resource_rules([resource])
    my_ids = _my_rule_event_ids(env)
    assert [event_id for event_id in submitted if event_id in my_ids] == []
    env.db.commit()
    env.db.expire_all()
    rows = env.db.query(AlertEvent).filter(AlertEvent.device_id == env.device.id).all()
    assert all(r.status == "resolved" for r in rows)
