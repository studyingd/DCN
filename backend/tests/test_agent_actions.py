"""agent_actions 分级动作测试:注册表钉住、审批环全路径、预算/重提拦截、恢复收敛。

红线:审批人批准的 command 是提议时定稿的不可变字符串;L1 免审集与自愈同源;
超时默认拒绝;被拒动作不得重提;恢复路径绝不自动补执行写操作。
"""

import json
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.database import Base, SessionLocal, engine
from app.models.agent_action_approval import AgentActionApproval
from app.models.agent_run import AgentRun
from app.models.device import Device
from app.models.rack import Rack
from app.models.room import Room
from app.services import agent
from app.services import agent_actions as aa
from app.services.agent_primitives import PrimitiveError

_FAKE_LINUX = SimpleNamespace(is_windows=False)


class _LinuxDev:
    is_windows = False


class _WinDev:
    is_windows = True


# ── 注册表:命令钉住与参数拒绝 ──


def test_action_commands_pinned_linux():
    assert (
        aa.build_action("container_start", {"name": "web-1"}, _LinuxDev())["command"]
        == "docker start web-1"
    )
    assert (
        aa.build_action("container_restart", {"name": "web-1"}, _LinuxDev())["command"]
        == "docker restart web-1"
    )
    assert (
        aa.build_action("service_start", {"unit": "nginx.service"}, _LinuxDev())[
            "command"
        ]
        == "systemctl start nginx.service"
    )
    assert (
        aa.build_action("service_restart", {"unit": "nginx.service"}, _LinuxDev())[
            "command"
        ]
        == "systemctl restart nginx.service"
    )


def test_action_commands_pinned_windows():
    # Restart/Stop 一律不带 -Force:有依赖服务时宁可用失败输出当证据
    assert (
        aa.build_action("win_service_restart", {"name": "WinRM"}, _WinDev())["command"]
        == "Restart-Service -Name 'WinRM'"
    )
    assert (
        aa.build_action("container_stop", {"name": "web-1"}, _WinDev())["command"]
        == "docker stop web-1"
    )


@pytest.mark.parametrize(
    "key,params,dev",
    [
        ("container_start", {"name": "web; rm -rf /"}, _LinuxDev()),
        ("container_start", {"name": "web$(id)"}, _LinuxDev()),
        ("container_start", {}, _LinuxDev()),
        ("service_restart", {"unit": "a; poweroff"}, _LinuxDev()),
        ("win_service_stop", {"name": "x'y"}, _WinDev()),
        ("service_start", {"unit": "nginx.service"}, _WinDev()),  # 平台不匹配
        ("win_service_start", {"name": "WinRM"}, _LinuxDev()),
    ],
)
def test_action_params_rejected(key, params, dev):
    with pytest.raises(PrimitiveError):
        aa.build_action(key, params, dev)


def test_action_risk_mapping():
    cat = aa.actions_catalog_for(_LinuxDev())
    assert cat["container_start"]["risk"] == "l1"
    assert cat["service_start"]["risk"] == "l1"
    assert cat["container_restart"]["risk"] == "l2"
    assert cat["service_stop"]["risk"] == "l2"


# ── 审批环:_dispatch 全路径(真实 DB 会话 + mock 执行层) ──


@pytest.fixture()
def run_row():
    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    room = Room(name=f"act-{datetime.now(timezone.utc).timestamp()}", location="F1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name="Rack-A", type="cabinet")
    db.add(rack)
    db.flush()
    dev = Device(
        rack_id=rack.id,
        name=f"srv-act-{datetime.now(timezone.utc).timestamp()}",
        type="server",
        ip_address="10.9.9.9",
        os_system="Rocky Linux 9.4",
        status="online",
    )
    db.add(dev)
    db.flush()
    run = AgentRun(
        user_id=None,
        device_id=dev.id,
        device_name=dev.name,
        device_ip=dev.ip_address,
        question="restart test",
        status="running",
        mode="standard",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id, dev_id, rack_id, room_id = run.id, dev.id, rack.id, room.id
    db.close()
    try:
        yield SimpleNamespace(id=run_id, device_id=dev_id)
    finally:
        db = SessionLocal()
        try:
            db.query(AgentActionApproval).filter(
                AgentActionApproval.run_id == run_id
            ).delete()
            db.query(AgentRun).filter(AgentRun.id == run_id).delete()
            db.query(Device).filter(Device.id == dev_id).delete()
            db.query(Rack).filter(Rack.id == rack_id).delete()
            db.query(Room).filter(Room.id == room_id).delete()
            db.commit()
        finally:
            db.close()


def _action_call(key, params, reason="内存泄漏,重启回收"):
    return {
        "id": "a1",
        "function": {
            "name": "run_action",
            "arguments": json.dumps({"key": key, "params": params, "reason": reason}),
        },
    }


def _pending_approval_id(run_id):
    db = SessionLocal()
    try:
        row = (
            db.query(AgentActionApproval)
            .filter(AgentActionApproval.run_id == run_id)
            .order_by(AgentActionApproval.id.desc())
            .first()
        )
        return row.id if row else None
    finally:
        db.close()


def _decide_when_pending(run_id, approve, note=""):
    def _worker():
        for _ in range(150):
            aid = _pending_approval_id(run_id)
            if aid:
                db = SessionLocal()
                try:
                    agent.decide_approval(db, aid, approve, user_id=None, note=note)
                finally:
                    db.close()
                return
            time.sleep(0.1)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def test_run_action_readonly_refused(run_row):
    steps: list[dict] = []
    out = json.loads(
        agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call("service_restart", {"unit": "nginx.service"}),
            steps,
            mode="readonly",
        )
    )
    assert "只读模式" in out["error"]
    assert steps[-1]["final"] == "refused"


def test_missing_reason_refused(run_row):
    steps: list[dict] = []
    out = json.loads(
        agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call("service_restart", {"unit": "nginx.service"}, reason=""),
            steps,
            mode="standard",
            l1_auto=True,
            run_id=run_row.id,
        )
    )
    assert "reason" in out["error"]


def test_l1_auto_executes_with_redaction(run_row, monkeypatch):
    captured = {}

    def fake_exec(db, dev, command, timeout, session=None):
        captured["command"] = command
        return 0, "TOKEN=supersecret\nstarted", ""

    monkeypatch.setattr(agent, "_exec_diagnostic", fake_exec)
    steps: list[dict] = []
    out = json.loads(
        agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call("container_start", {"name": "web-1"}),
            steps,
            mode="standard",
            l1_auto=True,
            run_id=run_row.id,
        )
    )
    assert captured["command"] == "docker start web-1"
    assert out["exit_code"] == 0
    assert "supersecret" not in out["output"]  # 动作输出同样脱敏
    assert steps[-1]["auto"] is True and steps[-1]["final"] == "executed"


def test_l2_approved_then_executes(run_row, monkeypatch):
    monkeypatch.setattr(
        agent,
        "_exec_diagnostic",
        lambda db, dev, command, timeout, session=None: (0, "restarted", ""),
    )
    _decide_when_pending(run_row.id, True)
    steps: list[dict] = []
    out = json.loads(
        agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call("service_restart", {"unit": "nginx.service"}),
            steps,
            mode="standard",
            l1_auto=True,
            run_id=run_row.id,
        )
    )
    assert out.get("approved") is True and out["exit_code"] == 0
    assert steps[-1]["final"] == "executed" and steps[-1]["approval_id"]
    db = SessionLocal()
    try:
        row = (
            db.query(AgentActionApproval)
            .filter(AgentActionApproval.run_id == run_row.id)
            .first()
        )
        assert row.status == "executed"
        # 审批人批准的就是提议时定稿的不可变字符串
        assert row.command == "systemctl restart nginx.service"
    finally:
        db.close()


def test_l2_rejected_then_retry_blocked(run_row, monkeypatch):
    monkeypatch.setattr(agent, "AGENT_ACTION_APPROVAL_TIMEOUT", 5)
    _decide_when_pending(run_row.id, False, note="风险大")
    steps: list[dict] = []
    out = json.loads(
        agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call("service_restart", {"unit": "nginx.service"}),
            steps,
            mode="standard",
            l1_auto=True,
            run_id=run_row.id,
        )
    )
    assert out["status"] == "rejected" and "风险大" in out["detail"]
    assert steps[-1]["final"] == "rejected"

    steps2: list[dict] = list(steps)
    out2 = json.loads(
        agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call(
                "service_restart", {"unit": "nginx.service"}, reason="再试一次"
            ),
            steps2,
            mode="standard",
            l1_auto=True,
            run_id=run_row.id,
        )
    )
    assert "不得重提" in out2["error"]


def test_l2_timeout_expires(run_row, monkeypatch):
    monkeypatch.setattr(agent, "AGENT_ACTION_APPROVAL_TIMEOUT", 1)
    steps: list[dict] = []
    out = json.loads(
        agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call("service_stop", {"unit": "nginx.service"}),
            steps,
            mode="standard",
            l1_auto=True,
            run_id=run_row.id,
        )
    )
    assert out["status"] == "expired"
    db = SessionLocal()
    try:
        row = (
            db.query(AgentActionApproval)
            .filter(AgentActionApproval.run_id == run_row.id)
            .first()
        )
        assert row.status == "expired"
    finally:
        db.close()


def test_action_budget_exhausted(run_row, monkeypatch):
    monkeypatch.setattr(agent, "AGENT_ACTION_LIMIT", 1)
    monkeypatch.setattr(
        agent,
        "_exec_diagnostic",
        lambda db, dev, command, timeout, session=None: (0, "ok", ""),
    )
    steps: list[dict] = []
    out1 = json.loads(
        agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call("container_start", {"name": "web-1"}),
            steps,
            mode="standard",
            l1_auto=True,
            run_id=run_row.id,
        )
    )
    assert out1.get("exit_code") == 0
    out2 = json.loads(
        agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call("container_start", {"name": "web-2"}),
            steps,
            mode="standard",
            l1_auto=True,
            run_id=run_row.id,
        )
    )
    assert "预算" in out2["error"]


def test_strict_mode_l1_requires_approval(run_row, monkeypatch):
    """strict 模式下 L1 也走审批。"""
    monkeypatch.setattr(agent, "AGENT_ACTION_APPROVAL_TIMEOUT", 5)
    monkeypatch.setattr(
        agent,
        "_exec_diagnostic",
        lambda db, dev, command, timeout, session=None: (0, "started", ""),
    )
    _decide_when_pending(run_row.id, True)
    steps: list[dict] = []
    out = json.loads(
        agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call("container_start", {"name": "web-1"}),
            steps,
            mode="strict",
            l1_auto=True,
            run_id=run_row.id,
        )
    )
    assert out.get("approved") is True
    assert steps[-1]["approval_id"]


# ── 决策幂等与恢复收敛 ──


def test_decide_approval_idempotent(run_row, monkeypatch):
    monkeypatch.setattr(agent, "AGENT_ACTION_APPROVAL_TIMEOUT", 5)
    monkeypatch.setattr(
        agent,
        "_exec_diagnostic",
        lambda db, dev, command, timeout, session=None: (0, "ok", ""),
    )
    t = threading.Thread(
        target=lambda: agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call("service_stop", {"unit": "nginx.service"}),
            [],
            mode="standard",
            l1_auto=True,
            run_id=run_row.id,
        ),
        daemon=True,
    )
    t.start()
    aid = None
    for _ in range(150):
        aid = _pending_approval_id(run_row.id)
        if aid:
            break
        time.sleep(0.1)
    db = SessionLocal()
    try:
        row = agent.decide_approval(db, aid, True, user_id=None)
        assert row is not None and row.status == "approved"
        # 第二次决策(并发双击)返回 None → 路由层 409
        assert agent.decide_approval(db, aid, True, user_id=None) is None
    finally:
        db.close()
    t.join(timeout=15)


def test_finalize_dangling_approvals(run_row):
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        p = AgentActionApproval(
            run_id=run_row.id,
            action_key="service_restart",
            params_json="{}",
            command="systemctl restart nginx",
            risk_level="l2",
            reason="r",
            status="pending",
            requested_at=now,
            expires_at=now,
        )
        a = AgentActionApproval(
            run_id=run_row.id,
            action_key="container_restart",
            params_json="{}",
            command="docker restart web",
            risk_level="l2",
            reason="r",
            status="approved",
            requested_at=now,
            expires_at=now,
        )
        e = AgentActionApproval(
            run_id=run_row.id,
            action_key="container_stop",
            params_json="{}",
            command="docker stop web",
            risk_level="l2",
            reason="r",
            status="executed",
            requested_at=now,
            expires_at=now,
        )
        db.add_all([p, a, e])
        db.commit()
        n = agent._finalize_dangling_approvals(db, run_row.id)
        db.expire_all()
        assert n == 2
        assert db.get(AgentActionApproval, p.id).status == "expired"
        # approved 未执行 → execution_failed,绝不自动补执行
        assert db.get(AgentActionApproval, a.id).status == "execution_failed"
        assert db.get(AgentActionApproval, e.id).status == "executed"  # 终态不动
    finally:
        db.close()


# ── custom_read:白名单外一次性只读命令(静态只读屏 + 审批双闸) ──


def test_custom_read_build_valid():
    cmd = "grep -c -i error /var/log/messages"
    out = aa.build_action("custom_read", {"command": cmd}, _LinuxDev())
    assert out["command"] == cmd
    assert out["risk"] == "l2"  # 永远走审批
    # PowerShell 只读命令同样放行(静态屏按平台选模式集)
    ps = aa.build_action(
        "custom_read", {"command": "Get-Content C:\\x\\y.log -TotalCount 5"}, _WinDev()
    )
    assert ps["command"].startswith("Get-Content")


@pytest.mark.parametrize(
    "params,dev",
    [
        ({"command": "rm -rf /tmp/x"}, _LinuxDev()),
        ({"command": "cat a > /tmp/b"}, _LinuxDev()),  # 重定向写文件
        ({"command": "echo $(id)"}, _LinuxDev()),  # 命令替换
        ({"command": "Remove-Item x"}, _WinDev()),
        ({"command": ""}, _LinuxDev()),
        ({"command": None}, _LinuxDev()),
        ({"command": "a\nc"}, _LinuxDev()),  # 多行
        ({"command": "x" * 501}, _LinuxDev()),
    ],
)
def test_custom_read_rejected(params, dev):
    with pytest.raises(PrimitiveError):
        aa.build_action("custom_read", params, dev)


def test_custom_read_in_catalog_always_l2():
    for dev in (_LinuxDev(), _WinDev()):
        cat = aa.actions_catalog_for(dev)
        assert cat["custom_read"]["risk"] == "l2"
        assert cat["custom_read"]["param_names"] == ["command"]


def test_custom_read_never_auto_even_standard(run_row, monkeypatch):
    """standard + L1 免审开启:custom_read 仍走审批(risk 恒为 l2)。"""
    monkeypatch.setattr(agent, "AGENT_ACTION_APPROVAL_TIMEOUT", 5)
    monkeypatch.setattr(
        agent,
        "_exec_diagnostic",
        lambda db, dev, command, timeout, session=None: (0, "42", ""),
    )
    _decide_when_pending(run_row.id, True)
    steps: list[dict] = []
    out = json.loads(
        agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call(
                "custom_read",
                {"command": "grep -c -i error /var/log/messages"},
                reason="统计近段错误日志量,目录内没有等价项",
            ),
            steps,
            mode="standard",
            l1_auto=True,
            run_id=run_row.id,
        )
    )
    assert out.get("approved") is True and out["output"] == "42"
    assert steps[-1]["final"] == "executed" and steps[-1]["approval_id"]
    db = SessionLocal()
    try:
        row = (
            db.query(AgentActionApproval)
            .filter(AgentActionApproval.run_id == run_row.id)
            .first()
        )
        # 审批人批准的 = 执行的 = 审计的同一字符串(LLM 原样给的命令)
        assert row.command == "grep -c -i error /var/log/messages"
        assert row.risk_level == "l2"
    finally:
        db.close()


# ── 时间线可见性(2026-10-30 修):等待审批期间步骤必须已落库 ──


def test_pending_step_persisted_before_decision(run_row, monkeypatch):
    """审批卡渲染依赖 steps 里的 pending 行:若 on_step 只在 _dispatch 返回后
    触发,event.wait 阻塞的整个窗口里前端只见「等待审批」标签、见不到审批卡
    与按钮——所以 pending 步骤必须在开始等待**之前**先落库。"""
    monkeypatch.setattr(agent, "AGENT_ACTION_APPROVAL_TIMEOUT", 5)
    snapshots: list[list[dict]] = []
    decided = threading.Event()

    def on_step(cur):
        snapshots.append([dict(s) for s in cur])

    def _decide_after_visible():
        for _ in range(150):
            if any(
                s.get("final") == "pending" and s.get("approval_id")
                for snap in snapshots
                for s in snap
            ):
                db = SessionLocal()
                try:
                    aid = _pending_approval_id(run_row.id)
                    agent.decide_approval(db, aid, True, user_id=None, note="")
                finally:
                    db.close()
                decided.set()
                return
            time.sleep(0.1)

    threading.Thread(target=_decide_after_visible, daemon=True).start()
    monkeypatch.setattr(
        agent,
        "_exec_diagnostic",
        lambda db, dev, command, timeout, session=None: (0, "started", ""),
    )
    steps: list[dict] = []
    out = json.loads(
        agent._dispatch(
            SessionLocal(),
            _FAKE_LINUX,
            {},
            _action_call("service_start", {"unit": "nginx.service"}),
            steps,
            mode="strict",  # 固定 strict 语义:l1 拉起类也走审批
            run_id=run_row.id,
            on_step=on_step,
        )
    )
    assert decided.is_set(), "决策未被触发——pending 步骤从未在等待前落库"
    assert out.get("approved") is True and out["exit_code"] == 0
    assert steps[-1]["final"] == "executed"
    first_pending = next(
        snap for snap in snapshots if any(s.get("final") == "pending" for s in snap)
    )
    assert any(s.get("approval_id") for s in first_pending)


def test_run_diagnostic_step_records_command(run_row, monkeypatch):
    """只读取证步骤必须携带命令原文(2026-10-30):时间线要展示「执行了什么
    指令」;命令是服务端白名单拼装,随步骤落库无风险。"""
    monkeypatch.setattr(
        agent,
        "_exec_diagnostic",
        lambda db, dev, command, timeout, session=None: (0, "out", ""),
    )
    steps: list[dict] = []
    catalog = {"mem_free": {"label": "内存占用", "command": "free -m", "timeout": 10}}
    agent._dispatch(
        SessionLocal(),
        _FAKE_LINUX,
        catalog,
        {
            "id": "c1",
            "function": {
                "name": "run_diagnostic",
                "arguments": json.dumps({"key": "mem_free"}),
            },
        },
        steps,
    )
    assert steps[-1]["command"] == "free -m"
    assert steps[-1]["ok"] is True
