"""Agent 诊断循环 —— OpenAI 兼容 tool-calling。

LLM 能"做"的只有三件事(全部为只读):
  1. list_diagnostics — 列出该设备可用的只读诊断项
  2. run_diagnostic   — 按 key 执行 agent_commands 注册表里的固定命令
  3. get_metrics      — 读本平台已采集的指标趋势(查 DB,不触设备)

边界保证:
  * LLM 永远不接触 shell 自由输入——run_diagnostic 的 key 强制白名单校验,
    幻觉 key 直接拒绝;
  * 每条注册命令在 agent_commands 导入期已过只读校验;
  * 每次诊断的步骤与报告落 agent_runs 审计;
  * 单条输出按 AGENT_MAX_OUTPUT_CHARS 截断,防上下文与内存爆炸。

执行模型:后台线程异步执行,步骤增量落库——前端轮询 /runs/{id}
即可实时看到诊断过程。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from sqlalchemy.orm import Session

from app.config import (
    AGENT_ACTION_APPROVAL_LIMIT,  # noqa: F401 — patch seam(agent.AGENT_ACTION_*)
    AGENT_ACTION_APPROVAL_TIMEOUT,  # noqa: F401 — patch seam
    AGENT_ACTION_LIMIT,  # noqa: F401 — patch seam
    AGENT_CMD_TIMEOUT,
    AGENT_LLM_MAX_RETRIES,
    AGENT_LLM_REPORT_TIMEOUT,
    AGENT_MAX_CONCURRENT_RUNS,
    AGENT_MAX_OUTPUT_CHARS,
    ALERT_ANALYSIS_LLM_TIMEOUT,
)
from app.database import SessionLocal
from app.models.agent_run import AgentRun
from app.models.device import Device
from app.services import (
    agent_actions,
    agent_commands,  # noqa: F401  测试经 agent_service.agent_commands patch
    agent_primitives,
)
from app.services.agent_approval import (  # noqa: F401 — 门面 re-export,patch seam 保持 agent 命名空间
    _action_denied,
    _approval_events,
    _approval_events_lock,
    _audit_container_action,
    _dispatch_run_action,
    _finalize_dangling_approvals,
    _run_action_exec,
    decide_approval,
)
from app.services.agent_catalog import (  # noqa: F401
    _DIAGNOSTIC_HINTS,
    _DOCKER_GATED_KEYS,
    _SUPPLEMENT_BUDGET,
    _docker_absent_note,
    _full_catalog,
    _select_catalog,
)
from app.services.agent_exec import (  # noqa: F401
    _diagnostic_output,
    _exec_diagnostic,
    _resolve_creds,
    _SshSession,
)
from app.services.agent_metrics import _metrics_summary  # noqa: F401
from app.services.agent_prompt import (  # noqa: F401
    _DOMAIN_GUIDANCE,
    _SYSTEM_PROMPT,
    _build_system_prompt,
)
from app.services.agent_types import (  # noqa: F401
    AgentConfig,
    AgentError,
    AgentTarget,
    get_agent_config,
    session_retry,
)
from app.services.settings import get_setting
from app.validators import validate_outbound_url

logger = logging.getLogger(__name__)


INTERRUPTED_RUN_ERROR = "服务重启，Agent 诊断线程已中断"


def recover_interrupted_runs() -> int:
    """服务重启后回收失去后台线程的 AgentRun 记录。"""
    db = SessionLocal()
    try:
        rows = db.query(AgentRun).filter(AgentRun.status == "running").all()
        for row in rows:
            row.status = "failed"
            row.error = INTERRUPTED_RUN_ERROR
        if rows:
            db.commit()
        try:
            # 悬挂审批一并收敛:pending→expired、approved 未执行→execution_failed,
            # 重启恢复绝不自动补执行写操作。
            _finalize_dangling_approvals(db)
        except Exception:
            logger.debug(
                "finalize dangling approvals on recovery failed", exc_info=True
            )
        return len(rows)
    finally:
        db.close()


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_diagnostics",
            "description": "列出服务端已按当前问题筛选的只读诊断项(key 与说明)。目录已在用户上下文中提供,通常无需重复调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_diagnostic",
            "description": "按 key 执行一个只读诊断项,返回命令输出。只能使用上下文中列出的 key(已选诊断项或补充只读项);目录说明标注「参数:」的项是参数化取证项,需在 params 对象里按名传参。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "诊断项 key"},
                    "params": {
                        "type": "object",
                        "description": (
                            "参数化取证项的可选参数(如 name/unit/path/tail/lines/depth),"
                            "约束见目录说明;缺参、越界或含特殊字符会被服务端拒绝"
                        ),
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_metrics",
            "description": "获取平台已采集的服务器性能指标趋势(CPU/内存/磁盘/网络/磁盘 IO 的均值与峰值)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "回看小时数,可选 1/6/24/168",
                        "default": 1,
                    },
                },
                "required": [],
            },
        },
    },
]

# ── 分级动作(2026-10-25):L1「恢复方向」写免审自动,L2「中断代价」写人工审批 ──

_ACTION_MODES = ("readonly", "standard", "strict")

_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "run_action",
        "description": (
            "请求执行一个变更动作。L1(启动已停止的容器/服务)在当前模式下自动执行;"
            "L2(重启/停止运行中的服务或容器)需人工审批,批准后才执行。"
            "一次只能提议一个动作并等待其结果。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "动作 key"},
                "params": {
                    "type": "object",
                    "description": "动作参数(name=容器名 / unit=服务名),约束见动作目录",
                },
                "reason": {
                    "type": "string",
                    "description": "处置理由:证据→为什么是这个动作→预期效果(审批卡会展示给人工)",
                },
            },
            "required": ["key", "reason"],
        },
    },
}

# 仅当动作工具可用时追加到系统提示词(与 _DOMAIN_GUIDANCE 的动态拼接同思路)
_ACTION_RULES = (
    "\n\n变更动作纪律:"
    "\n16. 只有当只读证据已定位根因、且动作能直接消除/缓解问题时才提议 run_action;"
    "每次只提议一个动作并等待其结果,不要一轮里提议多个。"
    "\n17. reason 必须写清「证据 → 为什么是这个动作 → 预期效果」,它会展示给审批人。"
    "\n18. 动作执行后必须用只读诊断项复核效果(如服务状态/容器状态),再下结论。"
    "\n19. 被拒绝或超时的动作不得换方式重试;转只读结论,并在建议中注明需人工执行。"
    "\n20. custom_read 用于「已选目录与补充只读项都未覆盖」的一次性只读取证"
    "(如统计指定日志、读取指定配置):params.command 传完整命令原文,必须只读"
    "——写原语/重定向/命令替换会被静态拒绝;reason 说明这条命令回答什么问题;"
    "优先用目录内已有诊断项,custom_read 是最后手段。"
)

# 审批事件:approval_id -> Event。审批人在 API 层决策后 set,唤醒挂起等待的
# 诊断线程(与 _run_events 同模式);等待线程自己负责超时裁决与状态流转。


_UNSET = object()
# 瞬时错误状态码:重试有意义;其余(4xx 参数/鉴权错误)重试也是同样的错。
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# 重试退避(秒),按已重试次数取;上限 10s 避免拖爆调用方的等待预算。
_RETRY_BACKOFF = (1.5, 4.0, 10.0)


def _chat(
    messages: list[dict],
    cfg: AgentConfig,
    tools: list[dict] | object = _UNSET,
    *,
    timeout: int | None = None,
    max_retries: int | None = None,
    usage_sink: list | None = None,
) -> dict:
    """一次 chat/completions 调用,返回 message 对象。

    * tools 省略时不发 tools 字段(单-pass 结论轮);交互循环传工具子集。
    * timeout 默认用 ``AGENT_LLM_REPORT_TIMEOUT``(180s):报告轮要产出 3000+
      token,推理型模型实测 55~60s,用常规 60s 贴线跑偶发超时会把
      "22 步证据全部采完"的诊断整个作废。单-pass 归因结论只有 ≤200 字,
      由调用方传入更紧的 ``ALERT_ANALYSIS_LLM_TIMEOUT``。
    * 429/5xx/连接抖动按退避重试(AGENT_LLM_MAX_RETRIES,默认 2 次):
      瞬时错误不再把已采完证据的整轮诊断作废。鉴权/参数类 4xx 不重试。
    """
    url = f"{cfg.base_url}/chat/completions"
    request_timeout = AGENT_LLM_REPORT_TIMEOUT if timeout is None else timeout
    retries = AGENT_LLM_MAX_RETRIES if max_retries is None else max_retries
    payload = {"model": cfg.model, "messages": messages, "temperature": 0.2}
    if tools is not _UNSET:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {"Authorization": f"Bearer {cfg.api_key}"}

    resp = None
    for attempt in range(retries + 1):
        try:
            validate_outbound_url(cfg.base_url)
            resp = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=request_timeout,
                allow_redirects=False,
            )
        except ValueError as exc:
            # 出站地址不合规是配置错误,重试没有意义
            raise AgentError(str(exc)) from exc
        except requests.RequestException as exc:
            if attempt < retries:
                _log_retry(exc, attempt, retries)
                continue
            raise AgentError(f"LLM 接口连接失败: {exc}") from exc
        if resp.status_code in _RETRYABLE_STATUS and attempt < retries:
            _log_retry(f"HTTP {resp.status_code}", attempt, retries)
            continue
        break

    if resp.status_code != 200:
        raise AgentError(f"LLM 接口返回 {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise AgentError(f"LLM 响应格式异常: {exc}") from exc
    if usage_sink is not None and isinstance(data.get("usage"), dict):
        usage_sink.append(data["usage"])
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError, ValueError) as exc:
        raise AgentError(f"LLM 响应格式异常: {exc}") from exc


def _log_retry(reason, attempt: int, retries: int) -> None:
    delay = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
    logger.warning(
        "LLM 调用瞬时失败(%s),%.1fs 后重试(第 %d/%d 次)",
        reason,
        delay,
        attempt + 1,
        retries,
    )
    time.sleep(delay)


def _dispatch(
    db: Session,
    device: Device,
    catalog: dict,
    call: dict,
    steps: list[dict],
    executed_keys: set[str] | None = None,
    diagnostic_budget: int | None = None,
    metrics_allowed: bool = True,
    session: _SshSession | None = None,
    mode: str = "readonly",
    l1_auto: bool = False,
    run_id: int | None = None,
    on_step=None,
) -> str:
    """执行一个 tool_call 并返回 tool 消息内容。key 白名单校验在这里。"""
    name = call.get("function", {}).get("name", "")
    raw_args = call.get("function", {}).get("arguments") or "{}"
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        args = {}

    if name == "list_diagnostics":
        out = [
            {
                "key": k,
                "label": v["label"],
                "hint": _DIAGNOSTIC_HINTS.get(k, "只读诊断快照,需结合问题语境解释。"),
            }
            for k, v in catalog.items()
        ]
        return json.dumps(out, ensure_ascii=False)

    if name == "get_metrics":
        if not metrics_allowed:
            steps.append(
                {
                    "tool": "get_metrics",
                    "ok": False,
                    "error": "当前问题不是性能指标查询,已拒绝无关指标读取",
                }
            )
            return json.dumps(
                {"error": "当前问题不是性能指标查询,请勿读取 CPU/内存/磁盘等无关指标"},
                ensure_ascii=False,
            )
        try:
            hours = int(args.get("hours") or 1)
        except (TypeError, ValueError):
            hours = 1
        steps.append({"tool": "get_metrics", "hours": hours, "ok": True})
        return _metrics_summary(db, device, hours)

    if name == "run_diagnostic":
        key = str(args.get("key") or "")
        item = catalog.get(key)
        if item is None:
            # 补充只读项(2026-10-30):全量注册表允许按需调用。关键词收窄只
            # 决定「优先取哪些」,不是可执行边界——否则免审白名单项因问题措辞
            # 没被选中就不可调用,LLM 只能退到 custom_read 自写命令全走审批
            # (run 664 实测:问「跑的应用」,docker 项没入选,docker ps/logs
            # 全进审批)。命令仍是服务端钉死的只读模板,红线未变。
            item = _full_catalog(device).get(key)
        if item is None:
            # 动作 key 误用引导(run 782:docker_logs 被预算拒后,LLM 试图借
            # run_diagnostic 调 custom_read 绕审批)——正确通道是 run_action。
            hint = ""
            if key in agent_actions.actions_catalog_for(device):
                hint = (
                    f";{key} 是动作目录项,请改用 run_action 工具调用"
                    "(custom_read 类需人工审批)"
                )
            elif key in (
                "run_diagnostic",
                "run_action",
                "get_metrics",
                "list_diagnostics",
            ):
                # 工具名误当 key(run 1056:run_diagnostic 被传给 run_diagnostic)
                hint = ";这是工具名不是诊断项 key,请从上下文列出的诊断项中选择"
            # LLM 幻觉出的 key / 试图执行注册表外的命令——拒绝并记录
            steps.append(
                {
                    "tool": "run_diagnostic",
                    "key": key,
                    "ok": False,
                    "error": "诊断项不存在,已拒绝(只读边界)",
                }
            )
            return json.dumps(
                {
                    "error": (
                        f"诊断项 {key!r} 不存在,请从上下文列出的诊断项 key 中选择{hint}"
                    )
                },
                ensure_ascii=False,
            )
        # 参数化取证项:不同参数(如两个容器的日志)是不同证据,按 key+参数去重
        params = args.get("params") if item.get("parameterized") else None
        dedup_key = key
        if item.get("parameterized"):
            try:
                dedup_key = f"{key}|{json.dumps(params or {}, sort_keys=True, ensure_ascii=False)}"
            except (TypeError, ValueError):
                dedup_key = f"{key}|{params!r}"
        if executed_keys is not None and dedup_key in executed_keys:
            # 附已执行清单:LLM 重复点名多因忘了本轮跑过什么,给它清单比一句
            # 「已执行」更能引导下一轮换项(run 843 里 docker_runtime 被重复
            # 点名拒了一次)。
            done = ", ".join(sorted(k.split("|", 1)[0] for k in executed_keys))
            steps.append(
                {
                    "tool": "run_diagnostic",
                    "key": key,
                    "label": item["label"],
                    "ok": False,
                    "error": "该诊断项已执行,拒绝重复执行",
                }
            )
            return json.dumps(
                {
                    "key": key,
                    "error": f"该诊断项已执行,无需重复执行;本轮已执行项: {done}",
                },
                ensure_ascii=False,
            )
        # 预算分道(2026-10-30):目录内项对 diagnostic_budget;补充项对独立的
        # _SUPPLEMENT_BUDGET(按 key 前缀统计,顺序无关)——否则目录预算用满后
        # 兑底通道被掐死,LLM 只能空手写结论。
        budget_reject: str | None = None
        if diagnostic_budget is not None and executed_keys is not None:
            if key not in catalog:
                used = sum(
                    1 for k in executed_keys if k.split("|", 1)[0] not in catalog
                )
                if used >= _SUPPLEMENT_BUDGET:
                    budget_reject = (
                        f"补充只读项最多执行 {_SUPPLEMENT_BUDGET} 项;"
                        "请基于已有证据给出结论或说明证据不足"
                    )
            elif len(executed_keys) >= diagnostic_budget:
                budget_reject = f"具体问题最多执行 {diagnostic_budget} 项相关诊断"
        if budget_reject:
            steps.append(
                {
                    "tool": "run_diagnostic",
                    "key": key,
                    "label": item["label"],
                    "ok": False,
                    "error": budget_reject,
                }
            )
            return json.dumps({"key": key, "error": budget_reject}, ensure_ascii=False)
        if executed_keys is not None:
            executed_keys.add(dedup_key)
        # Docker 依赖门控:docker_runtime(或任一 docker 前置项)已确认目标
        # 未安装 Docker 时,后续 docker/traefik 系列诊断项必然也拿不到数据
        # (每个命令都自带 `command -v docker` 守卫并输出「未安装 Docker」),
        # 直接短路拒绝,省掉 LLM 每项一次往返与目标机执行开销。
        # 跳过项不写入 steps:执行明细只列真实执行过的项,用户已经从
        # 探测项的「未安装 Docker」知道了后续为什么没有容器数据;
        # LLM 侧仍会在 tool 结果里看到同样的说明。
        if key in _DOCKER_GATED_KEYS and "未安装 Docker" in _docker_absent_note(steps):
            return json.dumps(
                {
                    "key": key,
                    "error": "目标未安装 Docker,该项依赖容器运行时,无需执行;请基于已有证据给出结论。",
                },
                ensure_ascii=False,
            )
        if item.get("parameterized"):
            try:
                built = agent_primitives.build_command(key, params, device)
            except agent_primitives.PrimitiveError as exc:
                steps.append(
                    {
                        "tool": "run_diagnostic",
                        "key": key,
                        "label": item["label"],
                        "params": params,
                        "ok": False,
                        "error": f"参数校验失败: {exc}",
                    }
                )
                return json.dumps(
                    {"key": key, "error": f"参数校验失败: {exc}"}, ensure_ascii=False
                )
            command = built["command"]
            timeout = min(built["timeout"], AGENT_CMD_TIMEOUT)
        else:
            command = item["command"]
            timeout = min(item["timeout"], AGENT_CMD_TIMEOUT)
        _t0 = time.monotonic()
        try:
            exit_code, out, err = _exec_diagnostic(
                db,
                device,
                command,
                timeout,
                session=session,
            )
        except Exception as exc:
            steps.append(
                {
                    "tool": "run_diagnostic",
                    "key": key,
                    "label": item["label"],
                    "ok": False,
                    "error": str(exc)[:200],
                    "duration_ms": int((time.monotonic() - _t0) * 1000),
                }
            )
            return json.dumps({"key": key, "error": str(exc)[:300]}, ensure_ascii=False)

        # 输出统一脱敏:固定项(docker inspect 的 env/labels)与原语一样可能带凭据
        raw_text = agent_primitives.redact_output(
            _diagnostic_output(exit_code, out, err)
        )
        truncated = len(raw_text) > AGENT_MAX_OUTPUT_CHARS
        text = raw_text[:AGENT_MAX_OUTPUT_CHARS]
        step_record = {
            "tool": "run_diagnostic",
            "key": key,
            "label": item["label"],
            # 命令原文随步骤落库(2026-10-30):时间线要展示"执行了什么指令",
            # 固定项/原语都是服务端白名单拼装,展示无风险
            "command": command,
            "ok": exit_code == 0,
            "preview": text[:300],
            "truncated": truncated,
            # 步骤级耗时(run 1056 复盘):定位慢项(installed_updates 类 CIM 查询)
            "duration_ms": int((time.monotonic() - _t0) * 1000),
        }
        if item.get("parameterized"):
            step_record["params"] = params
        steps.append(step_record)
        return json.dumps(
            {
                "key": key,
                "exit_code": exit_code,
                "output": text,
                "truncated": truncated,
            },
            ensure_ascii=False,
        )

    if name == "run_action":
        return _dispatch_run_action(
            db, device, args, steps, mode, l1_auto, run_id, session, on_step=on_step
        )

    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)


# ── 变更动作执行与审批环 ──


def _persist_steps(run_id: int, steps: list[dict]) -> None:
    """增量落库当前步骤(供前端轮询实时进度)。"""
    db = SessionLocal()
    try:
        row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        if row:
            row.steps_json = json.dumps(steps, ensure_ascii=False)
            db.commit()
    except Exception:
        logger.debug("persist steps failed for run %s", run_id, exc_info=True)
        db.rollback()
    finally:
        db.close()


def _append_usage_step(steps: list[dict], usage: list[dict], cfg) -> None:
    """把本轮 LLM 用量记进步骤尾部(agent_runs 审计可见,无需额外表结构)。"""
    if not usage:
        return
    prompt_tokens = sum(int(u.get("prompt_tokens") or 0) for u in usage)
    completion_tokens = sum(int(u.get("completion_tokens") or 0) for u in usage)
    steps.append(
        {
            "tool": "llm_usage",
            "label": "LLM 用量",
            "ok": True,
            "preview": (
                f"{cfg.model} · {len(usage)} 次调用 · "
                f"prompt {prompt_tokens} tok · completion {completion_tokens} tok"
            ),
        }
    )


def _dispatch_calls(
    db: Session,
    device,
    catalog: dict,
    tool_calls: list[dict],
    steps: list[dict],
    executed_keys: set[str] | None,
    diagnostic_budget: int | None,
    metrics_allowed: bool,
    session: "_SshSession | None",
    messages: list[dict],
    on_step,
    mode: str = "readonly",
    l1_auto: bool = False,
    run_id: int | None = None,
) -> None:
    """执行一整轮 tool_call 并按原顺序回填 tool 消息。

    模型经常在一轮里同时点多个诊断项;串行执行时 Wall time = 各命令耗时之和,
    并行(共享同一条 SSH 连接,paramiko 支持并发 channel;WinRM 本就无状态)
    后变成取最大值。同一轮里重复点名的 key 只执行第一个,其余等批次结束后
    走已有的「已执行拒绝」路径,避免并行下 check-then-add 竞态重复执行。
    """
    if len(tool_calls) == 1:
        result = _dispatch(
            db,
            device,
            catalog,
            tool_calls[0],
            steps,
            executed_keys,
            diagnostic_budget,
            metrics_allowed,
            session=session,
            mode=mode,
            l1_auto=l1_auto,
            run_id=run_id,
            on_step=on_step,
        )
        if on_step:
            on_step(steps)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_calls[0].get("id", ""),
                "content": result,
            }
        )
        return

    unique: list[tuple[int, dict]] = []
    dups: list[tuple[int, dict]] = []
    seen: set[str] = set()
    for idx, call in enumerate(tool_calls):
        name = call.get("function", {}).get("name", "")
        raw_args = call.get("function", {}).get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {}
        key = str(args.get("key") or "") if name == "run_diagnostic" else ""
        if key and key in seen:
            dups.append((idx, call))
        else:
            if key:
                seen.add(key)
            unique.append((idx, call))

    results: dict[int, str] = {}

    def _one(item: tuple[int, dict]) -> tuple[int, str]:
        idx, call = item
        content = _dispatch(
            db,
            device,
            catalog,
            call,
            steps,
            executed_keys,
            diagnostic_budget,
            metrics_allowed,
            session=session,
            mode=mode,
            l1_auto=l1_auto,
            run_id=run_id,
            on_step=on_step,
        )
        return idx, content

    with ThreadPoolExecutor(max_workers=min(4, len(unique))) as pool:
        for fut in as_completed([pool.submit(_one, item) for item in unique]):
            idx, content = fut.result()
            results[idx] = content
            if on_step:
                on_step(steps)
    for idx, call in dups:
        results[idx] = _dispatch(
            db,
            device,
            catalog,
            call,
            steps,
            executed_keys,
            diagnostic_budget,
            metrics_allowed,
            session=session,
            mode=mode,
            l1_auto=l1_auto,
            run_id=run_id,
            on_step=on_step,
        )
        if on_step:
            on_step(steps)
    for idx, call in enumerate(tool_calls):
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": results[idx],
            }
        )


def _loop(
    db: Session,
    device: Device | AgentTarget,
    question: str,
    steps: list[dict],
    cfg: AgentConfig,
    on_step=None,
    focus: list[str] | tuple[str, ...] | None = None,
    mode: str = "readonly",
    run_id: int | None = None,
) -> str:
    full_catalog = _full_catalog(device)
    catalog = _select_catalog(device, question, focus, full_catalog)
    os_label = "Windows" if device.is_windows else "Linux"
    # 动作策略:readonly=无 run_action 工具;standard=L1 自动+L2 审批
    # (受 settings.agent_action_l1_enabled 总开关约束);strict=写动作全审批。
    mode = mode if mode in _ACTION_MODES else "readonly"
    l1_auto = mode == "standard" and (
        (get_setting(db, "agent_action_l1_enabled") or "true").lower() == "true"
    )

    user_ctx = (
        f"目标设备:{device.name}(IP: {device.ip_address or '未知'},"
        f"类型: {device.type},系统: {device.os_system or os_label})。\n"
        f"用户问题:{question}\n"
        "请开始只读排查。"
    )
    system_prompt = _build_system_prompt(catalog)
    action_catalog_ctx = ""
    if mode != "readonly":
        system_prompt += _ACTION_RULES
        action_catalog_ctx = "；".join(
            f"{k}（{v['label']},风险 {v['risk'].upper()},"
            f"参数: {','.join(v['param_names'])}）"
            for k, v in agent_actions.actions_catalog_for(device).items()
        )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_ctx},
    ]
    selected_keys = (
        "；".join(
            f"{key}（{_DIAGNOSTIC_HINTS.get(key, '只读诊断快照')}）" for key in catalog
        )
        or "无"
    )
    # 补充只读项:全量注册表减已选目录。让 LLM 在已选目录覆盖不了取证需求时
    # 能直接调免审白名单项(而不是退到 custom_read 自写命令进审批),但明确
    # 约束只在覆盖不了时用,防借机全量巡检。参数化项带参数名提示。
    supplemental_keys = "；".join(
        f"{key}（{item['label']}）"
        for key, item in full_catalog.items()
        if key not in catalog
    )
    supplement_ctx = (
        "\n补充只读项(注册表全量,仅当已选目录覆盖不了取证需求时按 key 调用,"
        f"禁止借机全量巡检): {supplemental_keys}。"
        if supplemental_keys
        else ""
    )
    messages.append(
        {
            "role": "user",
            "content": (
                f"本轮服务端筛选出的相关诊断项为: {selected_keys}。\n"
                f"请优先使用这些诊断项取证。{supplement_ctx}"
            ),
        }
    )
    if action_catalog_ctx:
        messages.append(
            {
                "role": "user",
                "content": (
                    f"本次诊断允许的变更动作(L2 需人工审批,批准后才执行): "
                    f"{action_catalog_ctx}。\n"
                    "动作是最后手段:先完成只读取证,再在必要时提议单个动作。"
                ),
            }
        )
    executed_keys: set[str] = set()
    # 具体问题默认最多 2 项诊断;目录自带 3 项以上的分组(如 Docker 组 4 项),
    # 预算取下限,保证同组取证能跑完,又不放任全量体检。
    diagnostic_budget = None if catalog is full_catalog else max(2, len(catalog))
    metric_keywords = (
        "cpu",
        "内存",
        "memory",
        "磁盘",
        "disk",
        "网络",
        "指标",
        "性能",
        "负载",
    )
    metrics_allowed = catalog is full_catalog or any(
        word in question.lower() for word in metric_keywords
    )
    available_tools = (
        _TOOLS
        if metrics_allowed
        else [tool for tool in _TOOLS if tool["function"]["name"] != "get_metrics"]
    )
    # 目录(含 key/标签/hint)已注入用户上下文,list_diagnostics 只会诱导模型
    # 多花一轮 LLM 往返去问它已经知道的答案,一律不提供。_dispatch 的处理分支
    # 保留,兼容旧客户端/测试直接调用的场景。
    available_tools = [
        tool
        for tool in available_tools
        if tool["function"]["name"] != "list_diagnostics"
    ]
    if mode != "readonly":
        available_tools = available_tools + [_ACTION_TOOL]
    usage: list[dict] = []
    evidence_retry = False

    # Linux 目标整轮复用一条 SSH 连接：握手要 5~10s，而单条诊断命令只要 0.1s。
    # Windows 走 WinRM 无状态请求，没有可复用的会话。
    session = None if device.is_windows else _SshSession()
    try:
        for _ in range(cfg.max_steps):
            msg = _chat(messages, cfg, available_tools, usage_sink=usage)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                if not any(step.get("ok") for step in steps) and not evidence_retry:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": msg.get("content"),
                        }
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": "你还没有取得有效运行时证据。请先调用一个最相关的只读诊断工具，再给出结论。",
                        }
                    )
                    evidence_retry = True
                    continue
                _append_usage_step(steps, usage, cfg)
                return msg.get("content") or "(模型未返回内容)"

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": tool_calls,
                }
            )
            _dispatch_calls(
                db,
                device,
                catalog,
                tool_calls,
                steps,
                executed_keys,
                diagnostic_budget,
                metrics_allowed,
                session,
                messages,
                on_step,
                mode=mode,
                l1_auto=l1_auto,
                run_id=run_id,
            )

        raise AgentError(f"超过最大诊断步数({cfg.max_steps}),已中止")
    finally:
        if session is not None:
            session.close()


def _focus_call(key: str) -> dict:
    """单-pass 归因合成 run_diagnostic tool_call(复用 _dispatch 的白名单校验)。"""
    return {
        "id": f"focus-{key}",
        "function": {"name": "run_diagnostic", "arguments": json.dumps({"key": key})},
    }


_SINGLE_PASS_NOTE = (
    "\n\n[单-pass 归因]以上运行时证据已由服务端并行采集完毕，不要再请求任何工具。"
    "请基于证据直接判断归因(业务增长、异常进程、资源泄漏等)。"
    "输出要求:只输出一段 ≤200 字的结论，按「归因 + 关键证据 + 处置建议」三段式，"
    "不要铺陈、不要逐条复述证据、不要标题。"
)


def _container_snapshot_evidence(db: Session, device, max_age: int = 180) -> str | None:
    """容器采集器(60s 一批)的最新容器资源快照;缺失/过期返回 None，调用方回退现跑命令。"""
    from app.models.device_container import DeviceContainer
    from app.models.pve_guest_binding import PveGuestBinding

    if getattr(device, "id", None):

        def _load_rows():
            return (
                db.query(DeviceContainer)
                .filter(DeviceContainer.device_id == device.id)
                .all()
            )
    else:

        def _load_rows():
            binding = (
                db.query(PveGuestBinding)
                .filter(
                    PveGuestBinding.ip_address == device.ip_address,
                    PveGuestBinding.enabled == 1,
                )
                .first()
            )
            if binding is None:
                return []
            return (
                db.query(DeviceContainer)
                .filter(DeviceContainer.pve_guest_binding_id == binding.id)
                .all()
            )

    # 长会话可能已横跨多轮 LLM/SSH 空闲期,连接被服务端抬掉时自愈重试
    rows = session_retry(db, _load_rows)
    if not rows:
        return None
    stamps = [r.updated_at for r in rows if r.updated_at]
    if not stamps:
        return None
    newest = max(stamps)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - newest).total_seconds() > max_age:
        return None

    def _fmt(value) -> str:
        return "-" if value is None else f"{value}"

    return "\n".join(
        f"{r.name} state={r.state or '-'} cpu={_fmt(r.cpu_pct)}% mem={_fmt(r.mem_pct)}%"
        for r in sorted(rows, key=lambda row: row.name)
    )


def _platform_trend_evidence(db: Session, device) -> str | None:
    """平台已采集的指标趋势(零成本证据):设备=采集样本,虚拟机=PVE 快照历史。

    区分「业务增长」与「突发泄漏」靠趋势,不靠一次快照;这份证据平台自己就有,
    不必让目标机再跑命令。无数据时返回 None(不往上下文里塞噪音)。
    """
    try:
        summary = _metrics_summary(db, device, 1)
    except Exception:
        logger.debug("platform trend evidence failed", exc_info=True)
        return None
    if not summary or '"note"' in summary:
        return None
    return summary


def run_focus_diagnosis(
    db: Session,
    device: Device | AgentTarget,
    question: str,
    steps: list[dict],
    cfg: AgentConfig,
    focus: list[str] | tuple[str, ...],
    on_step=None,
) -> str:
    """告警归因专用的单-pass 诊断;手动诊断/自动化 agent 的交互 _loop 不受影响。

    归因的取证项已由调用方按指标精确锁定(_ANALYSIS_FOCUS)，交互循环里 LLM
    那几轮「选项」是纯浪费:这里跳过全部选择轮，取证并行执行(Linux 复用同一条
    SSH 连接)，然后仅一次 LLM 调用出结论。系统提示只含 focus 子集(天然瘦身);
    docker_stats 优先复用容器采集器快照，缺失时回退现跑命令。
    结论轮不带任何工具(协议上就发不了 tool_call,不会再出现"模型违反提示去
    调工具→content 为空→报告变成占位符"的情况),超时用归因专用的更紧预算。
    """
    full_catalog = _full_catalog(device)
    catalog = {key: full_catalog[key] for key in focus if key in full_catalog}
    if not catalog:
        raise AgentError("归因取证项为空或目标平台不具备其中任何一项")
    snapshot = _container_snapshot_evidence(db, device)
    use_snapshot = snapshot is not None and "docker_stats" in catalog
    keys = [k for k in catalog if not (k == "docker_stats" and use_snapshot)]

    results: list[tuple[str, str]] = []
    session = None if device.is_windows else _SshSession()
    try:

        def _one(key: str):
            local: list[dict] = []
            content = _dispatch(
                db, device, catalog, _focus_call(key), local, session=session
            )
            return key, content, local

        with ThreadPoolExecutor(max_workers=min(4, max(len(keys), 1))) as pool:
            for fut in as_completed([pool.submit(_one, k) for k in keys]):
                key, content, local = fut.result()
                steps.extend(local)
                results.append((key, content))
                if on_step:
                    on_step(steps)
    finally:
        if session is not None:
            session.close()
    if use_snapshot:
        steps.append(
            {
                "tool": "run_diagnostic",
                "key": "docker_stats",
                "label": "容器资源快照(复用采集器快照)",
                "ok": True,
                "preview": (snapshot or "")[:2000],
            }
        )
        results.append(("docker_stats", snapshot or ""))
        if on_step:
            on_step(steps)
    order = {key: idx for idx, key in enumerate(catalog)}
    results.sort(key=lambda item: order.get(item[0], 99))
    evidence = "\n\n".join(
        f"## {catalog[key].get('label', key)}({key})\n{content}"
        for key, content in results
    )
    trend = _platform_trend_evidence(db, device)
    if trend:
        # 平台自己的采集数据:先看趋势再归因,能区分持续增长与突发泄漏。
        evidence = f"## 平台指标趋势(近 1 小时,平台采集)\n{trend}\n\n{evidence}"
    messages = [
        {
            "role": "system",
            "content": _build_system_prompt(catalog) + _SINGLE_PASS_NOTE,
        },
        {
            "role": "user",
            "content": (
                f"{question}\n\n运行时证据已采集完毕，如下:\n{evidence}\n\n"
                "请直接归因并给出处置建议。"
            ),
        },
    ]
    usage: list[dict] = []
    msg = _chat(
        messages,
        cfg,
        timeout=ALERT_ANALYSIS_LLM_TIMEOUT,
        max_retries=1,
        usage_sink=usage,
    )
    _append_usage_step(steps, usage, cfg)
    if on_step:
        on_step(steps)
    return msg.get("content") or "(模型未返回内容)"


# 诊断运行完成事件:run_id -> Event。_execute_run 落完终态后 set,
# 等待方(告警归因)用事件唤醒代替秒级轮询,几十次 DB 会话缩成 1~2 次。
_run_events: dict[int, threading.Event] = {}
_run_events_lock = threading.Lock()
# 同时执行的诊断运行数上限(批量诊断/告警归因集中触发时排队,
# 防止把目标机 SSH 连接与 LLM 配额同时打满)。
_run_slots = threading.BoundedSemaphore(AGENT_MAX_CONCURRENT_RUNS)


def _register_run_event(run_id: int) -> threading.Event:
    event = threading.Event()
    with _run_events_lock:
        _run_events[run_id] = event
    return event


def wait_run_completion(run_id: int, timeout: float) -> bool:
    """等待运行落到终态(事件在终态落库后 set)。返回是否被事件唤醒;
    False 表示超时或该运行不是本进程创建的(调用方自行回退轮询)。"""
    with _run_events_lock:
        event = _run_events.get(run_id)
    if event is None:
        return False
    return event.wait(timeout)


def start_diagnosis(
    device: Device,
    question: str,
    user_id: int,
    cfg: AgentConfig | None = None,
    focus: list[str] | tuple[str, ...] | None = None,
    single_pass: bool = False,
    mode: str = "readonly",
    job_id: int | None = None,
    job_target_id: int | None = None,
) -> int:
    """创建运行记录、后台线程执行诊断,立即返回 run_id(前端轮询进度)。

    job_id/job_target_id(2026-10-30 统一执行记录入口):实时诊断发起时同步
    落一条 automation job 记录,run 终态后由 _execute_run 回写任务状态。"""
    if cfg is None:
        db0 = SessionLocal()
        try:
            cfg = get_agent_config(db0)
        finally:
            db0.close()

    mode = mode if mode in _ACTION_MODES else "readonly"
    db = SessionLocal()
    try:
        run = AgentRun(
            user_id=user_id,
            device_id=device.id,
            device_name=device.name,
            device_ip=device.ip_address,
            question=question[:1000],
            status="running",
            mode=mode,
            created_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id
    finally:
        db.close()

    if job_target_id:
        # 预写 run 链接:任务详情抽屉运行中即可跳时间线(终态覆写完整结果)
        try:
            from app.services.automation import link_agent_run

            link_agent_run(job_target_id, run_id)
        except Exception:
            logger.warning(
                "link agent run %s to job target %s failed", run_id, job_target_id
            )

    _register_run_event(run_id)
    threading.Thread(
        target=_execute_run,
        args=(
            run_id,
            device,
            question,
            cfg,
            focus,
            single_pass,
            mode,
            job_id,
            job_target_id,
        ),
        daemon=True,
        name=f"agent-run-{run_id}",
    ).start()
    return run_id


def _execute_run(
    run_id: int,
    device: Device | AgentTarget,
    question: str,
    cfg: AgentConfig,
    focus: list[str] | tuple[str, ...] | None = None,
    single_pass: bool = False,
    mode: str = "readonly",
    job_id: int | None = None,
    job_target_id: int | None = None,
) -> None:
    """后台线程:执行诊断循环,步骤增量落库,结束写终态。

    执行体包在全局信号量里(AGENT_MAX_CONCURRENT_RUNS):批量诊断时排队,
    避免几十条线程同时握手 SSH + 打 LLM。终态落库后 set 完成事件。
    """
    started = time.time()
    steps: list[dict] = []

    status = "completed"
    report = None
    error = None
    try:
        with _run_slots:
            db = SessionLocal()
            try:
                if getattr(device, "id", None) is None:
                    # 虚拟机等运行时目标(AgentTarget)没有 ORM 身份，凭据在内存里;
                    # 按 id 回查 Device 表必然落空并报「设备不存在」。
                    target = device
                else:
                    target = db.query(Device).filter(Device.id == device.id).first()
                    if target is None:
                        raise AgentError("设备不存在")

                def on_step(current_steps) -> None:
                    _persist_steps(run_id, current_steps)

                if single_pass:
                    report = run_focus_diagnosis(
                        db, target, question, steps, cfg, focus or (), on_step=on_step
                    )
                else:
                    report = _loop(
                        db,
                        target,
                        question,
                        steps,
                        cfg,
                        on_step=on_step,
                        focus=focus,
                        mode=mode,
                        run_id=run_id,
                    )
            finally:
                db.close()
    except Exception as exc:
        logger.warning("Agent diagnosis failed for run %s: %s", run_id, exc)
        status = "failed"
        error = str(exc)[:500]

    duration_ms = int((time.time() - started) * 1000)

    db = SessionLocal()
    try:
        row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        if row:
            row.status = status
            row.report = report
            row.error = error
            row.steps_json = json.dumps(steps, ensure_ascii=False)
            row.duration_ms = duration_ms
            db.commit()
        # 安全网:异常路径下悬挂的审批收敛(正常路径各状态已就地流转)
        try:
            _finalize_dangling_approvals(db, run_id)
        except Exception:
            logger.debug("finalize approvals failed for run %s", run_id, exc_info=True)
    finally:
        db.close()
        # 统一执行记录入口(2026-10-30):实时诊断 run 终态同步任务记录。
        # 必须在 run 终态 commit 之后;失败只记日志,不影响 run 本身的状态。
        if job_id and job_target_id:
            try:
                from app.services.automation import finish_agent_job

                finish_agent_job(
                    job_id,
                    job_target_id,
                    "completed" if status == "completed" else "failed",
                    {"report": report, "legacy_run_id": run_id},
                    error,
                )
            except Exception:
                logger.warning("sync agent run %s to job %s failed", run_id, job_id)
        # 终态已提交,唤醒等待方;事件对象从注册表摘掉(等待方手里已持有引用,
        # dict 里留着只会泄漏)。
        with _run_events_lock:
            event = _run_events.pop(run_id, None)
        if event:
            event.set()


def run_diagnosis(
    device: Device | AgentTarget,
    question: str,
    user_id: int,
    cfg: AgentConfig | None = None,
    on_step=None,
    audit_device_id: int | None = None,
    focus: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """同步执行一次完整诊断(测试/内部调用用;API 走 start_diagnosis 异步)。

    cfg 为空时自行从 DB/.env 解析有效配置。
    """
    started = time.time()
    steps: list[dict] = []

    if cfg is None:
        db0 = SessionLocal()
        try:
            cfg = get_agent_config(db0)
        finally:
            db0.close()

    db = SessionLocal()
    run = AgentRun(
        user_id=user_id,
        device_id=audit_device_id if audit_device_id is not None else device.id,
        device_name=device.name,
        device_ip=device.ip_address,
        question=question[:1000],
        status="running",
        created_at=datetime.now(timezone.utc),
    )
    try:
        db.add(run)
        db.commit()
        db.refresh(run)
    finally:
        db.close()

    status = "completed"
    report = None
    error = None
    try:
        db2 = SessionLocal()
        try:
            report = _loop(
                db2, device, question, steps, cfg, on_step=on_step, focus=focus
            )
        finally:
            db2.close()
    except Exception as exc:
        logger.warning("Agent diagnosis failed for device %s: %s", device.id, exc)
        status = "failed"
        error = str(exc)[:500]

    duration_ms = int((time.time() - started) * 1000)

    db3 = SessionLocal()
    try:
        row = db3.query(AgentRun).filter(AgentRun.id == run.id).first()
        if row:
            row.status = status
            row.report = report
            row.error = error
            row.steps_json = json.dumps(steps, ensure_ascii=False)
            row.duration_ms = duration_ms
            db3.commit()
    finally:
        db3.close()

    return {
        "run_id": run.id,
        "status": status,
        "report": report,
        "error": error,
        "steps": steps,
        "duration_ms": duration_ms,
    }
