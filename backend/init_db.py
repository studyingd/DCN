"""
数据库初始化脚本：创建所有表并插入管理员账户。
用法：
    DATABASE_URL="mysql+pymysql://admin:dcncloud@localhost:3306/dcn?charset=utf8mb4" python init_db.py

生产环境推荐使用 Alembic 管理 schema（``alembic upgrade head``），而非本脚本的
create_all。本脚本主要面向首次/开发环境的快速初始化。
"""

import json
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Set required environment variables if not already set (for convenience in dev)
if not os.getenv("DATABASE_URL"):
    os.environ["DATABASE_URL"] = (
        "mysql+pymysql://admin:dcncloud@localhost:3306/dcn?charset=utf8mb4"
    )
    print(
        "[WARNING] DATABASE_URL not set, using dev default. Set it explicitly for production."
    )

from app.config import ADMIN_PASSWORD, ADMIN_USERNAME
from app.database import Base, SessionLocal, engine
from app.models import User  # noqa: F401 — 确保模型被导入以注册表
from app.models.role import Role
from app.services.auth import hash_password
from app.services.permissions import PERMISSIONS

ADMIN_ROLE_NAME = "管理员"


def _ensure_admin_role(db) -> Role:
    """Ensure the built-in system-admin role exists (is_admin=True, all perms)."""
    role = db.query(Role).filter(Role.name == ADMIN_ROLE_NAME).first()
    if role is None:
        role = Role(
            name=ADMIN_ROLE_NAME,
            description="系统管理员（拥有全部权限与全部设备作用域）",
            permissions=json.dumps(list(PERMISSIONS.keys())),
            device_scope="all",
            is_builtin=1,
            is_admin=True,
        )
        db.add(role)
        db.commit()
        db.refresh(role)
    return role


def init_db():
    # 创建所有表（开发/首次初始化）
    Base.metadata.create_all(bind=engine)
    print("数据库表已创建。")

    db = SessionLocal()
    try:
        admin_role = _ensure_admin_role(db)

        existing = db.query(User).filter(User.username == ADMIN_USERNAME).first()
        if existing:
            # Backfill: bind legacy `role="admin"` users (created before the
            # RBAC model) to the system-admin role so they keep full access now
            # that the text-column authorization bypass has been removed.
            if existing.role_id is None:
                existing.role_id = admin_role.id
                db.commit()
                print(f"已将已有管理员 '{ADMIN_USERNAME}' 绑定到系统管理员角色。")
            print(f"管理员用户 '{ADMIN_USERNAME}' 已存在，跳过创建。")
        else:
            admin = User(
                username=ADMIN_USERNAME,
                password=hash_password(ADMIN_PASSWORD),
                role="admin",
                role_id=admin_role.id,
            )
            db.add(admin)
            db.commit()
            print(f"管理员用户 '{ADMIN_USERNAME}' 已创建。")
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
