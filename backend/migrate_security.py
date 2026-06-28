"""
数据库安全字段迁移脚本。
为已有的 users 表添加账户锁定和密码策略相关字段。

用法（在 backend/ 目录下执行）：
    DATABASE_URL="mysql+pymysql://user:pass@host:3306/dcn" python migrate_security.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text

from app.database import engine

MIGRATIONS = [
    # Add failed login tracking
    text(
        """
        ALTER TABLE users
        ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0
        """
    ),
    # Add lockout timestamp
    text(
        """
        ALTER TABLE users
        ADD COLUMN locked_until DATETIME NULL
        """
    ),
    # Add last login timestamp
    text(
        """
        ALTER TABLE users
        ADD COLUMN last_login DATETIME NULL
        """
    ),
    # Add password changed timestamp
    text(
        """
        ALTER TABLE users
        ADD COLUMN password_changed_at DATETIME NULL
        """
    ),
]


def run_migration():
    with engine.connect() as conn:
        # Check which columns already exist
        existing = set()
        result = conn.execute(
            text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'users'")
        )
        for row in result:
            existing.add(row[0])

        for stmt in MIGRATIONS:
            # Extract column name from the SQL
            sql_str = str(stmt).upper()
            if "ADD COLUMN" in sql_str:
                col_name = sql_str.split("ADD COLUMN")[1].strip().split()[0].lower()
                if col_name in existing:
                    print(f"[SKIP] Column '{col_name}' already exists.")
                    continue

            try:
                conn.execute(stmt)
                conn.commit()
                print(f"[OK] {str(stmt)[:80]}...")
            except Exception as e:
                print(f"[ERROR] {e}")
                conn.rollback()
                raise


if __name__ == "__main__":
    if not os.getenv("DATABASE_URL"):
        print(
            "[ERROR] DATABASE_URL not set. "
            "Set it before running: DATABASE_URL='mysql+pymysql://...' python migrate_security.py"
        )
        sys.exit(1)
    run_migration()
    print("Migration complete.")
