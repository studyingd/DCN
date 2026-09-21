import asyncio
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from starlette.requests import Request

import app.models  # noqa: F401 — ensure all models registered with Base.metadata
from app.config import (
    AGENT_RUN_RETENTION_DAYS,
    ALERT_EVENT_RETENTION_DAYS,
    AUTO_CREATE_TABLES,
    AUTOMATION_JOB_RETENTION_DAYS,
    BACKGROUND_TASKS_ENABLED,
    CONTAINERS_ENABLED,
    CORS_ORIGINS,
    GUEST_FS_METRICS_ENABLED,
    GUEST_IP_REFRESH_ENABLED,
    INSPECTION_RETENTION_DAYS,
    INTERFACE_PROBE_ENABLED,
    LOG_LEVEL,
    METRICS_ENABLED,
    PVE_STATUS_ENABLED,
    RATE_LIMIT_ENABLED,
)
from app.database import Base, engine

# ── 统一错误处理 ──
from app.exceptions import AppError
from app.logging_config import setup_logging
from app.middleware.error_handler import (
    app_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.middleware.trace import TraceMiddleware
from app.routers import (
    agent,
    alerts,
    auth,
    automation,
    businesses,
    containers,
    dashboard,
    devices,
    files,
    inspection,
    metrics,
    monitor,
    pve,
    pve_console,
    racks,
    roles,
    rooms,
    scripts,
    search,
    terminal,
    users,
    webhooks,
)
from app.services.agent import recover_interrupted_runs
from app.services.alerts import (
    cleanup_old_alert_events,
    close_orphaned_alert_events,
    run_business_alert_loop,
)
from app.services.auth import decode_token, security
from app.services.automation import recover_interrupted_jobs
from app.services.background_leader import BackgroundLeader
from app.services.containers_collector import run_containers_loop
from app.services.db_bootstrap import bootstrap_database
from app.services.guest_fs_metrics import run_guest_fs_metrics_loop
from app.services.interface_prober import run_interface_probe_loop
from app.services.metrics_collector import run_metrics_loop
from app.services.monitor import run_monitor_loop
from app.services.pve_guest_status import (
    run_pve_guest_ip_refresh_loop,
    run_pve_guest_status_loop,
)
from app.services.remediation import (
    recover_orphaned_analyses,
    sweep_expired_notification_holds,
)
from app.services.scheduler import run_scheduler_loop

# 必须早于任何 app.* 的日志输出配置好 root logger:uvicorn 从不配置 root，
# 否则 INFO 级日志会被 logging.lastResort 全部丢弃(见 app/logging_config.py)。
setup_logging(LOG_LEVEL)

logger = logging.getLogger(__name__)

# backend/ 目录(alembic.ini 所在)
BACKEND_DIR = Path(__file__).resolve().parent.parent


def _runtime_database_url() -> str:
    """正常模式下的 DATABASE_URL(安装模式不会被调到这里)。"""
    from app.config import DATABASE_URL

    assert DATABASE_URL is not None
    return DATABASE_URL


def _run_init_db() -> None:
    """在迁移完成后跑 init_db 的种子逻辑(幂等,已存在则跳过)。"""
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "init_db.py"],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-1000:]
        raise RuntimeError(f"数据库初始化(init_db)失败:\n{tail}")
    for line in (result.stdout or "").splitlines():
        if line.strip():
            logger.info("init_db: %s", line.strip())


async def _token_blacklist_cleanup_loop():
    """Periodically remove expired tokens from the blacklist table, and purge old alert events."""
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
        try:
            # 已解决的告警事件按保留期清理,防止表无限膨胀拖慢告警中心。
            cleanup_old_alert_events(ALERT_EVENT_RETENTION_DAYS)
        except Exception:
            logger.exception("Alert event cleanup cycle failed")
        # 运行数据(诊断/任务/巡检)保留期清理:默认 0=关闭,显式配置才生效;
        # 只删终态数据,agent_runs 跳过仍被告警事件引用的行。
        try:
            from app.services.data_retention import (
                cleanup_old_agent_runs,
                cleanup_old_automation_jobs,
                cleanup_old_inspection_records,
            )

            cleanup_old_agent_runs(AGENT_RUN_RETENTION_DAYS)
            cleanup_old_automation_jobs(AUTOMATION_JOB_RETENTION_DAYS)
            cleanup_old_inspection_records(INSPECTION_RETENTION_DAYS)
        except Exception:
            logger.exception("Run data retention cleanup cycle failed")
        await asyncio.sleep(3600)  # Run every hour


async def _maintenance_summary_loop():
    """静默窗口结束后的汇总卡:每 30s 扫描已结束且未汇总的窗口。

    汇总要及时(窗口一结束用户就该知道静默期有没有真出事),
    不能等每小时的清理循环。
    """
    from app.services.alerts import sweep_maintenance_summaries

    while True:
        try:
            await asyncio.to_thread(sweep_maintenance_summaries)
        except Exception:
            logger.exception("Maintenance summary cycle failed")
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动:先自动建库+迁移(幂等),再做恢复/迁移数据。

    生产环境的 schema 由 Alembic 管理;仅在显式设置 ``DCN_AUTO_CREATE=true``
    时才额外执行 create_all(开发便利)。
    """
    # 先自动建库+迁移(幂等),失败即拒启动,不带着「半拉子 schema」接受流量。
    await asyncio.to_thread(bootstrap_database, _runtime_database_url())
    await asyncio.to_thread(_run_init_db)
    if AUTO_CREATE_TABLES:
        Base.metadata.create_all(bind=engine)
    try:
        recovered = recover_interrupted_jobs()
        if recovered:
            logger.warning(
                "Recovered %d interrupted automation jobs after restart", recovered
            )
    except Exception:
        logger.exception("Failed to recover interrupted automation jobs")
    try:
        recovered_agent = recover_interrupted_runs()
        if recovered_agent:
            logger.warning(
                "Recovered %d interrupted Agent runs after restart", recovered_agent
            )
    except Exception:
        logger.exception("Failed to recover interrupted Agent runs")
    try:
        # 必须在 recover_interrupted_runs 之后:AgentRun 先落到终态，
        # 卡在"AI 归因中"的告警事件才能被识别成孤儿并收敛。
        recovered_analysis = recover_orphaned_analyses()
        if recovered_analysis:
            logger.warning(
                "Recovered %d orphaned alert analyses after restart", recovered_analysis
            )
    except Exception:
        logger.exception("Failed to recover orphaned alert analyses")
    try:
        # 放行重启前被扣住、等归因结论的告警通知，否则这些告警会永远发不出去。
        released = sweep_expired_notification_holds()
        if released:
            logger.warning(
                "Released %d held alert notifications after restart", released
            )
    except Exception:
        logger.exception("Failed to release held alert notifications")
    try:
        # 收尾失去评估来源的告警事件:规则被停用/删除、对象被移出规则范围或整行删掉
        # 之后，这些事件靠自己永远不会恢复。之后每个采集周期还会再跑一遍。
        closed = close_orphaned_alert_events()
        if closed:
            logger.warning("Closed %d orphaned alert events after restart", closed)
    except Exception:
        logger.exception("Failed to close orphaned alert events")
    try:
        # 把存量 webhook 的旧事件名（inspection.report / inspection.failed /
        # job.failed / device.offline / device.online）归一化到 automation.notify。
        # 幂等，重复启动无副作用；不做的话老订阅会指向已不存在的事件。
        from app.database import SessionLocal
        from app.services.device_events import migrate_event_names

        db = SessionLocal()
        try:
            migrated = migrate_event_names(db)
        finally:
            db.close()
        if migrated:
            logger.warning("Migrated webhook event names on %d webhook(s)", migrated)
    except Exception:
        logger.exception("Failed to migrate webhook event names")
    leader = BackgroundLeader()
    is_leader = await leader.acquire() if BACKGROUND_TASKS_ENABLED else False
    if BACKGROUND_TASKS_ENABLED and not is_leader:
        logger.info(
            "Background tasks disabled in this worker: another instance holds the leader lock"
        )
    monitor_task = (
        asyncio.create_task(run_monitor_loop())
        if BACKGROUND_TASKS_ENABLED and is_leader
        else None
    )
    scheduler_task = (
        asyncio.create_task(run_scheduler_loop())
        if BACKGROUND_TASKS_ENABLED and is_leader
        else None
    )
    token_cleanup_task = (
        asyncio.create_task(_token_blacklist_cleanup_loop())
        if BACKGROUND_TASKS_ENABLED and is_leader
        else None
    )
    maintenance_summary_task = (
        asyncio.create_task(_maintenance_summary_loop())
        if BACKGROUND_TASKS_ENABLED and is_leader
        else None
    )
    metrics_task = (
        asyncio.create_task(run_metrics_loop())
        if METRICS_ENABLED and BACKGROUND_TASKS_ENABLED and is_leader
        else None
    )
    containers_task = (
        asyncio.create_task(run_containers_loop())
        if CONTAINERS_ENABLED and BACKGROUND_TASKS_ENABLED and is_leader
        else None
    )
    probe_task = (
        asyncio.create_task(run_interface_probe_loop())
        if INTERFACE_PROBE_ENABLED and BACKGROUND_TASKS_ENABLED and is_leader
        else None
    )
    business_alert_task = (
        asyncio.create_task(run_business_alert_loop())
        if BACKGROUND_TASKS_ENABLED and is_leader
        else None
    )
    pve_status_task = (
        asyncio.create_task(run_pve_guest_status_loop())
        if PVE_STATUS_ENABLED and BACKGROUND_TASKS_ENABLED and is_leader
        else None
    )
    guest_fs_task = (
        asyncio.create_task(run_guest_fs_metrics_loop())
        if GUEST_FS_METRICS_ENABLED and BACKGROUND_TASKS_ENABLED and is_leader
        else None
    )
    guest_ip_task = (
        asyncio.create_task(run_pve_guest_ip_refresh_loop())
        if GUEST_IP_REFRESH_ENABLED and BACKGROUND_TASKS_ENABLED and is_leader
        else None
    )

    yield

    bg_tasks = [
        t
        for t in (
            monitor_task,
            scheduler_task,
            token_cleanup_task,
            maintenance_summary_task,
            metrics_task,
            containers_task,
            probe_task,
            business_alert_task,
            pve_status_task,
            guest_fs_task,
            guest_ip_task,
        )
        if t
    ]
    for task in bg_tasks:
        task.cancel()
    for task in bg_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    await leader.release()


app = FastAPI(
    title="DCN Visualization API",
    description="数据中心网络可视化系统后端 API",
    version="1.0.0",
    lifespan=lifespan,
    # 关闭尾斜杠自动重定向(默认 307 → 无斜杠路径，Location 从请求 Host 头拼出)。
    # 携带恶意 Host: evil.example.com + /api/health/ 会让重定向 Location 变成
    # http://evil.example.com/api/health ——缓存投毒/钓鱼链接素材(越权审计实测)。
    # 关掉后尾斜杠一律 404，注入面直接消失；前端与测试均用精确路径，无兼容问题。
    redirect_slashes=False,
)

# ── 中间件注册 ──
#
# Starlette 的 add_middleware() 是「插到栈顶」：**最后注册的最外层、最先执行**。
# 因此下面的注册顺序与真实执行顺序是反的，实际请求链路为：
#
#   CORS → Trace → SecurityHeaders → RateLimit → 路由
#
# 这个顺序是刻意的：
# - CORS 必须在最外层，预检 OPTIONS 直接短路返回，限流/日志不会把预检算进去；
#   更重要的是 429 这类由中间件直接产出的响应也要带上 Access-Control-Allow-Origin，
#   否则跨域前端看到的是 CORS 报错而不是真实的 429。
# - Trace 在限流之外，先分配 traceId，429 响应体里才不是 "unknown"，
#   同时被限流挡掉的请求也会留下一条访问日志（排查刷接口时必需）。
# - SecurityHeaders 在限流之外，中间件直接 return 的 429 同样带上安全响应头。
# - RateLimit 放在最内层（紧贴路由）：卸流量的效果不变，但上面三层的响应
#   加工都能覆盖到它。

# 1) 速率限制（最内层；可通过 RATE_LIMIT_ENABLED=false 关闭）
if RATE_LIMIT_ENABLED:
    from app.middleware.rate_limiter import setup_rate_limiter

    setup_rate_limiter(app)

# 2) Security Headers — 为所有响应添加安全头
app.add_middleware(SecurityHeadersMiddleware)

# 3) Trace — 为每个请求分配唯一 traceId + 记录请求日志
app.add_middleware(TraceMiddleware)

# 4) CORS — 最外层，处理预检请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 统一异常处理器 ──
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)

# ── 注册路由 ──
app.include_router(auth.router)
app.include_router(rooms.router)
app.include_router(racks.router)
app.include_router(devices.router)
app.include_router(files.router)
app.include_router(terminal.router)
app.include_router(monitor.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(scripts.router)
app.include_router(dashboard.router)
app.include_router(inspection.router)
app.include_router(metrics.router)
app.include_router(agent.router)
app.include_router(alerts.router)
app.include_router(automation.router)
app.include_router(webhooks.router)
app.include_router(containers.router)
app.include_router(businesses.router)
app.include_router(pve.router)
app.include_router(pve_console.router)
app.include_router(search.router)


@app.get("/api/health")
async def health_check(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Readiness check: database connectivity and schema migration currency.

    未认证时只返回 {"status": "ok"}(存活探针用，compose HEALTHCHECK 只看 200)；
    DB/迁移状态属于内部信息，仅登录后可见——匿名泄露"database: ready"
    给攻击者额外的后端情报(越权审计)。这里不用 get_current_user：它对
    无凭据/凭据失效直接 401，而本端点匿名必须 200。

    不再手工维护「必须具备的表/列」清单(每次迁移都要记得回来改,漏一次就
    带着半拉子 schema 接流量)——直接对比 alembic_version 与迁移脚本的 head,
    永远和迁移链保持同步。
    """
    token = request.cookies.get("dcn_access")
    if not token and credentials is not None:
        token = credentials.credentials
    authenticated = False
    if token:
        try:
            payload = decode_token(token)
            authenticated = payload.get("type") == "access"
        except (jwt.PyJWTError, ValueError):
            authenticated = False
    if not authenticated:
        return {"status": "ok"}
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    try:
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        heads = set(ScriptDirectory.from_config(cfg).get_heads())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            current = {
                row[0]
                for row in conn.execute(text("SELECT version_num FROM alembic_version"))
            }
        if current != heads:
            raise RuntimeError(
                f"schema revision mismatch: db={sorted(current)} head={sorted(heads)}"
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Readiness check failed: %s", exc)
        raise HTTPException(
            status_code=503, detail="database/schema not ready"
        ) from exc
    return {"status": "ok", "database": "ready"}


@app.get("/api/health/live")
def liveness_check():
    return {"status": "ok"}
