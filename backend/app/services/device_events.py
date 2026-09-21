"""自动化通知事件 ``automation.notify`` 的订阅判定与存量事件名迁移。

历史上这里散着 ``inspection.report`` / ``inspection.failed`` / ``job.failed``
三个事件，加上更早的 ``device.offline/online``。问题是：

  * 巡检失败其实**已经体现在巡检报告里**（全失败时报告正文就是"未取得任何结果"），
    单独再发一条 ``inspection.failed`` 是重复通知；
  * ``job.failed`` 只在失败时发，而 ``inspection.report`` 每次完成都发，
    两个订阅选项语义重叠，用户勾哪个都很困惑；
  * ``device.offline/online`` 与 ``host_status`` 告警规则重复建设，且不做设备级
    opt-in，已按方案 A 撤掉。

现已收敛为**唯一事件 ``automation.notify``，且只有健康巡检会发射**
（任务结束后发摘要 + PDF 报告，见 ``inspection_report``）。
Agent 智能判断 / 批量执行 / 电源操作的结果在任务详情实时可见，一律不推 webhook。

本模块因此只保留：事件名常量、旧订阅名迁移、订阅判定。巡检报告的
payload 合成与投递由 ``inspection_report`` 自行完成（复用告警的 ``_deliver_one``）。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.webhook import Webhook

logger = logging.getLogger(__name__)

# 唯一对外的自动化通知事件
EVENT_AUTOMATION_NOTIFY = "automation.notify"

# 旧事件名 → 新事件名。启动时把存量 webhook 订阅迁移过来（见 migrate_event_names）。
_LEGACY_EVENT_ALIASES = {
    "inspection.report": EVENT_AUTOMATION_NOTIFY,
    "inspection.failed": EVENT_AUTOMATION_NOTIFY,
    "job.failed": EVENT_AUTOMATION_NOTIFY,
    # 发射点已撤掉，订阅一并清掉，避免界面上留着永远收不到的勾
    "device.offline": None,
    "device.online": None,
}


def migrate_event_names(db: Session) -> int:
    """把存量 webhook 的旧事件名迁移到 automation.notify（幂等）。

    返回被改动的 webhook 行数。启动时调用一次；重复调用无副作用。
    """
    changed = 0
    for hook in db.query(Webhook).all():
        original = list(hook.events or [])
        if not original:
            continue
        migrated: list[str] = []
        for name in original:
            target = _LEGACY_EVENT_ALIASES.get(name, name)
            if target and target not in migrated:
                migrated.append(target)
        if migrated != original:
            hook.events = migrated
            changed += 1
    if changed:
        db.commit()
        logger.info("Migrated webhook event names on %d webhook(s)", changed)
    return changed


def is_automation_notify_subscribed(events) -> bool:
    """判断一个 webhook 的订阅列表是否应收 automation.notify。

    除新事件名外，也认存量未迁移的旧名字（inspection.report /
    inspection.failed / job.failed）——否则在迁移跑过之前（进程未重启、
    或跑的是旧镜像）开关打开了也一条都发不出去，表现为「配了没反应」。
    """
    names = set(events or [])
    if EVENT_AUTOMATION_NOTIFY in names:
        return True
    return any(_LEGACY_EVENT_ALIASES.get(n) == EVENT_AUTOMATION_NOTIFY for n in names)
