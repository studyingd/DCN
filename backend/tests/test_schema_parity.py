"""Alembic 建出的 schema 必须能承载全部模型 —— 把迁移链漏列钉在测试里。

`Base.metadata.create_all` 只建缺失的**表**，永远不会给已存在的表补**列**。所以迁移链
一旦漏掉某列，用 `alembic upgrade head` 建库(CI 与 docker-entrypoint 都走这条路)就会
得到一个"表都在、写数据就报错"的 schema。`alert_events` 的 6 个自动处置/AI 归因列曾经
正是如此:模型里有、迁移链里没有，只有手工跑过那个一次性 ALTER 脚本(现已删除)的开发库碰巧
能用，全新部署第一次插告警就炸 `Unknown column 'remediation_state'`，已由 0032 补齐。

这里刻意**不**调用 create_all，直接拿模型元数据比对真实库结构，检出的就是迁移链的欠账。
"""

import sqlalchemy as sa

import app.models  # noqa: F401 — 注册全部模型
from app.database import Base, engine


def _inspector() -> sa.Inspector:
    return sa.inspect(engine)


def _drift() -> tuple[list[str], list[str], list[str]]:
    """返回 (缺失的表, 缺失的列, 缺失的索引)。"""
    inspector = _inspector()
    db_tables = set(inspector.get_table_names())
    missing_tables: list[str] = []
    missing_columns: list[str] = []
    missing_indexes: list[str] = []
    for name, table in Base.metadata.tables.items():
        if name not in db_tables:
            missing_tables.append(name)
            continue
        db_columns = {column["name"] for column in inspector.get_columns(name)}
        missing_columns.extend(
            f"{name}.{column.name}"
            for column in table.columns
            if column.name not in db_columns
        )
        db_indexes = {index["name"] for index in inspector.get_indexes(name)}
        missing_indexes.extend(
            f"{name}.{index.name}"
            for index in table.indexes
            if index.name and index.name not in db_indexes
        )
    return sorted(missing_tables), sorted(missing_columns), sorted(missing_indexes)


def test_database_has_every_model_table():
    missing_tables, _, _ = _drift()
    assert not missing_tables, (
        f"迁移链没建出这些表: {missing_tables}。"
        "请补一条 alembic 迁移，不要依赖 create_all。"
    )


def test_database_has_every_model_column():
    """核心防回归:模型需要的每一列，`alembic upgrade head` 都必须建出来。"""
    _, missing_columns, _ = _drift()
    assert not missing_columns, (
        f"迁移链漏了这些列: {missing_columns}。"
        "create_all 不会给已存在的表补列，全新部署会在首次写入时报 Unknown column。"
    )


def test_database_has_every_model_index():
    _, _, missing_indexes = _drift()
    assert not missing_indexes, (
        f"迁移链漏了这些索引: {missing_indexes}。"
        "缺索引不会立刻报错，但会让列表/告警查询在大表上退化成全表扫描。"
    )
