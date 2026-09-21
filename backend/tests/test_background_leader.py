"""后台 leader 选举:多副本部署时只能有一个进程跑采集器/调度器。

用假的 Redis 客户端覆盖选主与续租语义，不依赖真实 Redis。这里钉住两个曾经存在的坑:

* ``--workers N`` 不会设置 ``WEB_CONCURRENCY``，所以只要配了 Redis 就必须走真锁，
  否则四个 worker 会全部自认 leader、采集器四倍重复跑;
* 续租遇到一次 Redis 抖动就退出循环，会让本进程在"自认 leader"的状态下永久失去锁
  保护，锁到期后另一个实例接管 → 双 leader。

每条用例都在**同一个**事件循环里完成 acquire/断言/release:续租是 asyncio.Task，
跨 asyncio.run 操作它会因为绑在已关闭的循环上而报 CancelledError。
"""

import asyncio

import pytest
import redis.asyncio as aioredis

from app.services import background_leader as bl


class FakeRedis:
    """最小可用的 redis.asyncio 客户端替身，记录调用并按脚本返回结果。"""

    def __init__(self, *, set_ok=True, eval_results=None, eval_errors=None):
        self.set_ok = set_ok
        self.calls: list[tuple] = []
        self.closed = False
        self._eval_results = list(eval_results or [])
        self._eval_errors = list(eval_errors or [])

    async def set(self, key, token, nx=False, ex=None):
        self.calls.append(("set", key, token, nx, ex))
        return self.set_ok

    async def eval(self, script, numkeys, *args):
        self.calls.append(("eval", script, numkeys, args))
        if self._eval_errors:
            error = self._eval_errors.pop(0)
            if error is not None:
                raise error
        if self._eval_results:
            return self._eval_results.pop(0)
        return 1

    async def close(self):
        self.closed = True

    @property
    def set_calls(self):
        return [call for call in self.calls if call[0] == "set"]

    @property
    def eval_calls(self):
        return [call for call in self.calls if call[0] == "eval"]


@pytest.fixture()
def fast_renew(monkeypatch):
    """把续租间隔压到毫秒级，否则每条用例都要等 10s。"""
    monkeypatch.setattr(bl, "_RENEW_INTERVAL_SECONDS", 0.01)


@pytest.fixture()
def install_redis(monkeypatch):
    """把 Redis.from_url 换成替身工厂，返回创建出来的客户端列表。"""
    created: list[FakeRedis] = []

    def install(**kwargs):
        def _from_url(_url, **_kw):
            client = FakeRedis(**kwargs)
            created.append(client)
            return client

        monkeypatch.setattr(aioredis.Redis, "from_url", staticmethod(_from_url))
        return created

    return install


@pytest.fixture()
def with_redis(monkeypatch):
    monkeypatch.setattr(bl, "_shared_redis_url", lambda: "redis://localhost:6379/0")


@pytest.fixture()
def without_redis(monkeypatch):
    monkeypatch.setattr(bl, "_shared_redis_url", lambda: None)


def test_acquire_takes_real_lock_even_without_web_concurrency(
    monkeypatch, install_redis, with_redis, fast_renew
):
    """核心防回归:配了 Redis 就必须取真锁，不能因为 WEB_CONCURRENCY 未设置就自封 leader。"""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    created = install_redis(set_ok=True)

    async def scenario():
        leader = bl.BackgroundLeader()
        assert await leader.acquire() is True
        assert len(created) == 1
        assert len(created[0].set_calls) == 1, "没走 SET NX，说明又短路成自封 leader 了"
        _cmd, key, token, nx, ex = created[0].set_calls[0]
        assert key == "dcn:background:leader"
        assert token == leader.token
        assert nx is True
        assert ex == bl._LOCK_TTL_SECONDS
        await leader.release()

    asyncio.run(scenario())


def test_acquire_is_false_when_another_instance_holds_lock(
    install_redis, with_redis, fast_renew
):
    """抢不到锁就不是 leader，且要关掉连接、不启动续租。"""
    created = install_redis(set_ok=False)

    async def scenario():
        leader = bl.BackgroundLeader()
        assert await leader.acquire() is False
        assert created[0].closed is True
        assert leader.renew_task is None
        assert leader.client is None

    asyncio.run(scenario())


def test_acquire_without_redis_single_process_becomes_leader(
    monkeypatch, install_redis, without_redis
):
    """本地/单进程且没有 Redis 时照常自任 leader，否则开发环境跑不了采集。"""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    created = install_redis()

    assert asyncio.run(bl.BackgroundLeader().acquire()) is True
    assert not created, "没有 Redis 却去建了连接"


def test_acquire_without_redis_multi_worker_fails_closed(monkeypatch, without_redis):
    """多 worker 又没有共享锁后端时，宁可一个都不跑也不能重复采集。"""
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    assert asyncio.run(bl.BackgroundLeader().acquire()) is False


def test_acquire_falls_back_when_redis_unreachable(monkeypatch, with_redis, fast_renew):
    """Redis 挂了不能让应用起不来:单进程降级自任 leader，多 worker 仍 fail closed。"""

    def _boom(_url, **_kw):
        raise ConnectionError("redis down")

    monkeypatch.setattr(aioredis.Redis, "from_url", staticmethod(_boom))

    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    assert asyncio.run(bl.BackgroundLeader().acquire()) is True

    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    assert asyncio.run(bl.BackgroundLeader().acquire()) is False


def test_renew_survives_transient_errors(install_redis, with_redis, fast_renew):
    """一次 Redis 抖动不能终止续租，否则锁到期后会出现双 leader。"""
    created = install_redis(eval_errors=[ConnectionError("blip"), None, None])

    async def scenario():
        leader = bl.BackgroundLeader()
        assert await leader.acquire() is True
        await asyncio.sleep(0.1)  # 足够跑好几轮续租
        alive = leader.renew_task is not None and not leader.renew_task.done()
        renewals = len(created[0].eval_calls)
        await leader.release()
        return alive, renewals

    alive, renewals = asyncio.run(scenario())
    assert alive, "续租循环因一次异常就退出了"
    assert renewals >= 3, f"抖动之后没有继续续租(只续了 {renewals} 次)"


def test_renew_stops_when_lock_is_lost(install_redis, with_redis, fast_renew):
    """eval 返回 0 说明锁已被别人抢走，必须停止续租并留下可排查的错误日志。"""
    install_redis(eval_results=[0])

    async def scenario():
        leader = bl.BackgroundLeader()
        assert await leader.acquire() is True
        await asyncio.wait_for(leader.renew_task, timeout=1)
        done = leader.renew_task.done()
        await leader.release()
        return done

    assert asyncio.run(scenario()) is True


def test_release_only_deletes_own_lock(install_redis, with_redis, fast_renew):
    """释放必须带 token 校验，不能把别人抢到的锁删掉。"""
    created = install_redis()

    async def scenario():
        leader = bl.BackgroundLeader()
        assert await leader.acquire() is True
        await leader.release()
        return leader

    leader = asyncio.run(scenario())
    release_calls = [c for c in created[0].eval_calls if "del" in c[1]]
    assert len(release_calls) == 1
    assert leader.token in release_calls[0][3], "释放脚本没带自己的 token"
    assert created[0].closed is True
    assert leader.client is None
