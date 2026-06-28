import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL

_cpu_count = os.cpu_count() or 4

# `init_command` is a PyMySQL/MySQLdb parameter; sqlite does not accept it.
# Gate it on the URL scheme so the app (and tests) can run against sqlite too.
_is_sqlite = DATABASE_URL.startswith("sqlite")
_connect_args: dict = (
    {}
    if _is_sqlite
    else {"init_command": "SET time_zone = '+00:00'"}
)

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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
