"""Token 黑名单服务 — 内存缓存 + 数据库双层存储"""

import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.token_blacklist import TokenBlacklist

logger = logging.getLogger(__name__)

# 内存缓存: jti -> expires_at (Unix timestamp)
_cache: dict[str, float] = {}
_MAX_CACHE_SIZE = 10_000


def is_blacklisted(jti: str, db: Session) -> bool:
    """检查 token 是否已被撤销"""
    now = time.time()

    # 第一级：查内存缓存
    if jti in _cache:
        if _cache[jti] > now:
            return True
        del _cache[jti]

    # 第二级：查数据库
    row = (
        db.query(TokenBlacklist)
        .filter(
            TokenBlacklist.token_jti == jti,
            TokenBlacklist.expires_at > datetime.now(timezone.utc),
        )
        .first()
    )
    if row:
        _cache[jti] = row.expires_at.timestamp()
        return True
    return False


def add_to_blacklist(
    jti: str, user_id: int | None, reason: str, expires_at: datetime, db: Session
) -> None:
    """将 token 加入黑名单"""
    entry = TokenBlacklist(
        token_jti=jti,
        user_id=user_id,
        reason=reason,
        expires_at=expires_at,
    )
    db.add(entry)
    db.commit()
    _cache[jti] = expires_at.timestamp()

    if len(_cache) > _MAX_CACHE_SIZE:
        _evict_expired()


def cleanup_expired(db: Session) -> int:
    """清理过期的黑名单记录"""
    now = datetime.now(timezone.utc)
    result = db.query(TokenBlacklist).filter(TokenBlacklist.expires_at < now).delete()
    db.commit()
    t = time.time()
    expired_keys = [k for k, v in _cache.items() if v <= t]
    for k in expired_keys:
        del _cache[k]
    return result


def _evict_expired():
    """缓存满时淘汰过期条目"""
    t = time.time()
    expired = [k for k, v in _cache.items() if v <= t]
    for k in expired:
        del _cache[k]
    if len(_cache) > _MAX_CACHE_SIZE:
        sorted_items = sorted(_cache.items(), key=lambda x: x[1])
        for k, _ in sorted_items[: len(sorted_items) // 2]:
            del _cache[k]
