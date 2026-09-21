"""数据库自举:首次启动时自动建库 + 跑 Alembic 迁移。

设计边界:
  * **幂等**——每次正常启动都会跑;``alembic upgrade head`` 对已是最新的库是空操作,
    ``CREATE DATABASE IF NOT EXISTS`` 同理。
  * **失败即拒启动**——库不可达/迁移失败直接抛异常让进程退出,绝不带着
    「半拉子 schema」继续跑(那比 crash 更难排查)。
  * **不动 Docker 流程**——compose 的 entrypoint 已做同样两步且幂等,
    这里再做一遍只是毫秒级的 no-op,行为完全一致。

依赖要求:连接账户需要建库权限(``CREATE DATABASE``)。权限不足时引导用户
手工建库后重试。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)


def ensure_database_exists(database_url: str) -> None:
    """若目标库不存在则创建(utf8mb4)。连接信息从 DATABASE_URL 解析。

    实现方式:连到 MySQL 服务器级(不带库名)执行 ``CREATE DATABASE IF NOT EXISTS``。
    权限不足(应用账户通常只对应用库有权限)时记 warning 并跳过——让后续
    迁移连接给出更真实的报错。
    """
    parsed = urlsplit(database_url)
    if parsed.scheme.split("+")[0] != "mysql":
        raise RuntimeError(
            f"不支持的数据库类型 {parsed.scheme!r},仅支持 MySQL(mysql+pymysql://)"
        )
    database = (parsed.path or "").lstrip("/")
    if not database:
        raise RuntimeError("DATABASE_URL 缺少数据库名(路径部分)")
    try:
        import pymysql

        conn = pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            charset="utf8mb4",
            connect_timeout=10,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            conn.commit()
            logger.info("Database `%s` ensured (created if missing)", database)
        finally:
            conn.close()
    except Exception as exc:
        # 常见:应用账户没有 CREATE 权限但库已存在 —— 不影响后续迁移。
        logger.warning(
            "ensure_database_exists skipped (%s: %s); 若库不存在请先手工创建",
            type(exc).__name__,
            str(exc)[:200],
        )


def run_migrations() -> None:
    """以子进程跑 ``alembic upgrade head``(从 backend/ 目录,读 alembic.ini)。

    用子进程而不是 alembic API:迁移环境与当前进程完全隔离(env.py 会重读
    配置/注册全部模型),且 stdout/stderr 直接进主进程日志,失败码可判。
    """
    backend_dir = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-1500:]
        raise RuntimeError(f"数据库迁移失败(alembic exit={result.returncode}):\n{tail}")
    for line in (result.stdout or "").splitlines():
        if line.strip():
            logger.info("alembic: %s", line.strip())


def bootstrap_database(database_url: str) -> None:
    """正常模式启动入口:确保库存在 → 迁移到 head。任一步失败即抛异常。"""
    ensure_database_exists(database_url)
    run_migrations()
