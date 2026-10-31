"""Agent 动作审批运行时(从 agent.py 拆出,2026-10-31 Phase-2)。

提议渲染定稿 → 审批等待(事件唤醒,等待线程持有 SSH 会话) → 执行 →
审计;以及 decide_approval 决策与 run 结束/进程重启的悬空收尾。
契约见 AGENTS.md「Agent 分级动作系统」。
测试 monkeypatch 的 agent._exec_diagnostic / agent.AGENT_ACTION_* 经
延迟导入的 agent 门面解析(agent.* patch 语义不变)。
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import AGENT_MAX_OUTPUT_CHARS
from app.database import SessionLocal
from app.models.agent_action_approval import AgentActionApproval
from app.services import agent_actions, agent_primitives
from app.services.agent_exec import _diagnostic_output

logger = logging.getLogger(__name__)

# 审批事件表:decide_approval 唤醒等待线程;跨实例部署时是本进程私有,
# 与等待线程同进程语义一致(审批经 API 落在等待进程内完成)。
_approval_events: dict[int, threading.Event] = {}
_approval_events_lock = threading.Lock()


def _action_denied(steps: list[dict], key: str, params, error: str) -> str:
    steps.append(
        {
            "tool": "run_action",
            "key": key,
            "params": params,
            "ok": False,
            "final": "refused",
            "error": error,
        }
    )
    return json.dumps({"key": key, "error": error}, ensure_ascii=False)


def _run_action_exec(
    db, device, command: str, timeout: int, session
) -> tuple[int, str]:
    from app.services import agent as _agent

    exit_code, out, err = _agent._exec_diagnostic(
        db, device, command, timeout, session=session
    )
    text = agent_primitives.redact_output(_diagnostic_output(exit_code, out, err))
    return exit_code, text[:AGENT_MAX_OUTPUT_CHARS]


def _dispatch_run_action(
    db,
    device,
    args: dict,
    steps: list[dict],
    mode,
    l1_auto,
    run_id,
    session,
    on_step=None,
) -> str:
    from app.services import agent as _agent

    """run_action 分支:L1(策略允许)自动执行;否则建审批行并挂起等待人工决策。

    审批人批准的就是提议时定稿入库的 command(不可变),执行/审计同一份字符串。
    超时与决策的竞态由条件更新裁决:等待超时后 pending→expired 改不动,说明
    审批人恰好刚批,按已批准继续执行。
    """
    key = str(args.get("key") or "")
    params = args.get("params")
    reason = str(args.get("reason") or "").strip()

    if mode == "readonly":
        return _action_denied(
            steps, key, params, "当前诊断运行在只读模式,变更动作不可用"
        )
    if not reason:
        return _action_denied(steps, key, params, "缺少 reason(处置理由,审批卡会展示)")

    proposed = [s for s in steps if s.get("tool") == "run_action" and s.get("key")]
    if len(proposed) >= _agent.AGENT_ACTION_LIMIT:
        return _action_denied(
            steps,
            key,
            params,
            f"本次诊断的动作预算已用完(上限 {_agent.AGENT_ACTION_LIMIT})",
        )
    if any(s.get("final") == "pending" for s in proposed):
        return _action_denied(steps, key, params, "已有动作在等待审批,一次只能一个")

    canon = json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
    for s in proposed:
        if (
            s.get("key") == key
            and s.get("params_canon") == canon
            and s.get("final") in ("rejected", "expired")
        ):
            return _action_denied(
                steps, key, params, "该动作已被拒绝或审批超时,不得重提;请转只读结论"
            )
    if (
        sum(1 for s in proposed if s.get("approval_id"))
        >= _agent.AGENT_ACTION_APPROVAL_LIMIT
    ):
        return _action_denied(
            steps,
            key,
            params,
            f"审批次数已达上限({_agent.AGENT_ACTION_APPROVAL_LIMIT});请转只读结论",
        )

    try:
        action = agent_actions.build_action(key, params, device)
    except agent_primitives.PrimitiveError as exc:
        return _action_denied(steps, key, params, f"参数校验失败: {exc}")

    needs_approval = (
        action["risk"] == "l2"
        or mode == "strict"
        or (action["risk"] == "l1" and not l1_auto)
    )

    if not needs_approval:
        step = {
            "tool": "run_action",
            "key": key,
            "params": params,
            "params_canon": canon,
            "command": action["command"],
            "risk": action["risk"],
            "reason": reason[:500],
            "auto": True,
        }
        steps.append(step)
        try:
            exit_code, text = _run_action_exec(
                db, device, action["command"], action["timeout"], session
            )
            _audit_container_action(
                db, device, key, params, exit_code, action["command"]
            )
        except Exception as exc:
            step.update(
                {"final": "execution_failed", "ok": False, "preview": str(exc)[:300]}
            )
            return json.dumps({"key": key, "error": str(exc)[:300]}, ensure_ascii=False)
        step.update(
            {
                "final": "executed" if exit_code == 0 else "execution_failed",
                "ok": exit_code == 0,
                "preview": text[:300],
            }
        )
        return json.dumps(
            {"key": key, "exit_code": exit_code, "output": text}, ensure_ascii=False
        )

    now = datetime.now(timezone.utc)
    approval = AgentActionApproval(
        run_id=run_id,
        action_key=key,
        params_json=canon,
        command=action["command"],
        risk_level=action["risk"],
        reason=reason[:1000],
        status="pending",
        requested_at=now,
        expires_at=now + timedelta(seconds=_agent.AGENT_ACTION_APPROVAL_TIMEOUT),
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)

    event = threading.Event()
    with _approval_events_lock:
        _approval_events[approval.id] = event
    step = {
        "tool": "run_action",
        "key": key,
        "params": params,
        "params_canon": canon,
        "command": action["command"],
        "risk": action["risk"],
        "reason": reason[:500],
        "approval_id": approval.id,
        "final": "pending",
    }
    steps.append(step)
    # 等待期间步骤也必须可见(2026-10-30 修):审批卡(批准/拒绝/倒计时)渲染
    # 依赖 steps 里的 pending 行,而常规 on_step 在 _dispatch 返回后才触发——
    # event.wait 阻塞的整个窗口里不先落库,前端只见「等待审批」标签、
    # 见不到审批卡与按钮。
    if on_step:
        on_step(steps)
    try:
        signaled = event.wait(timeout=_agent.AGENT_ACTION_APPROVAL_TIMEOUT)
        # 决策写在另一条连接上;run 级长会话在 REPEATABLE READ 下持有插入后
        # 打开的旧快照,醒来后重读会看不到决策——裁决与重读必须用独立短会话。
        s2 = SessionLocal()
        try:
            if not signaled:
                s2.query(AgentActionApproval).filter(
                    AgentActionApproval.id == approval.id,
                    AgentActionApproval.status == "pending",
                ).update(
                    {"status": "expired", "decided_at": datetime.now(timezone.utc)}
                )
                s2.commit()
            row = (
                s2.query(AgentActionApproval)
                .filter(AgentActionApproval.id == approval.id)
                .first()
            )
        finally:
            s2.close()
        if row is None or row.status == "pending":  # 防御:不应发生
            step.update({"final": "expired", "ok": False})
            return json.dumps({"key": key, "status": "expired"}, ensure_ascii=False)
        if row.status == "approved":
            exit_code = None
            try:
                exit_code, text = _run_action_exec(
                    db, device, row.command, action["timeout"], session
                )
                _audit_container_action(db, device, key, params, exit_code, row.command)
                final = "executed" if exit_code == 0 else "execution_failed"
            except Exception as exc:
                text, final = str(exc)[:300], "execution_failed"
            db.query(AgentActionApproval).filter(
                AgentActionApproval.id == approval.id
            ).update(
                {
                    "status": final,
                    "output": text[:8000],
                    "executed_at": datetime.now(timezone.utc),
                }
            )
            db.commit()
            step.update({"final": final, "ok": exit_code == 0, "preview": text[:300]})
            payload = (
                {"key": key, "approved": True, "exit_code": exit_code, "output": text}
                if exit_code is not None
                else {"key": key, "approved": True, "error": text}
            )
            return json.dumps(payload, ensure_ascii=False)
        step.update(
            {"final": row.status, "ok": False, "decision_note": row.decision_note}
        )
        detail = (
            f"审批被拒绝:{row.decision_note}"
            if row.status == "rejected"
            else "审批超时未决策,默认拒绝"
        )
        return json.dumps(
            {"key": key, "status": row.status, "detail": detail}, ensure_ascii=False
        )
    finally:
        with _approval_events_lock:
            _approval_events.pop(approval.id, None)


def decide_approval(
    db: Session, approval_id: int, approve: bool, user_id: int, note: str = ""
):
    """人工决策(API 层调用):pending→approved/rejected 行锁内原子流转,
    成功后唤醒挂起的诊断线程。执行由等待线程完成(SSH 会话在它手里)。
    返回 None 表示已不在 pending(已被决策/过期/不存在)。"""
    row = (
        db.query(AgentActionApproval)
        .filter(AgentActionApproval.id == approval_id)
        .with_for_update()
        .first()
    )
    if row is None or row.status != "pending":
        return None
    row.status = "approved" if approve else "rejected"
    row.decided_at = datetime.now(timezone.utc)
    row.decided_by = user_id
    row.decision_note = (note or "").strip()[:500] or None
    db.commit()
    with _approval_events_lock:
        event = _approval_events.get(approval_id)
    if event is not None:
        event.set()
    db.refresh(row)
    return row


def _finalize_dangling_approvals(db: Session, run_id: int | None = None) -> int:
    """run 结束/中断或进程重启后收敛悬挂审批:pending→expired、approved 未执行
    →execution_failed。恢复路径**绝不自动补执行写操作**(与自愈中断哲学一致)。"""
    pending_q = db.query(AgentActionApproval).filter(
        AgentActionApproval.status == "pending"
    )
    approved_q = db.query(AgentActionApproval).filter(
        AgentActionApproval.status == "approved"
    )
    if run_id is not None:
        pending_q = pending_q.filter(AgentActionApproval.run_id == run_id)
        approved_q = approved_q.filter(AgentActionApproval.run_id == run_id)
    now = datetime.now(timezone.utc)
    n1 = pending_q.update(
        {"status": "expired", "decided_at": now}, synchronize_session=False
    )
    n2 = approved_q.update(
        {
            "status": "execution_failed",
            "executed_at": now,
            "decision_note": "诊断线程在执行前退出",
        },
        synchronize_session=False,
    )
    if n1 or n2:
        db.commit()
    return n1 + n2


def _audit_container_action(db, device, key, params, exit_code, command) -> None:
    """Agent 的容器动作同步落 container_actions(source=agent),与自愈/界面同审计面。"""
    if not key.startswith("container_"):
        return
    device_id = getattr(device, "id", None)
    if device_id is None:
        return  # AgentTarget(虚机)无 ORM 身份,steps 已留痕
    from app.models.device_container import ContainerAction

    try:
        db.add(
            ContainerAction(
                user_id=None,
                device_id=device_id,
                device_name=getattr(device, "name", None),
                container_name=str((params or {}).get("name") or ""),
                action=key.removeprefix("container_"),
                success=1 if exit_code == 0 else 0,
                message=command[:300],
                source="agent",
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.debug("agent container action audit failed", exc_info=True)
