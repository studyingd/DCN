"""SSH 传输层复用池。

为什么需要它：文件管理器的每一个动作（列目录、上传、下载、进目录）都独立走一次
完整 SSH 握手。实测单次握手 ~164ms、``open_sftp`` ~64ms，于是"打开弹窗 + 上传一个
4KB 文件 + 刷新列表"要付三次握手 ≈ 0.8s——传输本身几乎不花时间，用户看到的就是
"小文件上传非常慢"。

复用的是 **Transport（SSHClient）**，不是 SFTPClient：paramiko 的一条 transport 可以
并发承载多个 channel，而单个 SFTPClient 不是线程安全的。因此每次借出时新开一个
SFTP channel（只是一次 channel open，约一个 RTT），用完关掉，transport 留在池里。

安全边界：
* 池键包含凭据指纹。设备改了密码/密钥就不会命中旧连接，旧条目按 TTL 淘汰。
* 借出前用 ``transport.is_active()`` 校验存活；对端关掉空闲连接时自动重连一次。
* 有 TTL 与容量上限，避免长期运行后堆积僵尸 transport。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time

logger = logging.getLogger(__name__)

# 空闲多久后淘汰。要比常见 sshd 的 ClientAliveInterval 短，避免借到已被对端
# 关掉的连接；同时又得长到足以覆盖一次文件管理会话里的连续操作。
IDLE_TTL_SECONDS = 120.0
MAX_POOL_SIZE = 32


class _Entry:
    __slots__ = ("client", "last_used", "in_use")

    def __init__(self, client) -> None:
        self.client = client
        self.last_used = time.monotonic()
        # 引用计数:正在传输(如大文件 SFTP)的连接不该被 LRU 驱逐。
        self.in_use = 0


class SshPool:
    """按 (host, port, username, 凭据指纹) 复用 SSH transport。"""

    def __init__(
        self, idle_ttl: float = IDLE_TTL_SECONDS, max_size: int = MAX_POOL_SIZE
    ):
        self._idle_ttl = idle_ttl
        self._max_size = max_size
        self._entries: dict[tuple, _Entry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def credential_fingerprint(password: str | None, private_key: str | None) -> str:
        """凭据指纹：只取摘要，绝不在内存里留明文副本作为字典键。"""
        material = f"{password or ''}\x00{private_key or ''}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def key(
        self,
        host: str,
        port: int,
        username: str,
        password: str | None,
        private_key: str | None,
    ) -> tuple:
        return (
            host,
            int(port or 22),
            username,
            self.credential_fingerprint(password, private_key),
        )

    def _evict_expired_locked(self) -> None:
        """清掉过期条目；调用方必须已持有锁。"""
        now = time.monotonic()
        stale = [
            k for k, e in self._entries.items() if now - e.last_used > self._idle_ttl
        ]
        for k in stale:
            entry = self._entries.pop(k, None)
            if entry is None:
                continue
            try:
                entry.client.close()
            except Exception:
                logger.debug("关闭过期 SSH 连接失败", exc_info=True)

    def _evict_oldest_locked(self) -> None:
        """LRU 驱逐,但跳过正在使用的连接(in_use > 0)——慢速大文件传输的
        Transport 不应因「最久未 touch」被掐断。"""
        while len(self._entries) >= self._max_size:
            candidates = [(k, e) for k, e in self._entries.items() if e.in_use == 0]
            if not candidates:
                # 全部在用:不驱逐,让新连接直接新建(不缓存)。
                return
            oldest = min(candidates, key=lambda kv: kv[1].last_used, default=None)
            if oldest is None:
                return
            entry = self._entries.pop(oldest[0], None)
            if entry is None:
                return
            try:
                entry.client.close()
            except Exception:
                logger.debug("淘汰 SSH 连接失败", exc_info=True)

    def acquire(self, key: tuple, factory):
        """取一条存活的 transport；没有就调用 ``factory()`` 新建并缓存。

        ``factory`` 由调用方提供（通常是 ``connect_device``），这样池本身不依赖
        ORM 对象，便于用假工厂做单测。
        """
        with self._lock:
            self._evict_expired_locked()
            entry = self._entries.get(key)
            if entry is not None and _transport_alive(entry.client):
                entry.last_used = time.monotonic()
                return entry.client, False
            if entry is not None:
                # 对端关掉了空闲连接：丢弃后重建。
                self._entries.pop(key, None)
                try:
                    entry.client.close()
                except Exception:
                    pass
            self._evict_oldest_locked()

        # 握手放在锁外：它可能耗时数百毫秒甚至几十秒，持锁会把其它目标的
        # 借出请求一起堵死。
        client = factory()
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None and _transport_alive(existing.client):
                # 并发场景下别人已经建好了：用先到的那条，关掉自己这条。
                try:
                    client.close()
                except Exception:
                    pass
                existing.last_used = time.monotonic()
                return existing.client, False
            self._entries[key] = _Entry(client)
            return client, True

    def mark_in_use(self, key: tuple) -> None:
        """标记一条连接正在使用中(如大文件传输),阻止 LRU 驱逐。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.in_use += 1

    def release(self, key: tuple) -> None:
        """传输完成后归还引用计数;配对 ``mark_in_use``。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.in_use > 0:
                entry.in_use -= 1

    def touch(self, key: tuple) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.last_used = time.monotonic()

    def discard(self, key: tuple) -> None:
        """显式作废一条连接（例如认证信息已变更或传输中途出错）。"""
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is not None:
            try:
                entry.client.close()
            except Exception:
                logger.debug("作废 SSH 连接失败", exc_info=True)

    def close_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            try:
                entry.client.close()
            except Exception:
                logger.debug("关闭 SSH 连接失败", exc_info=True)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _transport_alive(client) -> bool:
    try:
        transport = client.get_transport()
    except Exception:
        return False
    return bool(transport is not None and transport.is_active())


# 进程级单例：文件接口是短请求，跨请求复用才有意义。
ssh_pool = SshPool()
