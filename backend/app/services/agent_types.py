"""Agent 公共类型与 LLM 配置读取(从 agent.py 拆出,Phase-1 2026-10-31)。

AgentTarget 是无 ORM 身分的运行时目标(设备或 PVE 虚机),被 agent 主循环、
metrics/agent_metrics、exec/agent_exec 共用——放在独立模块避免回环 import。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session

from app.config import (
    AGENT_ENABLED,
    AGENT_LLM_API_KEY,
    AGENT_LLM_BASE_URL,
    AGENT_LLM_MODEL,
    AGENT_MAX_STEPS,
)
from app.models.device import is_windows_os
from app.services.crypto import decrypt
from app.services.settings import get_setting

logger = logging.getLogger(__name__)


def session_retry(db: Session, fn):
    """诊断 run 的长持有会话自愈：连接被服务端抬掉后回滚一次换新连接重试。

    诊断 run 的 DB 会话会横跨 LLM 轮次(每轮几十秒)、SSH 取证与审批等待(最长
    ``AGENT_ACTION_APPROVAL_TIMEOUT``)，期间连接空闲；MySQL ``wait_timeout``
    较短/服务重启/网络回收时连接被单方面断开，下次用它写库就报
    ``pymysql.err.InterfaceError (0, '')``。连接池的 ``pool_pre_ping`` 只在
    借出时探测，管不到已借出的会话——所以在这些中途触点上回滚后重试：
    回滚使会话作废旧连接，下一次查询从池里拿新连接（pre_ping 兜底）。
    连接健康时 fn 只执行一次，行为与直调完全一致。
    """
    try:
        return fn()
    except (InterfaceError, OperationalError):
        logger.warning(
            "agent run db connection lost, retrying once on a fresh connection"
        )
        db.rollback()
        return fn()


class AgentError(Exception):
    """LLM 未配置/调用失败/步数超限等。"""


@dataclass
class AgentTarget:
    """Device-compatible runtime target for a PVE guest binding."""

    id: int | None
    name: str
    ip_address: str | None
    os_system: str
    credential_id: int | None = None
    username: str | None = None
    password: str | None = None
    ssh_key: str | None = None
    ssh_port: int = 22
    winrm_port: int = 5985
    ssh_host_key: str | None = None
    type: str = "server"

    @property
    def is_windows(self) -> bool:
        """与 Device.is_windows 同一口径(共享 is_windows_os 真源)。"""
        return is_windows_os(self.os_system)


# 进程重启打断诊断时写进 AgentRun.error 的固定原因。告警自愈据此区分"平台自己
# 把归因掐了"和"归因真跑出了失败结论"，前者要退回未分析重跑，不该报给值班人。


# ── 配置解析:DB 设置优先,.env 兜底(系统管理页可改,保存即生效)──


class AgentConfig:
    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        api_key: str,
        model: str,
        max_steps: int,
    ):
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_steps = max_steps

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @property
    def ready(self) -> bool:
        return self.enabled and self.configured


def get_agent_config(db: Session) -> AgentConfig:
    """有效配置:DB(agent_*)优先,未设置项回退 .env。API Key 在 DB 中加密存储。"""
    enabled_raw = get_setting(db, "agent_enabled")
    enabled = (enabled_raw.lower() == "true") if enabled_raw else AGENT_ENABLED

    base_url = get_setting(db, "agent_base_url") or AGENT_LLM_BASE_URL

    api_key = ""
    api_key_enc = get_setting(db, "agent_api_key")
    if api_key_enc:
        try:
            api_key = decrypt(api_key_enc)
        except Exception:
            logger.warning("agent_api_key 解密失败,回退 .env")
    if not api_key:
        api_key = AGENT_LLM_API_KEY

    model = get_setting(db, "agent_model") or AGENT_LLM_MODEL

    max_steps_raw = get_setting(db, "agent_max_steps")
    try:
        max_steps = int(max_steps_raw) if max_steps_raw else AGENT_MAX_STEPS
    except ValueError:
        max_steps = AGENT_MAX_STEPS
    max_steps = min(max(max_steps, 1), 20)

    return AgentConfig(
        enabled=enabled,
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_steps=max_steps,
    )
