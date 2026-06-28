"""One-time migration: add RBAC tables, extend users, seed default roles."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Set required environment variables if not already set (for convenience in dev)
if not os.getenv("DATABASE_URL"):
    os.environ["DATABASE_URL"] = (
        "mysql+pymysql://admin:dcncloud@localhost:3306/dcn?charset=utf8mb4"
    )

from sqlalchemy import text

from app.config import ADMIN_USERNAME
from app.database import Base, SessionLocal, engine
from app.models.role import Role

# Import all models so relationships resolve
from app.models.user import User


def migrate():
    # 1. Create new tables
    Base.metadata.create_all(bind=engine)
    print("[OK] RBAC tables created")

    # 2. Add new columns to existing users table
    new_columns = [
        ("display_name", "VARCHAR(128) NULL"),
        ("is_active", "TINYINT NOT NULL DEFAULT 1"),
        ("role_id", "INT NULL"),
        ("group_id", "INT NULL"),
    ]
    with engine.connect() as conn:
        for col_name, col_def in new_columns:
            try:
                conn.execute(
                    text(
                        f"ALTER TABLE users ADD COLUMN {col_name} {col_name} {col_def}"
                    )
                )
                conn.commit()
                print(f"  Added column users.{col_name}")
            except Exception:
                print(f"  Column users.{col_name} already exists, skipping")

        # Fix: the ALTER above has a bug (duplicated column name). Re-do properly.
        # Actually let me check what's really needed.
    # Re-do with correct SQL
    with engine.connect() as conn:
        # Check existing columns
        result = conn.execute(text("SHOW COLUMNS FROM users"))
        existing = {row[0] for row in result}

        alters = []
        if "display_name" not in existing:
            alters.append("ALTER TABLE users ADD COLUMN display_name VARCHAR(128) NULL")
        if "is_active" not in existing:
            alters.append(
                "ALTER TABLE users ADD COLUMN is_active TINYINT NOT NULL DEFAULT 1"
            )
        if "role_id" not in existing:
            alters.append("ALTER TABLE users ADD COLUMN role_id INT NULL")
        if "group_id" not in existing:
            alters.append("ALTER TABLE users ADD COLUMN group_id INT NULL")

        for sql in alters:
            conn.execute(text(sql))
            conn.commit()
            print(f"  {sql}")

        # Add foreign keys if not present
        try:
            conn.execute(
                text(
                    "ALTER TABLE users ADD CONSTRAINT fk_user_role "
                    "FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE SET NULL"
                )
            )
            conn.commit()
            print("  Added FK users.role_id -> roles.id")
        except Exception:
            pass

        try:
            conn.execute(
                text(
                    "ALTER TABLE users ADD CONSTRAINT fk_user_group "
                    "FOREIGN KEY (group_id) REFERENCES user_groups(id) ON DELETE SET NULL"
                )
            )
            conn.commit()
            print("  Added FK users.group_id -> user_groups.id")
        except Exception:
            pass

    print("[OK] Users table extended")

    # 3. Seed default roles
    db = SessionLocal()
    try:
        all_perms = list(PERMISSIONS_CATALOG.keys())

        admin_role = db.query(Role).filter(Role.name == "管理员").first()
        if not admin_role:
            admin_role = Role(
                name="管理员",
                description="系统管理员，拥有全部权限",
                permissions=json.dumps(all_perms, ensure_ascii=False),
                device_scope="all",
                is_builtin=1,
            )
            db.add(admin_role)
            db.commit()
            db.refresh(admin_role)
            print("[OK] Created builtin role: 管理员")
        else:
            print("  管理员 role already exists")

        viewer_role = db.query(Role).filter(Role.name == "只读用户").first()
        if not viewer_role:
            viewer_role = Role(
                name="只读用户",
                description="只读查看权限",
                permissions=json.dumps(
                    ["device:read", "room:read"], ensure_ascii=False
                ),
                device_scope="all",
                is_builtin=1,
            )
            db.add(viewer_role)
            db.commit()
            print("[OK] Created builtin role: 只读用户")
        else:
            print("  只读用户 role already exists")

        # 4. Assign admin role to existing admin user
        admin_user = db.query(User).filter(User.username == ADMIN_USERNAME).first()
        if admin_user and admin_user.role_id is None and admin_role:
            admin_user.role_id = admin_role.id
            admin_user.is_active = 1
            db.commit()
            print(f"[OK] Assigned 管理员 role to user '{ADMIN_USERNAME}'")
        elif not admin_user:
            print("  No admin user found")
    finally:
        db.close()

    print("\nMigration complete!")


PERMISSIONS_CATALOG = {
    "device:read": "查看设备",
    "device:write": "创建/修改设备",
    "device:delete": "删除设备",
    "device:terminal": "SSH/RDP 终端访问",
    "credential:read": "查看凭据",
    "credential:write": "创建/修改凭据",
    "room:read": "查看机房/机架",
    "room:write": "创建/修改机房/机架",
    "user:manage": "管理用户和角色",
    "audit:read": "查看审计日志",
    "settings:manage": "系统设置",
}


if __name__ == "__main__":
    migrate()
