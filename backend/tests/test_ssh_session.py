"""ReusedSshSession(巡检 SSH 会话复用)的行为回归测试。

钉住三件事:
  1. ``_open`` 必须解包 connect_device 的 (client, key) 元组——曾把元组当
     client 存进 ``_client``,真机自测才炸出 ``'tuple' object has no attribute
     'exec_command'``(本地测试全部走 mock,不连真机就发现不了);
  2. 一轮内只握手一次:N 条命令共用同一条连接,连接被对端回收时重建一次;
  3. 重建路径上的 HostKeyMismatchError 原样上抛(MITM 嫌疑不许被吞)。
"""

from app.services.ssh_session import ReusedSshSession


class _FakeClient:
    def __init__(self, fail_exec_once: bool = False):
        self.exec_calls = 0
        self.closed = False
        self.fail_exec_once = fail_exec_once

    def get_transport(self):
        class _T:
            def __init__(self, alive: bool):
                self._alive = alive

            def is_active(self):
                return self._alive

        return _T(alive=not self.closed)

    def exec_command(self, command, timeout=15):
        self.exec_calls += 1
        if self.fail_exec_once and self.exec_calls == 1:
            raise OSError("connection reset by peer")
        return None, _Chan("ok"), _Chan("")

    def close(self):
        self.closed = True


class _Chan:
    def __init__(self, text: str):
        self._text = text
        self._sent = False
        self.channel = _StatusChannel()

    def read(self, n):
        if self._sent:
            return ""
        self._sent = True
        return self._text.encode()


class _StatusChannel:
    @staticmethod
    def recv_exit_status():
        return 0


class _Dev:
    """connect_device 的最小 device 形状(AgentTarget 等价)。"""

    name = "fake"
    ip_address = "127.0.0.1"
    ssh_port = 22
    ssh_host_key = None


def test_open_unpacks_client_from_connect_device_tuple(monkeypatch):
    """connect_device 返回 (client, key);_open 必须只留 client。

    旧 bug:元组整个存进 _client → exec_ssh_command 对元组调 .exec_command
    → AttributeError,每项命令必失败。
    """
    opened = []

    def fake_connect_device(
        device, username, password, *, db=None, timeout=10, private_key=None
    ):
        client = _FakeClient()
        opened.append(client)
        return client, "key-b64"  # 元组!

    monkeypatch.setattr("app.services.ssh.connect_device", fake_connect_device)

    session = ReusedSshSession()
    code, out, err = session.exec(None, _Dev(), "u", "p", "", "hostname", 15)
    assert code == 0 and out == "ok"
    assert session._client is opened[0]  # 存的是 client 本体,不是元组


def test_reuses_one_connection_across_commands(monkeypatch):
    """N 条命令只握手一次;连接死亡后的下一条命令重建一次。"""
    opens = []

    def fake_connect_device(
        device, username, password, *, db=None, timeout=10, private_key=None
    ):
        # 第 2 条命令时第 1 个 client 已被标记 closed(模拟对端回收)
        client = _FakeClient()
        opens.append(client)
        return client, "key-b64"

    monkeypatch.setattr("app.services.ssh.connect_device", fake_connect_device)

    session = ReusedSshSession()
    session.exec(None, _Dev(), "u", "p", "", "cmd-1", 15)
    assert len(opens) == 1  # 一轮一次握手
    session.exec(None, _Dev(), "u", "p", "", "cmd-2", 15)
    assert len(opens) == 1  # 连接活着 → 仍复用

    opens[0].closed = True  # 对端回收
    session.exec(None, _Dev(), "u", "p", "", "cmd-3", 15)
    assert len(opens) == 2  # 重建一次
    assert opens[0].exec_calls == 2  # 旧连接不再收命令


def test_reconnects_once_when_exec_fails(monkeypatch):
    """命令执行中途连接断开:丢弃缓存、重建后重试一次,命令不失败。"""
    opens = []

    def fake_connect_device(
        device, username, password, *, db=None, timeout=10, private_key=None
    ):
        # 第一个连接首次 exec 即断(模拟连接死亡),重试新建的连接正常
        client = _FakeClient(fail_exec_once=len(opens) == 0)
        opens.append(client)
        return client, "key-b64"

    monkeypatch.setattr("app.services.ssh.connect_device", fake_connect_device)

    session = ReusedSshSession()
    # 第一条命令先炸一次再重试:总效果=成功,且开了第二条连接
    code, out, err = session.exec(None, _Dev(), "u", "p", "", "cmd", 15)
    assert code == 0 and out == "ok"
    assert len(opens) == 2
    assert opens[0].exec_calls == 1 and opens[1].exec_calls == 1


def test_host_key_mismatch_propagates_from_open(monkeypatch):
    """MITM 嫌疑(HostKeyMismatchError)必须原样上抛,不许吞。"""

    def fake_connect_device(
        device, username, password, *, db=None, timeout=10, private_key=None
    ):
        from app.services.ssh import HostKeyMismatchError

        raise HostKeyMismatchError("mismatch")

    monkeypatch.setattr("app.services.ssh.connect_device", fake_connect_device)

    session = ReusedSshSession()
    from app.services.ssh import HostKeyMismatchError

    try:
        session.exec(None, _Dev(), "u", "p", "", "cmd", 15)
    except HostKeyMismatchError:
        pass  # 预期:原样上抛
    else:
        raise AssertionError("HostKeyMismatchError must propagate")
    assert session._client is None  # 失败连接不残留


def test_close_is_idempotent():
    session = ReusedSshSession()
    session.close()  # 未开过连接 → 不炸
    client = _FakeClient()
    session._client = client
    session.close()
    session.close()  # 重复 close → 幂等
    assert client.closed
    assert session._client is None
