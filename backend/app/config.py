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

# MySQL 数据库 (required — no default credentials)
DATABASE_URL = _require_env("DATABASE_URL")

# JWT 配置 (secret is auto-generated if not set; set explicitly for production)
JWT_SECRET = _secret("JWT_SECRET")
JWT_ALGORITHM = "HS256"
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

# 凭证加密密钥 — 必须显式设置（自动生成会导致历史加密凭据不可解密、无法恢复）
CREDENTIAL_SECRET_KEY = _required_secret("CREDENTIAL_SECRET_KEY")

# RDP 安全策略：negotiate/tls/nls/any；默认 any（与历史一致，生产建议 tls/nls）。
# 证书校验：默认严格（false=校验），仅在受控内网且确有必要时设 true 跳过。
GUAC_RDP_SECURITY = os.getenv("GUAC_RDP_SECURITY", "any")
GUAC_RDP_IGNORE_CERT = os.getenv("GUAC_RDP_IGNORE_CERT", "false").lower() == "true"

# MinIO / RustFS 对象存储 (auto-generated keys if not set)
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:19000")
MINIO_ACCESS_KEY = _secret("MINIO_ACCESS_KEY")
MINIO_SECRET_KEY = _secret("MINIO_SECRET_KEY")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"
MINIO_BUCKET_RECORDINGS = os.getenv("MINIO_BUCKET_RECORDINGS", "dcn-recordings")
MINIO_BUCKET_AUDIT = os.getenv("MINIO_BUCKET_AUDIT", "dcn-audit-logs")

# Device monitor configuration
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "30"))
MONITOR_TIMEOUT = int(os.getenv("MONITOR_TIMEOUT", "2"))
MONITOR_CONCURRENCY = int(os.getenv("MONITOR_CONCURRENCY", "20"))

# Rate limiting
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
RATE_LIMIT_STORAGE = os.getenv("RATE_LIMIT_STORAGE", "memory://")

# 脚本/远程命令执行策略：
#   "block"（默认）—— 拦截高危命令模式并审计；"allow" —— 仅审计不拦截。
SCRIPT_COMMAND_POLICY = os.getenv("SCRIPT_COMMAND_POLICY", "block").lower()

# 是否在启动时自动建表（仅开发便利）。生产应使用 Alembic (`alembic upgrade head`)。
AUTO_CREATE_TABLES = os.getenv("DCN_AUTO_CREATE", "false").lower() == "true"
