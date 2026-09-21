"""SSH transport 复用池测试（不连真实 SSH，用假工厂）。

存在意义：文件管理器每个动作（列目录/上传/下载/进目录）过去都独立走一次完整
握手。实测握手 ~164ms、open_sftp ~64ms，"打开弹窗 + 上传 4KB + 刷新列表" 要付
三次握手 ≈ 0.8s——传输本身几乎不花时间，用户看到的就是"小文件上传非常慢"。
"""

import time

import pytest

from app.services.ssh_pool import SshPool


class FakeTransport:
    def __init__(self, active=True):
        self._active = active

    def is_active(self):
        return self._active


class FakeClient:
    def __init__(self):
        self.closed = False
        self.transport = FakeTransport()

    def get_transport(self):
        return self.transport

    def close(self):
        self.closed = True


def _factory(counter):
    def _make():
        client = FakeClient()
        counter.append(client)
        return client

    return _make


KEY = ("10.0.0.1", 22, "root", "fp")


def test_second_acquire_reuses_transport():
    pool = SshPool()
    made: list = []
    first, created1 = pool.acquire(KEY, _factory(made))
    second, created2 = pool.acquire(KEY, _factory(made))
    assert created1 is True
    assert created2 is False
    assert first is second
    assert len(made) == 1, "复用命中时不应再握手"


def test_different_credentials_do_not_share():
    pool = SshPool()
    made: list = []
    other = ("10.0.0.1", 22, "root", "other-fp")
    pool.acquire(KEY, _factory(made))
    _client, created = pool.acquire(other, _factory(made))
    assert created is True
    assert len(made) == 2


def test_credential_fingerprint_is_stable_and_distinct():
    fp = SshPool.credential_fingerprint
    assert fp("pw", None) == fp("pw", None)
    assert fp("pw", None) != fp("pw2", None)
    assert fp(None, "key") != fp(None, "key2")
    # 密码与密钥不能互相混淆
    assert fp("x", None) != fp(None, "x")
    # 指纹里不含明文
    assert "secret" not in fp("secret", None)


def test_dead_transport_is_replaced():
    pool = SshPool()
    made: list = []
    client, _ = pool.acquire(KEY, _factory(made))
    client.transport = FakeTransport(active=False)  # 对端关掉了空闲连接
    new_client, created = pool.acquire(KEY, _factory(made))
    assert created is True
    assert new_client is not client
    assert client.closed is True, "失效连接必须被关闭，否则泄漏"


def test_idle_entries_expire():
    pool = SshPool(idle_ttl=0.01)
    made: list = []
    client, _ = pool.acquire(KEY, _factory(made))
    time.sleep(0.02)
    new_client, created = pool.acquire(KEY, _factory(made))
    assert created is True
    assert new_client is not client
    assert client.closed is True


def test_touch_keeps_entry_alive():
    pool = SshPool(idle_ttl=0.05)
    made: list = []
    client, _ = pool.acquire(KEY, _factory(made))
    for _ in range(4):
        time.sleep(0.02)
        pool.touch(KEY)  # 长传输期间续租
    reused, created = pool.acquire(KEY, _factory(made))
    assert created is False
    assert reused is client
    assert len(made) == 1


def test_pool_size_is_capped():
    pool = SshPool(max_size=2)
    made: list = []
    for i in range(4):
        pool.acquire((f"10.0.0.{i}", 22, "root", "fp"), _factory(made))
    assert len(pool) <= 2
    assert sum(1 for c in made if c.closed) >= 2, "被淘汰的连接必须关闭"


def test_discard_removes_and_closes():
    pool = SshPool()
    made: list = []
    client, _ = pool.acquire(KEY, _factory(made))
    pool.discard(KEY)
    assert client.closed is True
    assert len(pool) == 0
    _c, created = pool.acquire(KEY, _factory(made))
    assert created is True


def test_factory_failure_is_not_cached():
    """握手失败不能把坏条目留在池里，否则后续请求一直命中死连接。"""
    pool = SshPool()

    def _boom():
        raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError):
        pool.acquire(KEY, _boom)
    assert len(pool) == 0

    made: list = []
    client, created = pool.acquire(KEY, _factory(made))
    assert created is True
    assert client is made[0]


def test_close_all():
    pool = SshPool()
    made: list = []
    pool.acquire(KEY, _factory(made))
    pool.acquire(("10.0.0.2", 22, "root", "fp"), _factory(made))
    pool.close_all()
    assert len(pool) == 0
    assert all(c.closed for c in made)
