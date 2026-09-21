"""Best-effort Redis leader election for singleton background collectors.

只有 leader 才应该跑采集器/调度器，否则多副本部署时同一批设备会被成倍重复采集、
定时任务会被重复执行。这里用 Redis 的 ``SET key token NX EX 30`` 做租约锁，
每 10s 续租一次。

两条容易踩的坑，都在下面显式处理:

* **``--workers N`` 不会设置 ``WEB_CONCURRENCY``**。早先的实现在取锁之前先看
  ``WEB_CONCURRENCY == "1"`` 就直接自认 leader，于是"给 uvicorn 加 --workers 4"
  会让四个进程**全部**成为 leader、采集器四倍重复跑。现在只要配了 Redis 就一定走
  真正的分布式锁，``WEB_CONCURRENCY`` 只在"根本没有共享锁后端"时用来判断是否单进程。
* **续租失败不能直接放弃**。锁有 30s TTL，一次 Redis 抖动就退出续租循环，会让本进程
  在"自认 leader"的状态下继续跑采集器，而锁到期后另一个实例即可接管 —— 变成双 leader。
  所以瞬时错误要重试，只有确认"锁已经不属于自己"时才停止并大声报错。
"""

import asyncio
import logging
import os
import uuid

from app.config import RATE_LIMIT_STORAGE, WS_TICKET_STORAGE

logger = logging.getLogger(__name__)

# 锁 TTL 与续租间隔:连续 3 次续租失败(30s)即等于 TTL 到期，锁必然已经易主。
_LOCK_TTL_SECONDS = 30
_RENEW_INTERVAL_SECONDS = 10
_MAX_RENEW_FAILURES = _LOCK_TTL_SECONDS // _RENEW_INTERVAL_SECONDS

# 只有 token 匹配时才续期/删除，避免误动别人抢到的锁。
_RENEW_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)
_RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)


def _shared_redis_url() -> str | None:
    """Choose a configured shared Redis backend for distributed leadership."""
    for value in (WS_TICKET_STORAGE, RATE_LIMIT_STORAGE):
        if str(value or "").lower().startswith(("redis://", "rediss://")):
            return value
    return None


def _assumes_single_process() -> bool:
    """没有共享锁后端时，只能靠 WEB_CONCURRENCY 判断是不是单进程部署。"""
    return os.getenv("WEB_CONCURRENCY", "1") == "1"


class BackgroundLeader:
    def __init__(self) -> None:
        self.key = "dcn:background:leader"
        self.token = uuid.uuid4().hex
        self.client = None
        self.renew_task: asyncio.Task | None = None

    async def acquire(self) -> bool:
        redis_url = _shared_redis_url()
        if not redis_url:
            # 没有共享锁后端:单进程部署照常自任 leader；多 worker 必须配 Redis，
            # 否则宁可一个都不跑，也不能让采集器成倍重复执行。
            return _assumes_single_process()
        try:
            from redis.asyncio import Redis

            self.client = Redis.from_url(redis_url, decode_responses=True)
            ok = await self.client.set(
                self.key, self.token, nx=True, ex=_LOCK_TTL_SECONDS
            )
            if not ok:
                await self.client.close()
                self.client = None
                logger.info(
                    "Another instance holds the background leader lock; "
                    "collectors/scheduler stay off in this process"
                )
                return False
            self.renew_task = asyncio.create_task(self._renew())
            logger.info(
                "Acquired background leader lock (ttl=%ds, renew=%ds)",
                _LOCK_TTL_SECONDS,
                _RENEW_INTERVAL_SECONDS,
            )
            return True
        except Exception as exc:
            if self.client:
                await self.client.close()
            self.client = None
            # Local/dev fallback keeps the app usable; multi-worker deployments
            # must fail closed to avoid duplicate collectors.
            logger.warning(
                "Leader election unavailable (%s); falling back to %s",
                exc,
                "single-process mode"
                if _assumes_single_process()
                else "no background tasks",
            )
            return _assumes_single_process()

    async def _renew(self) -> None:
        failures = 0
        while True:
            await asyncio.sleep(_RENEW_INTERVAL_SECONDS)
            try:
                held = await self.client.eval(
                    _RENEW_SCRIPT,
                    1,
                    self.key,
                    self.token,
                    _LOCK_TTL_SECONDS,
                )
            except Exception as exc:
                failures += 1
                if failures < _MAX_RENEW_FAILURES:
                    # 锁还没到期，下一轮继续试；直接退出会丢掉锁保护。
                    logger.warning(
                        "Leader lock renewal failed (%d/%d): %s",
                        failures,
                        _MAX_RENEW_FAILURES,
                        exc,
                    )
                    continue
                logger.error(
                    "Leader lock renewal failed %d times in a row; the lock has "
                    "expired and another instance may take over while this process "
                    "keeps running its collectors. Restart this instance.",
                    failures,
                )
                return
            failures = 0
            if not held:
                logger.error(
                    "Leader lock is no longer held by this process (taken over by "
                    "another instance), but its collectors are still running. "
                    "Restart this instance to avoid duplicate collection."
                )
                return

    async def release(self) -> None:
        if self.renew_task:
            task = self.renew_task
            self.renew_task = None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.client:
            try:
                await self.client.eval(_RELEASE_SCRIPT, 1, self.key, self.token)
            finally:
                await self.client.close()
                self.client = None
