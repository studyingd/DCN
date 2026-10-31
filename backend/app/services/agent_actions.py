"""Agent 变更动作注册表(L1/L2 分级)——与读原语同一条拼装纪律。

分级口径(2026-10-25 设计定稿):
  * L1(免审自动执行,标准模式):方向为「恢复」且幂等的动作——start 已停止
    的容器/服务。与告警自愈(ALERT_AUTO_REMEDIATION)执行集同源,风险面
    没有扩大,方向单一(只拉起,不停止),可逆(再 stop 即回原状)。
  * L2(需人工审批):restart / stop 运行中的服务与容器——有真实业务中断
    代价,「谁能拍板」由人决定;也是提示注入最想伪造的动作类别。
  * L3(永不开放,不进本注册表):文件删除/清理、配置写入、包安装、账户/
    防火墙、任意写命令、PVE 宿主操作(Agent 会话在目标机内部,管不了宿主,
    虚机电源继续归自愈)。

技术红线与 agent_primitives 一致:命令由服务端模板拼装,参数只过白名单
正则(校验器直接复用 agent_primitives,单一真源),任何元字符拼装前拒绝。
写动作**不做** assert_readonly——写是目的;安全边界=注册表封闭 + 参数白名单
+ L2 人工审批批注定稿的不可变命令字符串。
"""

from __future__ import annotations

import shlex

from app.services.agent_primitives import (
    _CONTAINER_RE,
    _UNIT_RE,
    _WIN_SERVICE_RE,
    PrimitiveError,
    _ps_quote,  # noqa: F401 — 复用引号转义
    _require_str,
)

# ── Linux(sh)动作构建器 ──


def _sh_container_start(p: dict) -> str:
    name = _require_str(p, "name", _CONTAINER_RE, "docker 容器名")
    return f"docker start {name}"


def _sh_container_stop(p: dict) -> str:
    name = _require_str(p, "name", _CONTAINER_RE, "docker 容器名")
    return f"docker stop {name}"


def _sh_container_restart(p: dict) -> str:
    name = _require_str(p, "name", _CONTAINER_RE, "docker 容器名")
    return f"docker restart {name}"


def _sh_service_start(p: dict) -> str:
    unit = _require_str(p, "unit", _UNIT_RE, "systemd unit 名")
    return f"systemctl start {shlex.quote(unit)}"


def _sh_service_stop(p: dict) -> str:
    unit = _require_str(p, "unit", _UNIT_RE, "systemd unit 名")
    return f"systemctl stop {shlex.quote(unit)}"


def _sh_service_restart(p: dict) -> str:
    unit = _require_str(p, "unit", _UNIT_RE, "systemd unit 名")
    return f"systemctl restart {shlex.quote(unit)}"


# ── Windows(PowerShell)动作构建器 ──
# 注意:Restart/Stop-Service 一律不带 -Force——有依赖服务时宁可用失败输出
# 当证据,也不静默连带重启整条依赖链。


def _ps_container_start(p: dict) -> str:
    name = _require_str(p, "name", _CONTAINER_RE, "docker 容器名")
    return f"docker start {name}"


def _ps_container_stop(p: dict) -> str:
    name = _require_str(p, "name", _CONTAINER_RE, "docker 容器名")
    return f"docker stop {name}"


def _ps_container_restart(p: dict) -> str:
    name = _require_str(p, "name", _CONTAINER_RE, "docker 容器名")
    return f"docker restart {name}"


def _ps_service_start(p: dict) -> str:
    name = _require_str(p, "name", _WIN_SERVICE_RE, "Windows 服务名")
    return f"Start-Service -Name {_ps_quote(name)}"


def _ps_service_stop(p: dict) -> str:
    name = _require_str(p, "name", _WIN_SERVICE_RE, "Windows 服务名")
    return f"Stop-Service -Name {_ps_quote(name)}"


def _ps_service_restart(p: dict) -> str:
    name = _require_str(p, "name", _WIN_SERVICE_RE, "Windows 服务名")
    return f"Restart-Service -Name {_ps_quote(name)}"


# ── 注册表:risk 决定执行策略(L1 自动 / L2 审批;strict 模式下 L1 也走审批) ──

_SH_ACTIONS: dict[str, dict] = {
    "container_start": {
        "label": "启动已停止的容器",
        "risk": "l1",
        "timeout": 30,
        "param_names": {"name"},
        "build": _sh_container_start,
    },
    "service_start": {
        "label": "启动 systemd 服务",
        "risk": "l1",
        "timeout": 30,
        "param_names": {"unit"},
        "build": _sh_service_start,
    },
    "container_stop": {
        "label": "停止容器",
        "risk": "l2",
        "timeout": 30,
        "param_names": {"name"},
        "build": _sh_container_stop,
    },
    "container_restart": {
        "label": "重启容器",
        "risk": "l2",
        "timeout": 30,
        "param_names": {"name"},
        "build": _sh_container_restart,
    },
    "service_stop": {
        "label": "停止 systemd 服务",
        "risk": "l2",
        "timeout": 30,
        "param_names": {"unit"},
        "build": _sh_service_stop,
    },
    "service_restart": {
        "label": "重启 systemd 服务",
        "risk": "l2",
        "timeout": 30,
        "param_names": {"unit"},
        "build": _sh_service_restart,
    },
}

_PS_ACTIONS: dict[str, dict] = {
    "container_start": {
        "label": "启动已停止的容器",
        "risk": "l1",
        "timeout": 30,
        "param_names": {"name"},
        "build": _ps_container_start,
    },
    "win_service_start": {
        "label": "启动 Windows 服务",
        "risk": "l1",
        "timeout": 30,
        "param_names": {"name"},
        "build": _ps_service_start,
    },
    "container_stop": {
        "label": "停止容器",
        "risk": "l2",
        "timeout": 30,
        "param_names": {"name"},
        "build": _ps_container_stop,
    },
    "container_restart": {
        "label": "重启容器",
        "risk": "l2",
        "timeout": 30,
        "param_names": {"name"},
        "build": _ps_container_restart,
    },
    "win_service_stop": {
        "label": "停止 Windows 服务",
        "risk": "l2",
        "timeout": 30,
        "param_names": {"name"},
        "build": _ps_service_stop,
    },
    "win_service_restart": {
        "label": "重启 Windows 服务",
        "risk": "l2",
        "timeout": 30,
        "param_names": {"name"},
        "build": _ps_service_restart,
    },
}


def actions_catalog_for(device) -> dict[str, dict]:
    """动作目录形态(并入 _loop 的工具上下文;risk 供提示词与审批卡)。"""
    table = _PS_ACTIONS if device.is_windows else _SH_ACTIONS
    out = {
        key: {
            "label": spec["label"],
            "risk": spec["risk"],
            "timeout": spec["timeout"],
            "param_names": sorted(spec["param_names"]),
        }
        for key, spec in table.items()
    }
    # 一次性只读命令(第 3 期):白名单外的一次性取证,永远需人工审批。
    # 参数即命令原文——与结构化动作不同,这里 LLM 确实"写命令",边界从
    # 「参数白名单」换成「静态只读屏 + 审批人看原文」双闸。
    out["custom_read"] = {
        "label": "执行一次性只读命令",
        "risk": "l2",
        "timeout": 30,
        "param_names": ["command"],
    }
    return out


def build_custom_read(command: str, device) -> dict:
    """白名单外的一次性只读命令:静态只读屏 + 人工审批双闸。

    静态屏复用 agent_commands.assert_readonly 的模式集(写原语/重定向/
    命令替换)——命中的连审批都不进,省掉一条注定该拒的审批;未命中的
    由审批人看命令原文定夺(批的 = 执行的 = 审计的同一字符串)。
    """
    from app.services.agent_commands import assert_readonly

    cmd = (command or "").strip()
    if not cmd:
        raise PrimitiveError("缺少必填参数 command(要执行的只读命令原文)")
    if len(cmd) > 500:
        raise PrimitiveError("命令过长(≤500 字符)")
    if "\n" in cmd or "\r" in cmd:
        raise PrimitiveError("只接受单行命令")
    try:
        assert_readonly(cmd, "ps" if device.is_windows else "sh")
    except ValueError as exc:
        raise PrimitiveError(f"命令未通过静态只读检查:{exc}") from None
    return {
        "label": "执行一次性只读命令",
        "risk": "l2",
        "command": cmd,
        "timeout": 30,
    }


def build_action(key: str, params, device) -> dict:
    """校验参数并拼装动作命令;不合法即抛 PrimitiveError(不拼出任何片段)。"""
    if key == "custom_read":
        raw = params.get("command") if isinstance(params, dict) else None
        return build_custom_read(str(raw or ""), device)
    table = _PS_ACTIONS if device.is_windows else _SH_ACTIONS
    spec = table.get(key)
    if spec is None:
        raise PrimitiveError(f"动作 {key!r} 在目标平台不可用")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise PrimitiveError("params 必须是对象(参数名到值的映射)")
    unknown = set(params) - spec["param_names"]
    if unknown:
        raise PrimitiveError(
            f"未知参数 {sorted(unknown)};允许的参数: {sorted(spec['param_names'])}"
        )
    command = spec["build"](params)
    return {
        "label": spec["label"],
        "risk": spec["risk"],
        "command": command,
        "timeout": spec["timeout"],
    }
