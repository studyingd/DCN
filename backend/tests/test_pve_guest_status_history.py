"""pve_guest_status 内存快照历史(供 Agent 归因 / get_metrics 取虚拟机趋势)。"""

import time
from types import SimpleNamespace

from app.services import pve_guest_status as svc


class _FakeClient:
    def __init__(self, guests: list[dict]):
        self._guests = guests

    def list_guest_resources(self) -> list[dict]:
        return self._guests


def _raw(vmid: int = 101, **kwargs) -> dict:
    base = {
        "vmid": vmid,
        "id": f"qemu/{vmid}",
        "type": "qemu",
        "name": f"vm-{vmid}",
        "node": "pve1",
        "status": "running",
        "template": 0,
        "cpu": 0.25,
        "mem": 2 * 1024**3,
        "maxmem": 8 * 1024**3,
        "uptime": 1000,
    }
    base.update(kwargs)
    return base


def setup_function(_):
    svc._guests.clear()
    svc._history.clear()
    svc._connection_states.clear()
    svc._last_known_ip.clear()
    svc._ip_probe_at.clear()


def _refresh(monkeypatch, guests: list[dict]) -> None:
    monkeypatch.setattr(svc, "build_client", lambda conn: _FakeClient(guests))
    svc.refresh_connection(SimpleNamespace(id=1, name="pve"))


def test_refresh_appends_history_per_cycle(monkeypatch):
    _refresh(monkeypatch, [_raw(cpu=0.10)])
    _refresh(monkeypatch, [_raw(cpu=0.50)])
    hist = svc.get_guest_metric_history(1, 101)
    assert len(hist) == 2
    # 旧 → 新
    assert hist[0]["cpu"] == 0.10
    assert hist[1]["cpu"] == 0.50
    assert hist[1]["ts"] >= hist[0]["ts"]
    # 最新快照不受历史影响
    assert svc.get_guest_snapshot(1, 101)["cpu"] == 0.50


def test_history_is_bounded(monkeypatch):
    for _ in range(svc._HISTORY_MAXLEN + 10):
        _refresh(monkeypatch, [_raw()])
    assert len(svc.get_guest_metric_history(1, 101)) == svc._HISTORY_MAXLEN


def test_history_for_unknown_guest_is_empty(monkeypatch):
    _refresh(monkeypatch, [_raw()])
    assert svc.get_guest_metric_history(1, 999) == []
    assert svc.get_guest_metric_history(9, 101) == []


def test_failed_refresh_keeps_old_history(monkeypatch):
    _refresh(monkeypatch, [_raw()])

    class _Boom:
        def list_guest_resources(self):
            raise RuntimeError("pve down")

    monkeypatch.setattr(svc, "build_client", lambda conn: _Boom())
    assert svc.refresh_connection(SimpleNamespace(id=1, name="pve")) is False
    # PVE 不可达时保留旧快照与历史
    assert len(svc.get_guest_metric_history(1, 101)) == 1


# ── guest 最近已知 IP(离线告警的地址兜底,2026-09-17) ──


class _QgaClient:
    """带 agent interfaces 返回值的假 client:值非 None 才能被调用。"""

    def __init__(self, interfaces: dict | None):
        self._interfaces = interfaces
        self.calls = 0

    def guest_agent_interfaces(self, node: str, gtype: str, vmid: int):
        self.calls += 1
        if self._interfaces is None:
            raise RuntimeError("agent unavailable")
        return self._interfaces


_INTERFACES = {
    "result": [
        {
            "name": "eth0",
            "ip-addresses": [
                {"ip-address": "192.168.1.127", "ip-address-type": "ipv4"},
                {"ip-address": "127.0.0.1", "ip-address-type": "ipv4"},
                {"ip-address": "fe80::1", "ip-address-type": "ipv6"},
            ],
        }
    ]
}


class _FakeDb:
    """闸门(host_status 规则存在)+ 连接清单可控的假会话。"""

    def __init__(self, connections, has_rules=True):
        self._connections = connections
        self._has_rules = has_rules

    def _chain(self):
        db = self

        class _Chain:
            # 查询链只有两种形态:query(AlertRule.id).filter(...).first()
            # (规则闸门)与 query(PveConnection).filter(...).all()(连接清单),
            # 同一个链对象同时提供 first/all 即可。
            def filter(self, *a, **k):
                return self

            def first(self):
                return object() if db._has_rules else None

            def all(self):
                return list(db._connections)

        return _Chain()

    def query(self, model):
        return self._chain()

    def close(self):
        pass


def test_refresh_records_running_guest_ip(monkeypatch):
    """刷新循环把 running 虚机 QGA 报的地址记入最近已知;停机 guest 不探测。"""
    client = _QgaClient(_INTERFACES)
    monkeypatch.setattr(svc, "build_client", lambda conn: client)
    monkeypatch.setattr(svc, "SessionLocal", lambda: _FakeDb([SimpleNamespace(id=1)]))
    svc._guests.update(
        {
            (1, 101): dict(_raw(101), status="running", node="pve1", guest_type="qemu"),
            (1, 102): dict(_raw(102), status="stopped", node="pve1", guest_type="qemu"),
        }
    )
    updated = svc._refresh_guest_ips_sync()
    assert updated == 1
    assert svc.get_last_known_ip(1, 101) == "192.168.1.127"
    assert svc.get_last_known_ip(1, 102) is None
    assert client.calls == 1  # 停机 guest 没被探测


def test_refresh_skips_without_host_status_rules(monkeypatch):
    """闸门:没有启用中的 host_status 规则时整轮直接跳过,不发 QGA 请求。"""
    client = _QgaClient(_INTERFACES)
    monkeypatch.setattr(svc, "build_client", lambda conn: client)
    monkeypatch.setattr(svc, "SessionLocal", lambda: _FakeDb([], has_rules=False))
    svc._guests.update({(1, 101): dict(_raw(101), status="running", guest_type="qemu")})
    assert svc._refresh_guest_ips_sync() == 0
    assert client.calls == 0
    assert svc.get_last_known_ip(1, 101) is None


def test_last_known_ip_expiry_and_blanks(monkeypatch):
    svc.record_guest_ip(1, 201, "10.1.1.5")
    assert svc.get_last_known_ip(1, 201) == "10.1.1.5"
    # 过期(24h)视为未知
    with svc._lock:
        ip, ts = svc._last_known_ip[(1, 201)]
        svc._last_known_ip[(1, 201)] = (ip, ts - 25 * 3600)
    assert svc.get_last_known_ip(1, 201) is None
    # 空值不记录
    svc.record_guest_ip(1, 202, "  ")
    assert svc.get_last_known_ip(1, 202) is None


def test_refresh_backs_off_after_failed_probe(monkeypatch):
    """agent 未启用的 guest:探测失败后退避,同轮/短期内不再空耗超时。"""
    dead_client = _QgaClient(None)
    monkeypatch.setattr(svc, "build_client", lambda conn: dead_client)
    monkeypatch.setattr(svc, "SessionLocal", lambda: _FakeDb([SimpleNamespace(id=1)]))
    svc._guests.update({(1, 301): dict(_raw(301), status="running", guest_type="qemu")})
    assert svc._refresh_guest_ips_sync() == 0
    assert dead_client.calls == 1
    # 再次触发:退避期内不再探测
    assert svc._refresh_guest_ips_sync() == 0
    assert dead_client.calls == 1
    # 退避期过后再探测一次
    with svc._lock:
        ok, ts = svc._ip_probe_at[(1, 301)]
        svc._ip_probe_at[(1, 301)] = (ok, ts - svc._IP_PROBE_BACKOFF - 1)
    assert svc._refresh_guest_ips_sync() == 0
    assert dead_client.calls == 2


# ── 超时按次透传,不回写共享客户端(修复:client.timeout 一度被粘住) ──


class _TimeoutRecordingClient:
    """记录 list_guest_resources 调用参数;timeout 属性模拟客户端默认超时。"""

    def __init__(self, guests: list[dict]):
        self._guests = guests
        self.timeout = 15  # 共享客户端的默认超时,不应被 refresh_connection 改写
        self.calls: list[dict] = []

    def list_guest_resources(self, **kwargs):
        self.calls.append(kwargs)
        return self._guests


def test_refresh_timeout_is_passed_not_sticky(monkeypatch):
    client = _TimeoutRecordingClient([_raw()])
    monkeypatch.setattr(svc, "build_client", lambda conn: client)
    assert svc.refresh_connection(SimpleNamespace(id=1, name="pve"), timeout=6) is True
    # 短超时按次透传给这次请求,而不是回写共享客户端的默认超时
    assert client.calls == [{"timeout": 6}]
    assert client.timeout == 15
    # 不带 timeout 的后台循环路径保持零参调用(兼容既有 fake/lambda 签名)
    assert svc.refresh_connection(SimpleNamespace(id=1, name="pve")) is True
    assert client.calls == [{"timeout": 6}, {}]
    assert client.timeout == 15


# ── 虚机销毁后,边车缓存随下一轮成功快照清理(防长跑进程缓慢泄漏) ──


def test_refresh_prunes_deleted_guest_side_caches(monkeypatch):
    """快照里消失的 guest:历史环/最近已知 IP/探测退避条目一并清掉。"""
    _refresh(monkeypatch, [_raw(101)])
    svc.record_guest_ip(1, 101, "10.1.1.5")
    with svc._lock:
        svc._ip_probe_at[(1, 101)] = (False, time.time())
    assert svc.get_guest_metric_history(1, 101)
    assert svc.get_last_known_ip(1, 101) == "10.1.1.5"

    # 下一轮快照里 101 不见了(虚机已销毁),102 仍在
    _refresh(monkeypatch, [_raw(102)])
    assert svc.get_guest_metric_history(1, 101) == []
    assert svc.get_last_known_ip(1, 101) is None
    with svc._lock:
        assert (1, 101) not in svc._ip_probe_at
    # 存活的 guest 不受影响
    assert svc.get_guest_snapshot(1, 102) is not None
    assert svc.get_guest_metric_history(1, 102)


def test_failed_refresh_keeps_side_caches(monkeypatch):
    """PVE 不可达的那轮不做清理(旧数据保留是快照模块的既有语义)。"""
    _refresh(monkeypatch, [_raw(101)])
    svc.record_guest_ip(1, 101, "10.1.1.5")

    class _Boom:
        def list_guest_resources(self):
            raise RuntimeError("pve down")

    monkeypatch.setattr(svc, "build_client", lambda conn: _Boom())
    assert svc.refresh_connection(SimpleNamespace(id=1, name="pve")) is False
    assert svc.get_last_known_ip(1, 101) == "10.1.1.5"
    assert len(svc.get_guest_metric_history(1, 101)) == 1
