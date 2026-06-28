import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

import app.models  # noqa: F401 — ensure all models registered with Base.metadata
from app.config import AUTO_CREATE_TABLES, CORS_ORIGINS, RATE_LIMIT_ENABLED
from app.database import Base, engine

# ── 统一错误处理 ──
from app.exceptions import AppError
from app.middleware.error_handler import (
    app_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.middleware.trace import TraceMiddleware
from app.routers import (
    audit,
    auth,
    connections,
    credentials,
    dashboard,
    devices,
    inspection,
    monitor,
    racks,
    roles,
    rooms,
    scheduled_tasks,
    scripts,
    terminal,
    users,
)
from app.services.monitor import run_monitor_loop
from app.services.permissions import PERMISSION_MIGRATION
from app.services.scheduler import run_scheduler_loop

logger = logging.getLogger(__name__)


def _migrate_permissions():

    from app.models.role import Role

    with engine.connect() as conn:
        rows = conn.execute(Role.__table__.select()).fetchall()
        for row in rows:
            try:
                old_perms = json.loads(row.permissions) if row.permissions else []
            except (json.JsonDecodeError, TypeError):
                old_perms = []
            new_perms = list(
                dict.fromkeys(PERMISSION_MIGRATION.get(p, p) for p in old_perms)
            )
            if new_perms != old_perms:
                conn.execute(
                    Role.__table__.update()
                    .where(Role.__table__.c.id == row.id)
                    .values(permissions=json.dumps(new_perms))
                )

        # Ensure roles with script:manage also get inspection:manage
        for row in rows:
            try:
                perms = json.loads(row.permissions) if row.permissions else []
            except (json.JsonDecodeError, TypeError):
                perms = []
            if "script:manage" in perms and "inspection:manage" not in perms:
                perms.append("inspection:manage")
                conn.execute(
                    Role.__table__.update()
                    .where(Role.__table__.c.id == row.id)
                    .values(permissions=json.dumps(perms))
                )

        # Ensure roles with device:remote also get script:manage
        for row in rows:
            try:
                perms = json.loads(row.permissions) if row.permissions else []
            except (json.JSONDecodeError, TypeError):
                perms = []
            if "device:remote" in perms and "script:manage" not in perms:
                perms.append("script:manage")
                conn.execute(
                    Role.__table__.update()
                    .where(Role.__table__.c.id == row.id)
                    .values(permissions=json.dumps(perms))
                )

        conn.commit()


async def _token_blacklist_cleanup_loop():
    """Periodically remove expired tokens from the blacklist table."""
    while True:
        try:
            from app.database import SessionLocal
            from app.services import token_blacklist

            db = SessionLocal()
            try:
                token_blacklist.cleanup_expired(db)
            finally:
                db.close()
        except Exception:
            logger.exception("Token blacklist cleanup cycle failed")
        await asyncio.sleep(3600)  # Run every hour


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动：可选自动建表（仅开发），并执行权限数据迁移。

    生产环境的 schema 由 Alembic 管理（``alembic upgrade head``），不在启动期
    自动建表。仅在显式设置 ``DCN_AUTO_CREATE=true`` 时才执行 create_all。
    """
    if AUTO_CREATE_TABLES:
        Base.metadata.create_all(bind=engine)
    _migrate_permissions()

    monitor_task = asyncio.create_task(run_monitor_loop())
    scheduler_task = asyncio.create_task(run_scheduler_loop())
    token_cleanup_task = asyncio.create_task(_token_blacklist_cleanup_loop())

    yield

    for task in (monitor_task, scheduler_task, token_cleanup_task):
        task.cancel()
    for task in (monitor_task, scheduler_task, token_cleanup_task):
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="DCN Visualization API",
    description="数据中心网络可视化系统后端 API",
    version="1.0.0",
    lifespan=lifespan,
)

# ── 中间件注册（执行顺序：最后注册的最先执行）──

# 1. CORS — 最外层，处理预检请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Security Headers — 为所有响应添加安全头
app.add_middleware(SecurityHeadersMiddleware)

# 3. Trace — 为每个请求分配唯一 traceId + 记录请求日志
app.add_middleware(TraceMiddleware)

# 4. 速率限制（可通过 RATE_LIMIT_ENABLED=false 关闭）
if RATE_LIMIT_ENABLED:
    from app.middleware.rate_limiter import setup_rate_limiter

    setup_rate_limiter(app)

# ── 统一异常处理器 ──
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)

# ── 注册路由 ──
app.include_router(auth.router)
app.include_router(rooms.router)
app.include_router(racks.router)
app.include_router(devices.router)
app.include_router(connections.router)
app.include_router(terminal.router)
app.include_router(monitor.router)
app.include_router(credentials.router)
app.include_router(audit.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(scripts.router)
app.include_router(scheduled_tasks.router)
app.include_router(dashboard.router)
app.include_router(inspection.router)


@app.get("/api/health")
def health_check():
    """健康检查接口"""
    return {"status": "ok"}
