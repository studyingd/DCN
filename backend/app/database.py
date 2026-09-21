import os
from datetime import datetime, timezone

from sqlalchemy import DateTime, TypeDecorator, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL

_cpu_count = os.cpu_count() or 4

# The application supports MySQL 8 only. Keep every connection pinned to UTC.
_connect_args = {"init_command": "SET time_zone = '+00:00'"}

engine = create_engine(
    DATABASE_URL,
    pool_size=min(20, _cpu_count * 2 + 2),
    max_overflow=10,
    pool_recycle=3600,
    pool_pre_ping=True,
    connect_args=_connect_args,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """``DATETIME`` 列的读写都按 UTC 处理。

    MySQL 的 ``DATETIME`` 不保存时区，而连接又固定在 ``+00:00``，所以库里存的其实是
    UTC 墙钟时间，读出来是不带 tzinfo 的 naive 值。naive 值一路传到 API 就成了
    ``2026-09-08T09:02:15``——前端 ``new Date()`` 会按浏览器本地时间解析，展示出来
    慢 8 小时;飞书卡片同理。

    这个类型在写入时剥掉 tzinfo(统一转成 UTC 墙钟)，读出时补回 ``timezone.utc``，
    于是所有出站时间都自带显式偏移，客户端可以无歧义地换算成本地时区。DDL 仍然是
    普通 ``DATETIME``，不需要迁移。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None or value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
