"""PveClient 进程内缓存(节点列表 TTL / 资源缓存)行为钉子。

节点列表 30s TTL:总览页 10s 轮询每次都打 /nodes 纯属浪费;缓存命中不打
PVE,TTL 过期重新拉取。资源缓存(12s)已有既有行为,这里只钉节点口径。
"""

from app.services import pve as pve_service
from app.services.pve import PveClient


def _clear_node_cache():
    with pve_service._resource_cache_lock:
        pve_service._node_cache.clear()


def _make_client():
    return PveClient(
        host="192.0.2.1",
        port=8006,
        token_id="root@pam!t",
        token_secret="secret",
        verify_ssl=False,
        timeout=15,
    )


def test_list_nodes_cached_within_ttl(monkeypatch):
    _clear_node_cache()
    client = _make_client()
    calls = {"n": 0}

    def fake_req(method, path, **kwargs):  # noqa: ARG001
        calls["n"] += 1
        return [{"node": "pve1", "status": "online"}]

    monkeypatch.setattr(client, "_req", fake_req)
    first = client.list_nodes()
    assert calls["n"] == 1
    # TTL 内的重复调用全部命中缓存(总览页 10s 轮询场景)
    for _ in range(3):
        cached = client.list_nodes()
        assert cached == first
    assert calls["n"] == 1
    # 返回的是拷贝:调用方改写不会污染缓存
    cached[0]["node"] = "mutated"
    assert client.list_nodes()[0]["node"] == "pve1"


def test_list_nodes_refetches_after_ttl(monkeypatch):
    _clear_node_cache()
    client = _make_client()
    calls = {"n": 0}

    def fake_req(method, path, **kwargs):  # noqa: ARG001
        calls["n"] += 1
        return [{"node": "pve1", "status": "online"}]

    monkeypatch.setattr(client, "_req", fake_req)
    client.list_nodes()

    # 时间快进越过 TTL:下一次调用重新拉取
    key = (client.base, int(client.timeout), client.authorization_header)
    with pve_service._resource_cache_lock:
        ts, data = pve_service._node_cache[key]
        pve_service._node_cache[key] = (ts - pve_service._NODE_CACHE_TTL - 1, data)
    client.list_nodes()
    assert calls["n"] == 2


def test_list_nodes_cache_keyed_per_client_identity():
    """不同 token/host 的客户端不共享节点缓存条目。"""
    a = _make_client()
    b = PveClient(
        host="192.0.2.2",
        port=8006,
        token_id="root@pam!other",
        token_secret="secret2",
        verify_ssl=False,
    )
    assert (a.base, int(a.timeout), a.authorization_header) != (
        b.base,
        int(b.timeout),
        b.authorization_header,
    )
