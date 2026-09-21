import os
import secrets
from pathlib import Path

# 项目根目录 (backend/app/config.py → backend/ → project_root/)
BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
PROJECT_ROOT = BASE_DIR.parent  # project root (contains .env)

# 从项目根目录加载 .env 文件
_ENV_FILE = PROJECT_ROOT / ".env"
if _ENV_FILE.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_ENV_FILE)
    except ImportError:
        pass  # python-dotenv not installed; env vars must be set manually


# ── Helpers ──────────────────────────────────────────────────────────
def _read_file_secret(path: str) -> str:
    """Read a secret value from a file (Docker secrets / mounted volume)."""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def _secret_from_file_or_env(key: str) -> str | None:
    """Read a secret from ${key}_FILE (Docker secret) first, else the env var."""
    file_path = os.getenv(f"{key}_FILE")
    if file_path:
        return _read_file_secret(file_path)
    return os.getenv(key)


def _require_env(key: str) -> str:
    """Read a required env var (or ${key}_FILE) or raise immediately."""
    val = _secret_from_file_or_env(key)
    if val is None:
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Please set it (or {key}_FILE) before starting the application."
        )
    return val


def _secret(key: str) -> str:
    """
    Read a secret from the environment.  If not provided, auto-generate a
    strong random value so that every deployment is secure by default.
    The generated value is *not* persisted — it changes on every restart,
    which invalidates existing tokens.  For production, always set it
    explicitly.  The generated value is deliberately NOT printed.
    """
    val = _secret_from_file_or_env(key)
    if val is None:
        val = secrets.token_hex(32)
        print(
            f"[WARNING] {key} not set — auto-generated a random value for this session. "
            f"Set it explicitly in production to avoid losing tokens on restart."
        )
    return val


def _required_secret(key: str) -> str:
    """
    Read a secret that MUST be set explicitly — auto-generation is disabled
    because losing this value would make existing encrypted data unrecoverable.
    """
    val = _secret_from_file_or_env(key)
    if val is None:
        raise RuntimeError(
            f"Missing required secret: {key}. Set {key} (or {key}_FILE) before "
            f"starting the application."
        )
    return val


def _parse_cors_origins(raw: str) -> list[str]:
    """Parse comma-separated CORS origins; reject '*' (unsafe with credentials)."""
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if "*" in origins:
        raise RuntimeError(
            "CORS_ORIGINS must not contain '*' (allow_credentials is enabled). "
            "List explicit origins."
        )
    return origins


# CORS allowed origins (comma-separated in env; never '*')
CORS_ORIGINS = _parse_cors_origins(
    os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000")
)

# MySQL 数据库。DATABASE_URL 缺失时进程直接拒启动——配置一律走 .env,
# 不再有「先起个最小服务、再在网页上配数据库」的引导模式。
DATABASE_URL = _secret_from_file_or_env("DATABASE_URL") or None
if DATABASE_URL is None:
    raise RuntimeError(
        "DATABASE_URL is required. Copy .env.example to .env and fill in the "
        "database connection before starting."
    )
if not DATABASE_URL.lower().startswith("mysql+pymysql://"):
    raise RuntimeError("DATABASE_URL must use the mysql+pymysql:// MySQL driver")

# JWT 配置 (secret is auto-generated if not set; set explicitly for production)
JWT_SECRET = _secret("JWT_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256").upper()
if JWT_ALGORITHM not in {"HS256", "HS384", "HS512"}:
    raise RuntimeError("JWT_ALGORITHM must be HS256, HS384, or HS512")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

# Cookie 安全：浏览器鉴权使用 httpOnly Cookie。
# 生产环境（HTTPS）请设置 COOKIE_SECURE=true，使 Cookie 仅通过加密通道传输。
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"

# 管理员默认账户 — 首次启动时使用；密码缺失时自动生成并写入文件（不打印明文）
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
_ADMIN_PASSWORD_ENV = _secret_from_file_or_env("ADMIN_PASSWORD")
if _ADMIN_PASSWORD_ENV is None:
    _ADMIN_PASSWORD_ENV = secrets.token_urlsafe(16)
    _init_pw_path = PROJECT_ROOT / "data" / ".admin_initial_password"
    try:
        _init_pw_path.parent.mkdir(parents=True, exist_ok=True)
        _init_pw_path.write_text(_ADMIN_PASSWORD_ENV, encoding="utf-8")
        try:
            os.chmod(_init_pw_path, 0o600)
        except OSError:
            pass  # 非 POSIX 平台（Windows）忽略权限设置
        print(
            f"[WARNING] ADMIN_PASSWORD not set — auto-generated and written to "
            f"{_init_pw_path} (mode 0600). Change it immediately after first login."
        )
    except OSError:
        # 既未设置也无法写文件 —— 不打印明文，运维须通过数据库重置。
        print(
            "[WARNING] ADMIN_PASSWORD not set and unable to write initial password file. "
            "Reset the admin password in the database manually."
        )
ADMIN_PASSWORD = _ADMIN_PASSWORD_ENV

# Guacamole guacd 配置
GUACD_HOST = os.getenv("GUACD_HOST", "localhost")
GUACD_PORT = int(os.getenv("GUACD_PORT", "4822"))
# Only enable the host-side TCP relay when guacd cannot route to the target
# network directly (for example, an isolated Docker network). Direct guacd→RDP
# avoids an extra asyncio proxy hop and materially reduces keyboard/frame lag.
GUACD_USE_RELAY = os.getenv("GUACD_USE_RELAY", "false").lower() == "true"

# 凭证加密密钥 — 必须显式设置（自动生成会导致历史加密凭据不可解密、无法恢复）。
CREDENTIAL_SECRET_KEY = _required_secret("CREDENTIAL_SECRET_KEY")

# RDP 安全策略：negotiate/tls/nls/any；默认 any（与历史一致，生产建议 tls/nls）。
# 证书校验：默认严格（false=校验），仅在受控内网且确有必要时设 true 跳过。
GUAC_RDP_SECURITY = os.getenv("GUAC_RDP_SECURITY", "any")
GUAC_RDP_IGNORE_CERT = os.getenv("GUAC_RDP_IGNORE_CERT", "false").lower() == "true"
# RDP 色深：8/16/24/32 均合法，由 guacamole.py 原样透传给 guacd。
# 8 位(256 色)会让 guacd 走调色板位图编码，guacamole-common-js 对该路径的
# canvas 渲染支持有缺陷——表现为「能看到桌面但画面冻结、完全卡死」，连接与上行
# 事件却正常。docker-compose/.env.example 出于带宽考虑默认 8；若遇到冻结，改为 16/24/32。
GUAC_RDP_COLOR_DEPTH = os.getenv("GUAC_RDP_COLOR_DEPTH", "32")

# 应用日志级别（DEBUG / INFO / WARNING / ERROR / CRITICAL）。
# 必须由应用自己配置:uvicorn 只给 uvicorn / uvicorn.access 装 handler 且
# propagate=False，从不配置 root logger，否则 app.* 的 INFO 日志会全部被
# logging.lastResort 丢弃(详见 app/logging_config.py)。非法值回落 INFO。
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()

# Device monitor configuration
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "30"))
MONITOR_TIMEOUT = int(os.getenv("MONITOR_TIMEOUT", "2"))
MONITOR_CONCURRENCY = int(os.getenv("MONITOR_CONCURRENCY", "20"))

# WinRM（Windows 设备的唯一管理通道——平台不对 Windows 使用 SSH）
# NTLM 认证；5985(HTTP) 有消息级加密，内网可用；5986(HTTPS) 设 WINRM_USE_SSL=true。
# SSL 证书默认校验，仅受控内网自签场景可设 WINRM_CERT_VALIDATION=false。
WINRM_ENABLED = os.getenv("WINRM_ENABLED", "true").lower() == "true"
WINRM_PORT = int(os.getenv("WINRM_PORT", "5985"))
WINRM_USE_SSL = os.getenv("WINRM_USE_SSL", "false").lower() == "true"
WINRM_CERT_VALIDATION = os.getenv("WINRM_CERT_VALIDATION", "true").lower() == "true"

# 服务器指标采集（Linux 走 SSH，Windows 走 WinRM）
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "true").lower() == "true"
METRICS_INTERVAL = int(os.getenv("METRICS_INTERVAL", "60"))
METRICS_CONCURRENCY = int(os.getenv("METRICS_CONCURRENCY", "10"))
METRICS_TIMEOUT = int(os.getenv("METRICS_TIMEOUT", "15"))
METRICS_RETENTION_DAYS = int(os.getenv("METRICS_RETENTION_DAYS", "7"))
# Linux 采集的 SSH transport 复用池上限(每条复用连接 ≈ 1 线程 + 1 socket)。
# 设备数超过上限时池会 LRU 轮转，退化为逐轮握手(与无池时等价)，不报错。
METRICS_SSH_POOL_SIZE = int(os.getenv("METRICS_SSH_POOL_SIZE", "64"))

# Agent 诊断（LLM 只读排障，OpenAI 兼容接口；默认关闭，配置齐全后启用）
# 边界：LLM 只能调用注册表内的只读诊断项（services/agent_commands.py），
# 永远无法接触 shell 自由输入；每次诊断全量落 agent_runs 审计。
AGENT_ENABLED = os.getenv("AGENT_ENABLED", "false").lower() == "true"
AGENT_LLM_BASE_URL = os.getenv("AGENT_LLM_BASE_URL", "").rstrip("/")
AGENT_LLM_API_KEY = os.getenv("AGENT_LLM_API_KEY", "")
AGENT_LLM_MODEL = os.getenv("AGENT_LLM_MODEL", "")
AGENT_LLM_TIMEOUT = int(os.getenv("AGENT_LLM_TIMEOUT", "60"))
# Agent 循环单次 LLM 调用超时(决策轮+报告轮共用):推理型模型生成 3000+
# token 的最终报告实测 55~60s,60s 贴线偶发超时会把"22 步证据全采完"的
# 诊断整个作废(2026-09-15 K3 服务器 run 190),故上限放宽到 180s。
# AGENT_LLM_TIMEOUT 保留为兼容别名,不再被读取。
AGENT_LLM_REPORT_TIMEOUT = int(
    os.getenv("AGENT_LLM_REPORT_TIMEOUT", os.getenv("AGENT_LLM_TIMEOUT", "180"))
)
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "8"))
AGENT_CMD_TIMEOUT = int(os.getenv("AGENT_CMD_TIMEOUT", "20"))
AGENT_MAX_OUTPUT_CHARS = int(os.getenv("AGENT_MAX_OUTPUT_CHARS", "6000"))
# 同时执行的 Agent 诊断运行数上限。每次运行占用一条 SSH/WinRM 连接与一个
# LLM 会话,批量诊断或告警归因集中触发时按信号量排队,防止把目标机与
# LLM 配额同时打满(线程仍会创建,只是拿不到槽位时先等待)。
AGENT_MAX_CONCURRENT_RUNS = int(os.getenv("AGENT_MAX_CONCURRENT_RUNS", "4"))
# 自动化任务(巡检/脚本/电源)同一 job 内目标的并发执行上限。旧实现逐台串行,
# 50 台批量任务被单台耗时线性拖长;并发后每台结果口径不变(targets 按 id 排序,
# 汇总等全部终态)。默认 4 保守起步:巡检中心同水位 8、脚本 API 10 已长期在跑,
# 这里可按目标机承受度上调。脚本类另受全局 SSH 信号量(MAX_CONCURRENT_SSH=10)
# 封顶,与本配置取小生效。1 = 恢复逐台串行。
AUTOMATION_MAX_CONCURRENT_TARGETS = int(
    os.getenv("AUTOMATION_MAX_CONCURRENT_TARGETS", "4")
)
# LLM 调用对瞬时错误(429/5xx/连接异常)的最大重试次数,指数退避后重试;
# 一次抖动不再把已采完证据的整轮诊断作废。
AGENT_LLM_MAX_RETRIES = int(os.getenv("AGENT_LLM_MAX_RETRIES", "2"))

# 告警自愈:主机离线时若目标是 PVE 虚拟机/LXC 或 Docker 容器，自动下发启动指令;
# 指标过高时自动调用 Agent 做只读归因分析。两者都只在对应告警规则触发后执行，
# 规则本身就是用户的显式授权;可用下面的开关单独关闭。
ALERT_AUTO_REMEDIATION = os.getenv("ALERT_AUTO_REMEDIATION", "true").lower() == "true"
ALERT_AUTO_ANALYSIS = os.getenv("ALERT_AUTO_ANALYSIS", "true").lower() == "true"
# 同一告警事件最多自动启动几次、两次尝试之间的最小间隔(避免反复拉起)
ALERT_REMEDIATION_MAX_ATTEMPTS = int(os.getenv("ALERT_REMEDIATION_MAX_ATTEMPTS", "2"))
ALERT_REMEDIATION_COOLDOWN = int(os.getenv("ALERT_REMEDIATION_COOLDOWN_SECONDS", "600"))
# 下发启动指令后，最多等待多久确认目标真的恢复运行
ALERT_REMEDIATION_VERIFY_SECONDS = int(
    os.getenv("ALERT_REMEDIATION_VERIFY_SECONDS", "90")
)
# 触发自动分析前，指标需持续超阈值多久(避免瞬时毛刺就烧一次 LLM 调用)。
# 只在"不扣住通知"时生效:开启 HOLD_NOTIFICATION 后，告警本来就要等结论一起发，
# 再等一轮只会让放行那一刻仍然没有结论(防抖交给规则自己的 sustain_seconds)。
ALERT_ANALYSIS_SUSTAIN_SECONDS = int(os.getenv("ALERT_ANALYSIS_SUSTAIN_SECONDS", "180"))
# 等待 Agent 诊断出结论的上限
ALERT_ANALYSIS_WAIT_SECONDS = int(os.getenv("ALERT_ANALYSIS_WAIT_SECONDS", "300"))
# 指标告警(CPU/内存/磁盘)先扣住 alert.created，等 AI 归因出结论再连同结论一起发。
# 这样 Webhook / 飞书卡片里的「AI 归因」在同一条消息里就有结果，不必等第二条推送。
# 关掉则恢复旧行为:告警立刻发，归因结论另发一条 alert.remediation。
ALERT_ANALYSIS_HOLD_NOTIFICATION = (
    os.getenv("ALERT_ANALYSIS_HOLD_NOTIFICATION", "true").lower() == "true"
)
# 扣住通知的上限(秒)。归因卡死、进程重启或线程池打满时由看门狗放行——
# 告警可以迟到，绝不能被吞掉。取值要大于一次正常归因的耗时(实测约 50s)。
ALERT_ANALYSIS_HOLD_TIMEOUT = int(
    os.getenv("ALERT_ANALYSIS_HOLD_TIMEOUT_SECONDS", "180")
)
# 告警归因专用 Agent 步数上限(交互循环每步=SSH 取证+LLM 往返)。
ALERT_ANALYSIS_MAX_STEPS = int(os.getenv("ALERT_ANALYSIS_MAX_STEPS", "4"))
# 单-pass 归因结论轮的 LLM 调用超时。输出只有 ≤200 字,远小于交互诊断的
# 3000+ token 报告,不需要 180s 上限;收紧它让「取证 + 结论」几乎总落在
# ALERT_ANALYSIS_HOLD_TIMEOUT(180s)内,告警卡片稳定单条带结论,而不是
# 被看门狗先放行成无结论卡片、再靠 alert.analysis 兜底补发。
# 选值 60:实测结论轮 ~16s(含 reasoning,deepseek-v4-pro),留 4x 余量;
# 且取证 ~35s + 失败重试一轮(60s×2+退避)仍 < 180s 扣住预算。
# 注意:docker 部署下 .env 不会透传该变量(compose 未列),本默认值即容器值。
ALERT_ANALYSIS_LLM_TIMEOUT = int(os.getenv("ALERT_ANALYSIS_LLM_TIMEOUT", "60"))
# 已解决告警事件的保留天数;过期后由后台循环清理,防止告警中心查询随时间变慢。
ALERT_EVENT_RETENTION_DAYS = int(os.getenv("ALERT_EVENT_RETENTION_DAYS", "90"))

# ── 运行数据保留期(P0-3):agent_runs / automation_jobs / inspection_records
# 三张只增不删的表按天清理。0 = 不清理(默认,行为与现状完全一致)——
# 巡检/任务留档是产品特性,删不删是业务决策;这里只把清理能力装好,
# 何时启用由 .env 显式配置。只删终态数据(running/pending 永不删);
# agent_runs 额外跳过仍被 alert_events.agent_run_id 引用的行,
# 防止保留期低于告警保留期时出现归因详情悬空。
AGENT_RUN_RETENTION_DAYS = int(os.getenv("AGENT_RUN_RETENTION_DAYS", "0"))
INSPECTION_RETENTION_DAYS = int(os.getenv("INSPECTION_RETENTION_DAYS", "0"))
AUTOMATION_JOB_RETENTION_DAYS = int(os.getenv("AUTOMATION_JOB_RETENTION_DAYS", "0"))

# 业务监控:接口 HTTP 探测(后端直接探测,周期 upsert interface_probes)
INTERFACE_PROBE_ENABLED = os.getenv("INTERFACE_PROBE_ENABLED", "true").lower() == "true"
INTERFACE_PROBE_INTERVAL = int(os.getenv("INTERFACE_PROBE_INTERVAL", "60"))
INTERFACE_PROBE_CONCURRENCY = int(os.getenv("INTERFACE_PROBE_CONCURRENCY", "10"))

# 业务状态告警独立评估循环:输入是 monitor 状态/接口探测/PVE 快照,与指标
# 采集无关。原先挂在指标采集循环里,METRICS_ENABLED=false 的纯接口监控部署
# 会让业务告警静默失效,故独立成环(2026-09-17)。
# 业务状态告警评估周期(2026-09-18 由 60 收紧到 30):评估只读内存快照+三条
# 批量查询,成本极低;配合资源项的 sampled_at 回溯,触发延迟不再叠加评估相位。
BUSINESS_ALERT_INTERVAL = int(os.getenv("BUSINESS_ALERT_INTERVAL", "30"))

# Docker 容器管理(周期探测服务器内所有容器;Linux 走 SSH、Windows 走 WinRM)
CONTAINERS_ENABLED = os.getenv("CONTAINERS_ENABLED", "true").lower() == "true"
CONTAINERS_INTERVAL = int(os.getenv("CONTAINERS_INTERVAL", "60"))
CONTAINERS_CONCURRENCY = int(os.getenv("CONTAINERS_CONCURRENCY", "10"))
CONTAINERS_TIMEOUT = int(os.getenv("CONTAINERS_TIMEOUT", "25"))

# PVE 虚拟机状态快照(周期拉取 /cluster/resources;业务监控等模块从内存读取,
# 避免请求路径直接阻塞在 PVE API 上)
PVE_STATUS_ENABLED = os.getenv("PVE_STATUS_ENABLED", "true").lower() == "true"
PVE_STATUS_INTERVAL = int(os.getenv("PVE_STATUS_INTERVAL", "30"))
# 虚拟机文件系统使用率采集(磁盘告警数据源):QGA get-fsinfo 优先,运维接入
# SSH/WinRM 兑底。每轮要对每台目标虚机真实调 PVE API/QGA 或握手 SSH,比读
# 快照贵得多,放独立低频循环(评估器只读内存缓存)。只在存在启用中的
# disk_max_pct 规则时才采集。
GUEST_FS_METRICS_ENABLED = (
    os.getenv("GUEST_FS_METRICS_ENABLED", "true").lower() == "true"
)
GUEST_FS_METRICS_INTERVAL = int(os.getenv("GUEST_FS_METRICS_INTERVAL", "300"))
# 虚拟机最近已知 IP 刷新(离线告警的地址兕底):guest 运行期间低频问一次 QGA
# network-get-interfaces 记下地址,停机后告警卡片的「地址」不再退回 '-'。
# 与 fs 采集同一闸门思路:仅在存在启用中的 host_status 规则时才探测。
GUEST_IP_REFRESH_ENABLED = (
    os.getenv("GUEST_IP_REFRESH_ENABLED", "true").lower() == "true"
)
GUEST_IP_REFRESH_INTERVAL = int(os.getenv("GUEST_IP_REFRESH_INTERVAL", "300"))
PVE_STATUS_TIMEOUT = int(os.getenv("PVE_STATUS_TIMEOUT", "6"))

# Rate limiting
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
RATE_LIMIT_STORAGE = os.getenv("RATE_LIMIT_STORAGE", "memory://")
WS_TICKET_STORAGE = os.getenv("WS_TICKET_STORAGE", "memory://")
BACKGROUND_TASKS_ENABLED = (
    os.getenv("BACKGROUND_TASKS_ENABLED", "true").lower() == "true"
)

# 是否在启动时自动建表（仅开发便利）。生产应使用 Alembic (`alembic upgrade head`)。
AUTO_CREATE_TABLES = os.getenv("DCN_AUTO_CREATE", "false").lower() == "true"

# 通知文案里给人看的时间所使用的时区(IANA 名称)。数据库统一存 UTC，飞书卡片、
# 多维表格字段等是直接给运维读的，渲染时要转成这个时区。留空则跟随服务器系统
# 时区(容器里通常是 UTC，一般不是想要的结果)。
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "Asia/Shanghai")
