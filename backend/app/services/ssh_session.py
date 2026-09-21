"""巡检 SSH 会话复用。

实测部分目标机(CentOS 7 / OpenSSH 7.4)paramiko 每次新建连接的认证阶段要
5~10s,而单条命令本身只要 0.1s 左右;一轮 core 巡检要跑 6 条命令,逐条重连
会把每台设备白耗 30~60s 纯握手。本模块在一轮执行内复用同一条 transport,
把 N 次握手压成 1 次;连接在两条命令之间被对端回收时,丢弃缓存重建后重试
一次。

agent._SshSession(诊断链路,告警归因共用)保持不动,两边稳定后再评估合一;
本模块是它对巡检侧的独立等价实现,行为口径一致:

  * device 兼容 ORM Device(connect_device 负责 TOFU host key 回写)与
    PVE 运行时目标(AgentTarget 等,connect_device 检测到无持久 id 时自动
    跳过回写,仅做 pinned key 校验);
  * HostKeyMismatchError 一律原样抛出——MITM 嫌疑必须让调用方如实上报,
    绝不在重建路径里被吞掉;
  * 单目标内命令串行执行,``_open_lock`` 仅作防御(将来并行复用时安全)。
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class ReusedSshSession:
    """一次巡检内复用的 SSH 连接。"""

    def __init__(self) -> None:
        self._client = None
        self._open_lock = threading.Lock()

    @staticmethod
    def _alive(client) -> bool:
        try:
            transport = client.get_transport()
            return transport is not None and transport.is_active()
        except Exception:
            return False

    def _open(
        self, db, device, username: str, password: str, ssh_key: str, timeout: int
    ):
        from app.services.ssh import connect_device

        client, _key = connect_device(
            device,
            username,
            password,
            db=db,
            timeout=timeout,
            private_key=ssh_key or None,
        )
        return client

    def exec(
        self,
        db,
        device,
        username: str,
        password: str,
        ssh_key: str,
        command: str,
        timeout: int,
    ) -> tuple[int, str, str]:
        """执行一条命令;连接死亡时丢弃缓存重建重试一次。"""
        from app.services.ssh import exec_ssh_command

        if self._client is not None and not self._alive(self._client):
            self.close()
        if self._client is None:
            with self._open_lock:
                if self._client is None:
                    self._client = self._open(
                        db, device, username, password, ssh_key, timeout
                    )
        try:
            return exec_ssh_command(self._client, command, timeout=timeout)
        except Exception:
            # 连接可能在两条命令之间被对端回收(或命令超时后通道状态不可知):
            # 丢弃缓存、重建后重试一次。重建时 host key 变化会抛
            # HostKeyMismatchError 并原样上抛,不会静默吞掉 MITM 告警。
            self.close()
            self._client = self._open(db, device, username, password, ssh_key, timeout)
            return exec_ssh_command(self._client, command, timeout=timeout)

    def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.close()
        except Exception:
            logger.debug("close reused SSH client failed", exc_info=True)
