"""Agent 诊断循环测试(mock LLM,验证只读边界在运行时真实生效)。

核心断言:
  * LLM 点名的注册表 key 才会执行;
  * 幻觉 key / 任意命令字符串永远到不了设备执行层;
  * 每一步都落 agent_runs 审计。
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models.agent_run import AgentRun
from app.models.device import Device
from app.models.rack import Rack
from app.models.role import Role
from app.models.room import Room
from app.models.user import User
from app.services import agent as agent_service
from app.services.auth import create_access_token


class _FakeResp:
    def __init__(self, payload: dict | None, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or (json.dumps(payload) if payload else "")

    def json(self):
        return self._payload


def _tool_call_msg(call_id: str, name: str, args: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            }
        ]
    }


def _final_msg(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.fixture()
def device():
    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    room = Room(name="Agent Room", location="F1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name="Rack-A", type="cabinet")
    db.add(rack)
    db.flush()
    dev = Device(
        rack_id=rack.id,
        name=f"srv-agent-{datetime.now(timezone.utc).timestamp()}",
        type="server",
        ip_address="10.9.0.1",
        os_system="Rocky Linux 9.4",
        status="online",
    )
    db.add(dev)
    db.commit()
    try:
        yield dev
    finally:
        db.query(AgentRun).filter(AgentRun.device_id == dev.id).delete()
        db.query(Device).filter(Device.id == dev.id).delete()
        db.query(Rack).filter(Rack.id == rack.id).delete()
        db.query(Room).filter(Room.id == room.id).delete()
        db.commit()
        db.close()


def test_agent_loop_executes_only_registry_keys(device):
    """LLM 幻觉出的 key 被拒绝且不触设备;注册表 key 正常执行;全程落审计。"""
    llm_script = [
        _tool_call_msg("c1", "list_diagnostics", {}),
        _tool_call_msg("c2", "run_diagnostic", {"key": "memory"}),
        _tool_call_msg("c3", "run_diagnostic", {"key": "rm_rf_root"}),  # 幻觉 key
        _final_msg("## 结论\n内存正常。"),
    ]
    sent_payloads: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):  # noqa: A002
        sent_payloads.append(json)
        return _FakeResp(llm_script[len(sent_payloads) - 1])

    exec_calls: list[str] = []

    def fake_exec(db: Session, dev, command: str, timeout: int, session=None):
        exec_calls.append(command)
        return 0, "Mem: 8000 total 4000 used", ""

    with (
        patch.object(agent_service.requests, "post", side_effect=fake_post),
        patch.object(agent_service, "_exec_diagnostic", side_effect=fake_exec),
    ):
        cfg = agent_service.AgentConfig(
            enabled=True,
            base_url="http://llm-gw/v1",
            api_key="test-key",
            model="test-model",
            max_steps=8,
        )
        result = agent_service.run_diagnosis(
            device, "看看内存有没有问题", user_id=None, cfg=cfg
        )

    # 循环正常完成,报告透传
    assert result["status"] == "completed"
    assert "内存正常" in result["report"]

    # 设备执行层只被调用一次——注册表里的 memory 命令原文
    assert len(exec_calls) == 1
    assert exec_calls[0] == "free -m"

    # 步骤:memory 成功;幻觉 key 被拒绝
    assert len(result["steps"]) == 2
    assert result["steps"][0]["ok"] is True
    rejected = result["steps"][1]
    assert rejected["key"] == "rm_rf_root"
    assert rejected["ok"] is False
    assert "不存在" in rejected["error"]

    # 幻觉 key 的 tool 结果反馈给 LLM 的也是拒绝信息
    last_msgs = sent_payloads[-1]["messages"]
    tool_msgs = [m for m in last_msgs if m.get("role") == "tool"]
    assert any("不存在" in m["content"] for m in tool_msgs)

    # 审计落库
    db = SessionLocal()
    try:
        row = db.query(AgentRun).filter(AgentRun.id == result["run_id"]).first()
        assert row is not None
        assert row.status == "completed"
        assert row.report and "内存正常" in row.report
        steps = json.loads(row.steps_json)
        assert len(steps) == 2
    finally:
        db.close()


def test_agent_llm_failure_marks_run_failed(device):
    """LLM 接口报错:诊断标记 failed 并落审计,不抛未处理异常。"""
    with (
        patch.object(
            agent_service.requests,
            "post",
            return_value=_FakeResp(None, status_code=500, text="upstream boom"),
        ),
        # 重试退避真实 sleep 会拖慢测试;这里只关心最终失败
        patch.object(agent_service.time, "sleep", lambda *_: None),
    ):
        cfg = agent_service.AgentConfig(
            enabled=True,
            base_url="http://llm-gw/v1",
            api_key="test-key",
            model="test-model",
            max_steps=8,
        )
        result = agent_service.run_diagnosis(device, "体检", user_id=None, cfg=cfg)

    assert result["status"] == "failed"
    assert "500" in (result["error"] or "")

    db = SessionLocal()
    try:
        row = db.query(AgentRun).filter(AgentRun.id == result["run_id"]).first()
        assert row.status == "failed"
        assert "500" in (row.error or "")
    finally:
        db.close()


def test_agent_catalog_is_narrowed_for_specific_questions(device):
    """具体查询只暴露相关诊断项，避免模型顺手执行系统体检。"""
    catalog = agent_service._select_catalog(device, "查询 Traefik 路由")
    assert set(catalog) == {"traefik_routes", "traefik_runtime"}
    assert "cpu" not in catalog
    assert "disk" not in catalog

    catalog = agent_service._select_catalog(device, "看看 CPU 使用")
    assert "cpu" in catalog
    assert "disk" not in catalog
    assert "established_top" not in catalog

    catalog = agent_service._select_catalog(device, "全面检查系统健康")
    assert "cpu" in catalog
    assert "disk" in catalog
    assert "established_top" in catalog

    catalog = agent_service._select_catalog(device, "查看 Docker Compose 容器状态")
    assert set(catalog) == {
        "docker_runtime",
        "docker_processes",
        "docker_logs",
        "docker_stats",
    }

    prompt = agent_service._build_system_prompt({"traefik_routes": {"label": "x"}})
    assert "Traefik 专项规则" in prompt
    assert "已观测事实" in prompt


def test_agent_rejects_duplicate_diagnostic(device):
    """同一轮中重复点名同一个 key 时不应再次触达设备。"""
    steps: list[dict] = []
    calls: list[str] = []

    def fake_exec(db, dev, command, timeout, session=None):
        calls.append(command)
        return 0, "ok", ""

    with patch.object(agent_service, "_exec_diagnostic", side_effect=fake_exec):
        catalog = agent_service._select_catalog(device, "查询内存")
        executed: set[str] = set()
        call = {
            "function": {
                "name": "run_diagnostic",
                "arguments": json.dumps({"key": "memory"}),
            }
        }
        agent_service._dispatch(None, device, catalog, call, steps, executed, 2)
        result = agent_service._dispatch(
            None, device, catalog, call, steps, executed, 2
        )

    assert calls == ["free -m"]
    assert "无需重复执行" in result
    assert steps[-1]["ok"] is False


# ── /config/models 模型列表端点 ──


def _admin_token(db: Session):
    role = Role(
        name=f"cfg_role_{datetime.now(timezone.utc).timestamp()}",
        permissions='["settings:manage"]',
        device_scope="all",
    )
    db.add(role)
    db.flush()
    user = User(
        username=f"cfguser_{datetime.now(timezone.utc).timestamp()}",
        password="irrelevant",
        role="admin_cfg",
        is_active=1,
        role_id=role.id,
    )
    db.add(user)
    db.flush()
    db.commit()
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=["settings:manage"],
        device_scope="all",
    )
    return role, user, token


def test_list_models_returns_sorted_ids():
    """拉取 /models 成功:返回去重排序后的模型 id 列表。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    db = SessionLocal()
    role, user, token = _admin_token(db)
    payload = {
        "data": [{"id": "qwen-plus"}, {"id": "deepseek-chat"}, {"id": "qwen-plus"}]
    }
    try:
        with patch("requests.get", return_value=_FakeResp(payload)):
            resp = client.post(
                "/api/agent/config/models",
                json={"base_url": "http://llm-gw/v1", "api_key": "sk-test"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200
        assert resp.json()["models"] == ["deepseek-chat", "qwen-plus"]
    finally:
        db.query(User).filter(User.id == user.id).delete()
        db.query(Role).filter(Role.id == role.id).delete()
        db.commit()
        db.close()


def test_list_models_requires_api_key():
    """未填 Key 且无已保存密钥 → 400。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    db = SessionLocal()
    role, user, token = _admin_token(db)
    try:
        # 开发库可能已保存真实 LLM Key，闸门必须钉死「无已保存密钥」分支。
        with patch("app.routers.agent.get_agent_config") as get_cfg:
            get_cfg.return_value = SimpleNamespace(api_key="")
            resp = client.post(
                "/api/agent/config/models",
                json={"base_url": "http://llm-gw/v1", "api_key": ""},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 400
    finally:
        db.query(User).filter(User.id == user.id).delete()
        db.query(Role).filter(Role.id == role.id).delete()
        db.commit()
        db.close()


# ── 归因分析性能:SSH 连接复用与诊断项锁定 ──


def test_ssh_session_reuses_one_connection(device):
    """一轮诊断内的多条命令只握手一次。

    目标机每次新建 SSH 连接在认证阶段要 5~10s，而单条诊断命令只要 0.1s 左右。
    逐条重连会把一次 AI 归因拖成分钟级，所以 _loop 必须复用同一条 transport。
    """
    from app.services import agent_commands
    from app.services.crypto import encrypt

    device.remote_username = "root"
    device.remote_password_enc = encrypt("secret")

    opened: list[str] = []
    executed: list[str] = []
    closed: list[int] = []

    class _FakeTransport:
        @staticmethod
        def is_active() -> bool:
            return True

    class _FakeClient:
        def __init__(self) -> None:
            closed.append(0)

        def get_transport(self):
            return _FakeTransport()

        def close(self) -> None:
            closed[-1] += 1

    def fake_connect(dev, username, password, **kwargs):  # noqa: ARG001
        opened.append(dev.ip_address)
        return _FakeClient(), "host-key"

    def fake_exec(client, command, **kwargs):  # noqa: ARG001
        executed.append(command)
        return 0, "ok", ""

    session = agent_service._SshSession()
    try:
        with (
            patch("app.services.ssh.connect_device", side_effect=fake_connect),
            patch("app.services.ssh.exec_ssh_command", side_effect=fake_exec),
        ):
            for key in ("top_processes_cpu", "cpu", "load_uptime"):
                item = agent_commands.LINUX_DIAGNOSTICS[key]
                agent_service._exec_diagnostic(
                    None, device, item["command"], 15, session=session
                )
    finally:
        session.close()

    assert len(executed) == 3
    # 三条命令只建了一条连接，且收尾时被关闭（不会泄漏 transport）。
    assert len(opened) == 1
    assert closed == [1]


def test_ssh_session_reopens_when_connection_dies(device):
    """缓存在两条命令之间被对端回收时，丢弃并重建一次，而不是直接报错。"""
    from app.services import agent_commands
    from app.services.crypto import encrypt

    device.remote_username = "root"
    device.remote_password_enc = encrypt("secret")

    opened: list[int] = []
    attempts: list[int] = []

    class _FakeTransport:
        @staticmethod
        def is_active() -> bool:
            return True

    class _FakeClient:
        def __init__(self) -> None:
            opened.append(1)

        def get_transport(self):
            return _FakeTransport()

        def close(self) -> None:
            return None

    def fake_connect(dev, username, password, **kwargs):  # noqa: ARG001
        return _FakeClient(), "host-key"

    def fake_exec(client, command, **kwargs):  # noqa: ARG001
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("Socket is closed")
        return 0, "ok", ""

    session = agent_service._SshSession()
    try:
        with (
            patch("app.services.ssh.connect_device", side_effect=fake_connect),
            patch("app.services.ssh.exec_ssh_command", side_effect=fake_exec),
        ):
            item = agent_commands.LINUX_DIAGNOSTICS["cpu"]
            code, out, _err = agent_service._exec_diagnostic(
                None, device, item["command"], 15, session=session
            )
    finally:
        session.close()

    assert (code, out) == (0, "ok")
    assert len(opened) == 2


def test_focus_overrides_keyword_catalog_for_metric_alerts(device):
    """指标告警的归因话术是固定模板，不能让关键词规则去猜诊断目录。

    "定位当前占用最高的进程/服务…判断属于业务增长、异常进程还是资源泄漏"
    里的"服务""异常"会被误命中服务组和日志组，把只有 2 项的诊断预算
    浪费在 journalctl / systemctl 这些又慢又无关的命令上。
    """
    from app.services.remediation import _ANALYSIS_FOCUS, _analysis_question

    class _Event:
        metric = "cpu_pct"
        value = 95.0
        threshold = 90.0

    question = _analysis_question(_Event(), device)

    guessed = agent_service._select_catalog(device, question)
    # 纯关键词猜测会带进日志/服务类诊断项（最慢的 journalctl 超时 20s）。
    assert "app_errors" in guessed and "kernel_errors" in guessed
    assert "failed_services" in guessed

    focused = agent_service._select_catalog(
        device, question, _ANALYSIS_FOCUS["cpu_pct"]
    )
    # 进程/CPU 快照 + 容器进程与资源占用取证
    assert set(focused) == {
        "top_processes_cpu",
        "cpu",
        "docker_processes",
        "docker_stats",
    }
    assert not ({"app_errors", "kernel_errors", "failed_services"} & set(focused))


def test_focus_drops_list_diagnostics_tool(device):
    """目录已由调用方精确指定时不再提供 list_diagnostics，省掉一轮 LLM 往返。"""
    captured: list[list[dict]] = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):  # noqa: A002
        captured.append(json["tools"])
        return _FakeResp(_final_msg("## 结论\n内存正常。"))

    with (
        patch.object(agent_service.requests, "post", side_effect=fake_post),
        patch.object(agent_service, "_exec_diagnostic", return_value=(0, "ok", "")),
    ):
        cfg = agent_service.AgentConfig(
            enabled=True,
            base_url="http://llm-gw/v1",
            api_key="test-key",
            model="test-model",
            max_steps=2,
        )
        agent_service._loop(
            None, device, "内存使用率过高", [], cfg, focus=("top_processes_mem",)
        )

    names = [tool["function"]["name"] for tool in captured[0]]
    assert "list_diagnostics" not in names
    assert "run_diagnostic" in names


# ══════════════════════════════════════════════════════════════════════
# 交给 LLM 的输出选取
#
# 旧写法 `raw_text = out or err` 在 stdout 为空时回退到 stderr，而 stderr 是
# PowerShell 的告警/进度流(CLIXML)。对诊断而言「命令成功且无输出」本身就是
# 结论——例如「近 24h 无服务失败事件」——回退之后模型读到的是几百字符 XML
# 噪音，得先自己看懂那段 XML 才能得出「无异常」。实测(SRM / Windows Server
# 2019)报告里真的出现了「输出仅有 Preparing modules for first use 进度消息」。
# ══════════════════════════════════════════════════════════════════════

CLIXML_NOISE = (
    "#< CLIXML\n"
    '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
    '<Obj S="progress" RefId="0"><AV>Preparing modules for first use.</AV></Obj></Objs>'
)


def test_diagnostic_output_prefers_stdout_on_success():
    assert agent_service._diagnostic_output(0, "Mem: 8000", "") == "Mem: 8000"


def test_diagnostic_output_ignores_stderr_noise_on_success():
    """成功且无输出 = 有效结论，不能用 stderr 噪音顶替。"""
    assert agent_service._diagnostic_output(0, "", CLIXML_NOISE) == ""


def test_diagnostic_output_keeps_stderr_on_failure():
    """失败时 stderr 才是错误信息，必须交给模型，否则它无从判断原因。"""
    assert agent_service._diagnostic_output(1, "", "连接超时") == "连接超时"


def test_diagnostic_output_falls_back_to_stdout_on_failure():
    assert agent_service._diagnostic_output(1, "部分输出", "") == "部分输出"


def test_diagnostic_output_all_empty():
    assert agent_service._diagnostic_output(0, "", "") == ""
    assert agent_service._diagnostic_output(1, "", "") == ""


def test_agent_does_not_send_stderr_noise_to_llm(device):
    """端到端：CLIXML 不得出现在发往 LLM 的 tool 结果里。"""
    llm_script = [
        _tool_call_msg("c1", "run_diagnostic", {"key": "failed_services"}),
        _final_msg("## 结论\n无服务失败事件。"),
    ]
    sent_payloads: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):  # noqa: A002
        sent_payloads.append(json)
        return _FakeResp(llm_script[len(sent_payloads) - 1])

    # 命令成功、stdout 为空、stderr 是 PowerShell 进度流
    def fake_exec(db: Session, dev, command: str, timeout: int, session=None):
        return 0, "", CLIXML_NOISE

    with (
        patch.object(agent_service.requests, "post", side_effect=fake_post),
        patch.object(agent_service, "_exec_diagnostic", side_effect=fake_exec),
    ):
        cfg = agent_service.AgentConfig(
            enabled=True,
            base_url="http://llm-gw/v1",
            api_key="test-key",
            model="test-model",
            max_steps=8,
        )
        result = agent_service.run_diagnosis(
            device, "服务有没有异常", user_id=None, cfg=cfg
        )

    assert result["status"] == "completed"
    # 第二轮请求携带的 tool 结果里不能有进度流噪音
    second_request = json.dumps(sent_payloads[1], ensure_ascii=False)
    assert "CLIXML" not in second_request
    assert "Preparing modules" not in second_request


def test_agent_does_send_stderr_to_llm_on_failure(device):
    """反过来：命令失败时错误信息必须送达，否则模型只能瞎猜。"""
    llm_script = [
        _tool_call_msg("c1", "run_diagnostic", {"key": "failed_services"}),
        _final_msg("## 结论\n取证失败。"),
    ]
    sent_payloads: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):  # noqa: A002
        sent_payloads.append(json)
        return _FakeResp(llm_script[len(sent_payloads) - 1])

    def fake_exec(db: Session, dev, command: str, timeout: int, session=None):
        return 1, "", "WinRM 连接超时(25s)"

    with (
        patch.object(agent_service.requests, "post", side_effect=fake_post),
        patch.object(agent_service, "_exec_diagnostic", side_effect=fake_exec),
    ):
        cfg = agent_service.AgentConfig(
            enabled=True,
            base_url="http://llm-gw/v1",
            api_key="test-key",
            model="test-model",
            max_steps=8,
        )
        agent_service.run_diagnosis(device, "服务有没有异常", user_id=None, cfg=cfg)

    second_request = json.dumps(sent_payloads[1], ensure_ascii=False)
    assert "WinRM 连接超时" in second_request


def test_docker_gated_diagnostics_skip_when_docker_absent():
    """Docker 探测项确认未安装后,依赖容器运行时的诊断项直接短路。

    场景来自 2026-09-15 K3 服务器诊断:docker_runtime 输出「未安装 Docker」,
    LLM 仍按目录继续调 docker_processes/docker_logs/docker_stats/traefik_*,
    每项都跑一遍 `command -v docker` 守卫再输出同样的「未安装 Docker」——
    白耗目标机执行与 LLM 往返。门控后这些项在 _dispatch 层直接拒绝。
    """
    from app.services import agent as agent_service

    steps = [
        {
            "tool": "run_diagnostic",
            "key": "docker_runtime",
            "label": "Docker 容器与 Compose 运行状态",
            "ok": True,
            "preview": "未安装 Docker",
            "truncated": False,
        }
    ]
    executed: set[str] = set()
    catalog = {
        "docker_processes": {
            "label": "容器内进程与 PID 1 命令行",
            "command": "echo x",
            "timeout": 40,
        }
    }

    result = json.loads(
        agent_service._dispatch(
            None,
            None,
            catalog,
            {
                "function": {
                    "name": "run_diagnostic",
                    "arguments": json.dumps({"key": "docker_processes"}),
                }
            },
            steps,
            executed,
            diagnostic_budget=10,
        )
    )
    assert "未安装 Docker" in result["error"]
    # 跳过项不进执行明细:steps 保持只有探测项一条,不再追加「已跳过」行
    assert len(steps) == 1
    # 命令不应被执行:预算已消耗(executed 已 add),但没有真实输出
    assert "output" not in result

    # 反例:Docker 存在时不拦截(该走真实执行的路径)
    steps_docker_ok = [
        {
            "tool": "run_diagnostic",
            "key": "docker_runtime",
            "label": "Docker 容器与 Compose 运行状态",
            "ok": True,
            "preview": "## containers\nabc123|nginx|running",
        }
    ]
    assert agent_service._docker_absent_note(steps) == "未安装 Docker"
    assert agent_service._docker_absent_note(steps_docker_ok) == ""
    # 非探测项的输出包含同样文案不算数(如 LLM 幻觉其它项输出)
    assert (
        agent_service._docker_absent_note(
            [{"tool": "run_diagnostic", "key": "cpu", "preview": "未安装 Docker"}]
        )
        == ""
    )


# ── 单-pass 归因(告警归因专用) ──


def test_single_pass_executes_each_focus_key_once_and_one_llm_call(device):
    """单-pass:取证项各跑一次(并行)、LLM 只调一次出结论，没有选择轮。"""
    from app.services import agent as agent_service

    calls: list[str] = []
    chats: list[list] = []

    def _fake_dispatch(db, dev, catalog, call, steps, *args, **kwargs):
        key = json.loads(call["function"]["arguments"])["key"]
        calls.append(key)
        steps.append(
            {"tool": "run_diagnostic", "key": key, "ok": True, "preview": f"out-{key}"}
        )
        return f"out-{key}"

    def _fake_chat(messages, cfg, tools="NOT-PASSED", **kwargs):
        chats.append(messages)
        chat_calls.append({"tools": tools, **kwargs})
        return {"content": "结论:业务增长，无异常进程。"}

    chat_calls: list[dict] = []

    catalog = {
        "memory": {"label": "内存使用", "command": "free"},
        "cpu": {"label": "CPU 使用", "command": "top"},
    }
    steps: list[dict] = []
    with (
        patch.object(
            agent_service.agent_commands,
            "get_diagnostics_for_device",
            return_value=catalog,
        ),
        patch.object(agent_service, "_dispatch", side_effect=_fake_dispatch),
        patch.object(agent_service, "_chat", side_effect=_fake_chat),
        patch.object(agent_service, "_container_snapshot_evidence", return_value=None),
    ):
        report = agent_service.run_focus_diagnosis(
            SessionLocal(),
            device,
            "内存为什么高?",
            steps,
            SimpleNamespace(),
            ("memory", "cpu"),
        )

    assert report == "结论:业务增长，无异常进程。"
    assert sorted(calls) == ["cpu", "memory"]
    assert len(chats) == 1
    # 结论轮不带任何工具(调用方不传 tools,请求体不发该字段,模型协议上就发不出
    # tool_call),且用归因专用更紧的 LLM 超时;一旦模型违反提示去调工具,
    # content 为空 → 报告变成占位符,事件却标 completed。
    assert chat_calls[0]["tools"] == "NOT-PASSED"
    assert chat_calls[0]["timeout"] == agent_service.ALERT_ANALYSIS_LLM_TIMEOUT
    assert chat_calls[0]["max_retries"] == 1
    # 瘦身提示词:只含 focus 子集 + 单-pass 说明
    system = chats[0][0]["content"]
    assert "[单-pass 归因]" in system
    assert "内存使用" in chats[0][1]["content"]
    assert "out-memory" in chats[0][1]["content"]
    assert {s["key"] for s in steps} == {"cpu", "memory"}


def test_single_pass_reuses_fresh_container_snapshot(device):
    """docker_stats 有快照时不重跑远程命令;无快照回退现跑。"""
    from app.services import agent as agent_service

    calls: list[str] = []

    def _fake_dispatch(db, dev, catalog, call, steps, *args, **kwargs):
        key = json.loads(call["function"]["arguments"])["key"]
        calls.append(key)
        steps.append(
            {"tool": "run_diagnostic", "key": key, "ok": True, "preview": "live"}
        )
        return "live"

    catalog = {
        "memory": {"label": "内存使用"},
        "docker_stats": {"label": "容器资源"},
    }
    steps: list[dict] = []
    with (
        patch.object(
            agent_service.agent_commands,
            "get_diagnostics_for_device",
            return_value=catalog,
        ),
        patch.object(agent_service, "_dispatch", side_effect=_fake_dispatch),
        patch.object(agent_service, "_chat", return_value={"content": "ok"}),
        patch.object(
            agent_service,
            "_container_snapshot_evidence",
            return_value="web cpu=12% mem=39%",
        ),
    ):
        agent_service.run_focus_diagnosis(
            SessionLocal(),
            device,
            "q",
            steps,
            SimpleNamespace(),
            ("memory", "docker_stats"),
        )
    assert calls == ["memory"]
    assert any(s["key"] == "docker_stats" and "快照" in s["label"] for s in steps)

    calls.clear()
    steps.clear()
    with (
        patch.object(
            agent_service.agent_commands,
            "get_diagnostics_for_device",
            return_value=catalog,
        ),
        patch.object(agent_service, "_dispatch", side_effect=_fake_dispatch),
        patch.object(agent_service, "_chat", return_value={"content": "ok"}),
        patch.object(agent_service, "_container_snapshot_evidence", return_value=None),
    ):
        agent_service.run_focus_diagnosis(
            SessionLocal(),
            device,
            "q",
            steps,
            SimpleNamespace(),
            ("memory", "docker_stats"),
        )
    assert sorted(calls) == ["docker_stats", "memory"]


def test_container_snapshot_evidence_freshness(device):
    """快照新鲜 → 拼每容器一行;过期 → None(回退现跑命令)。"""
    from datetime import datetime, timedelta, timezone

    from app.models.device_container import DeviceContainer
    from app.services import agent as agent_service

    db = SessionLocal()
    row = DeviceContainer(
        device_id=device.id,
        container_id="snap-c1",
        name="web",
        state="running",
        cpu_pct=12.5,
        mem_pct=39.6,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    row_id = row.id
    try:
        text = agent_service._container_snapshot_evidence(db, device)
        assert text and "web" in text and "39.6" in text
        row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=9999)
        db.commit()
        db.expire_all()
        assert agent_service._container_snapshot_evidence(db, device) is None
    finally:
        db.query(DeviceContainer).filter(DeviceContainer.id == row_id).delete()
        db.commit()
        db.close()


def test_execute_run_routes_single_pass_only_for_alert_analysis(device):
    """路由契约:single_pass=True 走 run_focus_diagnosis，否则走 _loop
    (手动诊断/自动化 agent 的交互路径不受影响)。"""
    from app.models.agent_run import AgentRun
    from app.services import agent as agent_service

    db = SessionLocal()
    run = AgentRun(
        user_id=None,
        device_id=device.id,
        device_name=device.name,
        device_ip=device.ip_address,
        question="q",
        status="running",
    )
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()

    cfg = SimpleNamespace(max_steps=4)
    seen: list[str] = []
    try:
        with (
            patch.object(
                agent_service,
                "_loop",
                side_effect=lambda *a, **k: seen.append("loop") or "r",
            ),
            patch.object(
                agent_service,
                "run_focus_diagnosis",
                side_effect=lambda *a, **k: seen.append("focus") or "r",
            ),
        ):
            agent_service._execute_run(run_id, device, "q", cfg, ("memory",), True)
            agent_service._execute_run(run_id, device, "q", cfg, ("memory",), False)
        assert seen == ["focus", "loop"]
    finally:
        db = SessionLocal()
        db.query(AgentRun).filter(AgentRun.id == run_id).delete()
        db.commit()
        db.close()


# ── _chat 契约:tools 省略 / 超时 / 瞬时错误重试 / 用量 ──


def _cfg() -> agent_service.AgentConfig:
    return agent_service.AgentConfig(
        enabled=True,
        base_url="http://llm-gw/v1",
        api_key="test-key",
        model="test-model",
        max_steps=8,
    )


def test_chat_omits_tools_field_when_unset():
    """tools 用哨兵省略时请求体不含 tools/tool_choice;显式传列表时才携带。"""
    payloads: list[tuple[dict, object]] = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):  # noqa: A002
        payloads.append((json, timeout))
        return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

    with patch.object(agent_service.requests, "post", side_effect=fake_post):
        agent_service._chat([{"role": "user", "content": "q"}], _cfg())
        agent_service._chat(
            [{"role": "user", "content": "q"}], _cfg(), agent_service._TOOLS
        )
    first, timeout = payloads[0]
    assert "tools" not in first and "tool_choice" not in first
    # 未显式指定超时 → 报告轮默认 180s
    assert timeout == agent_service.AGENT_LLM_REPORT_TIMEOUT
    assert "tools" in payloads[1][0] and payloads[1][0]["tool_choice"] == "auto"


def test_chat_retries_transient_status_then_succeeds(monkeypatch):
    monkeypatch.setattr(agent_service.time, "sleep", lambda *_: None)
    responses = [
        _FakeResp(None, status_code=429, text="rate limited"),
        _FakeResp(None, status_code=503, text="upstream"),
        _FakeResp({"choices": [{"message": {"content": "ok"}}]}),
    ]
    calls: list[str] = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return responses[len(calls) - 1]

    with patch.object(agent_service.requests, "post", side_effect=fake_post):
        msg = agent_service._chat([{"role": "user", "content": "q"}], _cfg())
    assert msg["content"] == "ok"
    assert len(calls) == 3  # 两次失败 + 第三次成功


def test_chat_retries_connection_errors(monkeypatch):
    import requests as requests_lib

    monkeypatch.setattr(agent_service.time, "sleep", lambda *_: None)
    calls: list[int] = []

    def fake_post(url, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise requests_lib.ConnectionError("conn reset")
        return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

    with patch.object(agent_service.requests, "post", side_effect=fake_post):
        msg = agent_service._chat([{"role": "user", "content": "q"}], _cfg())
    assert msg["content"] == "ok"
    assert len(calls) == 2


def test_chat_gives_up_after_retries_exhausted(monkeypatch):
    monkeypatch.setattr(agent_service.time, "sleep", lambda *_: None)
    with patch.object(
        agent_service.requests,
        "post",
        return_value=_FakeResp(None, status_code=502, text="bad gw"),
    ):
        with pytest.raises(agent_service.AgentError, match="502"):
            agent_service._chat([{"role": "user", "content": "q"}], _cfg())


def test_chat_does_not_retry_non_retryable_4xx():
    """鉴权/参数类错误重试无意义:一次调用即抛。"""
    calls: list[int] = []

    def fake_post(url, **kwargs):
        calls.append(1)
        return _FakeResp(None, status_code=401, text="bad key")

    with patch.object(agent_service.requests, "post", side_effect=fake_post):
        with pytest.raises(agent_service.AgentError, match="401"):
            agent_service._chat([{"role": "user", "content": "q"}], _cfg())
    assert len(calls) == 1


def test_chat_collects_usage_into_sink():
    payload = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 34},
    }
    sink: list[dict] = []
    with patch.object(agent_service.requests, "post", return_value=_FakeResp(payload)):
        agent_service._chat([{"role": "user", "content": "q"}], _cfg(), usage_sink=sink)
    assert sink == [{"prompt_tokens": 12, "completion_tokens": 34}]


def test_append_usage_step():
    steps: list[dict] = []
    agent_service._append_usage_step(
        steps,
        [{"prompt_tokens": 10, "completion_tokens": 5}, {"prompt_tokens": 7}],
        SimpleNamespace(model="test-model"),
    )
    assert steps[0]["tool"] == "llm_usage"
    assert steps[0]["ok"] is True
    assert "prompt 17 tok" in steps[0]["preview"]
    assert "completion 5 tok" in steps[0]["preview"]
    # 无调用不记,不给步骤列表添噪音
    steps2: list[dict] = []
    agent_service._append_usage_step(steps2, [], SimpleNamespace(model="m"))
    assert steps2 == []


# ── 一轮多个 tool_call 并行执行 ──


def test_parallel_tool_calls_execute_concurrently_and_keep_order(device):
    """同一轮多个 run_diagnostic 并行执行(串行会卡在 Barrier 上),
    tool 消息按原 tool_calls 顺序回填,重复 key 只执行第一次。"""
    import threading

    barrier = threading.Barrier(3)

    def fake_exec(db, dev, command, timeout, session=None):
        # 三个并发执行都能到齐;串行执行会超时抛 BrokenBarrier → ok=False
        barrier.wait(timeout=5)
        return 0, "ok", ""

    llm_script = [
        _tool_call_msg(
            "c1",
            "run_diagnostic",
            {"key": "memory"},
        ),
    ]
    # 手工构造一轮多 tool_call 的消息
    multi = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "m1",
                            "type": "function",
                            "function": {
                                "name": "run_diagnostic",
                                "arguments": json.dumps({"key": "memory"}),
                            },
                        },
                        {
                            "id": "m2",
                            "type": "function",
                            "function": {
                                "name": "run_diagnostic",
                                "arguments": json.dumps({"key": "load_uptime"}),
                            },
                        },
                        {
                            "id": "m3",
                            "type": "function",
                            "function": {
                                "name": "run_diagnostic",
                                "arguments": json.dumps({"key": "cpu"}),
                            },
                        },
                        {
                            "id": "m4",
                            "type": "function",
                            "function": {
                                "name": "run_diagnostic",
                                "arguments": json.dumps({"key": "memory"}),
                            },
                        },
                    ],
                }
            }
        ]
    }
    llm_script = [multi, _final_msg("## 结论\n并发取证正常。")]
    sent: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):  # noqa: A002
        sent.append(json)
        return _FakeResp(llm_script[len(sent) - 1])

    with (
        patch.object(agent_service.requests, "post", side_effect=fake_post),
        patch.object(agent_service, "_exec_diagnostic", side_effect=fake_exec),
    ):
        result = agent_service.run_diagnosis(
            device, "综合看看", user_id=None, cfg=_cfg()
        )

    assert result["status"] == "completed"
    # 三个不同 key 全部成功(并发到位);重复的 memory 拒绝
    keys = [s["key"] for s in result["steps"] if s.get("tool") == "run_diagnostic"]
    assert sorted(set(keys)) == ["cpu", "load_uptime", "memory"]
    dup = [
        s for s in result["steps"] if s.get("tool") == "run_diagnostic" and not s["ok"]
    ]
    assert len(dup) == 1 and "已执行" in dup[0]["error"]
    # tool 消息顺序与 tool_calls 一致
    tool_msgs = [m for m in sent[-1]["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["m1", "m2", "m3", "m4"]


# ── 虚拟机 get_metrics:PVE 快照历史 ──


def test_metrics_summary_for_vm_target_reads_pve_history(device):
    """AgentTarget(id=None) 经运维绑定反查宿主,读 PVE 快照环历史出趋势。"""
    from app.models.pve_connection import PveConnection
    from app.models.pve_guest_binding import PveGuestBinding

    db = SessionLocal()
    conn = PveConnection(
        name=f"metric-hist-{id(device)}",
        host="10.7.0.1",
        token_id="root@pam!t",
        token_secret_enc="encrypted",
    )
    db.add(conn)
    db.flush()
    binding = PveGuestBinding(
        connection_id=conn.id,
        guest_type="qemu",
        vmid=101,
        ip_address="10.7.0.21",
        os_system="linux",
        username="root",
        enabled=1,
    )
    db.add(binding)
    db.commit()
    conn_id, binding_id = conn.id, binding.id
    db.close()

    target = agent_service.AgentTarget(
        id=None, name="web-01", ip_address="10.7.0.21", os_system="linux"
    )
    now = datetime.now(timezone.utc).timestamp()
    samples = [
        {"ts": now - 120, "cpu": 0.10, "mem": 1, "maxmem": 4, "status": "running"},
        {"ts": now - 60, "cpu": 0.30, "mem": 2, "maxmem": 4, "status": "running"},
        {"ts": now - 5, "cpu": 0.50, "mem": 3, "maxmem": 4, "status": "running"},
    ]
    db = SessionLocal()
    try:
        with patch.object(
            agent_service, "get_guest_metric_history", return_value=samples
        ):
            summary = json.loads(agent_service._metrics_summary(db, target, 1))
        assert summary["samples"] == 3
        assert summary["cpu_pct"]["latest"] == 50.0
        assert summary["mem_pct"]["latest"] == 75.0
        assert summary["data_status"] == "fresh"
        # 绑定不存在 → 明确的 note,而不是异常
        orphan = agent_service.AgentTarget(
            id=None, name="x", ip_address="10.9.9.9", os_system="linux"
        )
        note = json.loads(agent_service._metrics_summary(db, orphan, 1))
        assert "note" in note
    finally:
        db.query(PveGuestBinding).filter(PveGuestBinding.id == binding_id).delete()
        db.query(PveConnection).filter(PveConnection.id == conn_id).delete()
        db.commit()
        db.close()


# ── 并发上限与完成事件 ──


def test_execute_run_queues_behind_semaphore(device):
    """同时运行数达到 AGENT_MAX_CONCURRENT_RUNS 后,新运行排队不执行。"""
    import threading

    db = SessionLocal()
    run = AgentRun(
        user_id=None,
        device_id=device.id,
        device_name=device.name,
        device_ip=device.ip_address,
        question="q",
        status="running",
    )
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()

    slots = agent_service.AGENT_MAX_CONCURRENT_RUNS
    called: list[bool] = []
    try:
        for _ in range(slots):
            agent_service._run_slots.acquire()
        with patch.object(
            agent_service,
            "_loop",
            side_effect=lambda *a, **k: called.append(True) or "r",
        ):
            worker = threading.Thread(
                target=agent_service._execute_run,
                args=(run_id, device, "q", _cfg()),
                daemon=True,
            )
            worker.start()
            worker.join(timeout=1.0)
            # 没拿到槽位:还没开始执行,run 仍停在 running
            assert called == []
            row = SessionLocal()
            try:
                assert (
                    row.query(AgentRun).filter(AgentRun.id == run_id).first().status
                    == "running"
                )
            finally:
                row.close()
            for _ in range(slots):
                agent_service._run_slots.release()
            worker.join(timeout=10)
        assert called == [True]
        row = SessionLocal()
        try:
            assert (
                row.query(AgentRun).filter(AgentRun.id == run_id).first().status
                == "completed"
            )
        finally:
            row.close()
    finally:
        db = SessionLocal()
        db.query(AgentRun).filter(AgentRun.id == run_id).delete()
        db.commit()
        db.close()


def test_execute_run_signals_completion_event(device):
    """诊断落终态后 set 完成事件并从注册表摘除,等待方零延迟唤醒。"""
    db = SessionLocal()
    run = AgentRun(
        user_id=None,
        device_id=device.id,
        device_name=device.name,
        device_ip=device.ip_address,
        question="q",
        status="running",
    )
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()

    event = agent_service._register_run_event(run_id)
    assert agent_service.wait_run_completion(run_id, 0) is False
    try:
        with patch.object(agent_service, "_loop", return_value="r"):
            agent_service._execute_run(run_id, device, "q", _cfg())
        assert event.wait(0) is True
        with agent_service._run_events_lock:
            assert run_id not in agent_service._run_events
    finally:
        db = SessionLocal()
        db.query(AgentRun).filter(AgentRun.id == run_id).delete()
        db.commit()
        db.close()
