"""Pytest configuration for the DCN backend.

Tests run directly against the dev MySQL (127.0.0.1:3307) ``dcn`` schema:
fixtures create rows normally and the id-snapshot janitor in this file
deletes exactly what each test created afterwards, so dev data survives.
The URL is exposed as ``DATABASE_URL`` before any application module is
imported; ``TEST_DATABASE_URL`` can still override it (e.g. CI).
"""

import os

import pytest

# 默认直连开发 MySQL(3307) 的 dcn 库——测试数据由 conftest 的 id 快照机制
# 精确清理,不污染开发数据,无需独立测试库/容器。
_DEFAULT_TEST_DATABASE_URL = (
    "mysql+pymysql://root:{pwd}@127.0.0.1:3307/dcn?charset=utf8mb4".format(
        pwd=os.getenv("MYSQL_ROOT_PASSWORD", "")
    )
)
_test_database_url = os.getenv("TEST_DATABASE_URL") or _DEFAULT_TEST_DATABASE_URL
os.environ["DATABASE_URL"] = _test_database_url
os.environ.setdefault("DCN_AUTO_CREATE", "false")
# Ensure required secrets exist even if .env is absent (CI).
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-" + "x" * 32)
os.environ.setdefault("CREDENTIAL_SECRET_KEY", "test-credential-secret-" + "y" * 32)
os.environ.setdefault("ADMIN_PASSWORD", "TestAdmin-123")


def _snapshotable_tables(metadata):
    """挑出「单一整型自增主键 id」的表。

    只有这类表能靠 id 快照做增量清理；复合主键或字符串主键的表跳过。
    """
    from sqlalchemy import Integer

    tables = []
    for table in metadata.sorted_tables:
        columns = list(table.primary_key.columns)
        if (
            len(columns) == 1
            and columns[0].name == "id"
            and isinstance(columns[0].type, Integer)
        ):
            tables.append(table)
    # sorted_tables 是「父表在前」的拓扑序，反过来正好是删除需要的子表优先。
    return list(reversed(tables))


@pytest.fixture(scope="session", autouse=True)
def sweep_session_rows():
    """会话结束后回收本次运行新建的行，避免测试库越跑越脏。

    历史情况：不少用例直接 ``SessionLocal()`` 建数据却不回收，dcn_test 里累积了
    170+ users、28 pve_connections、42 device_docker_status，以及 14 条 rule_id
    被外键置空的孤儿 alert_events。脏数据本身不致命，但会让「读全表」的用例
    （``_pve_entries``、告警总览之类）读到上一轮残留，失败与否随运行次数漂移。

    这里只是兜底：按主键快照删掉本次会话新增的行，运行前就存在的数据一律不动。
    用例内部仍应自己清理，否则同一次运行里用例之间还是会互相看见对方的数据。
    """
    from sqlalchemy import func, or_, text

    from app.database import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    tables = _snapshotable_tables(Base.metadata)

    baseline: dict[str, int] = {}
    with SessionLocal() as db:
        for table in tables:
            baseline[table.name] = db.query(func.max(table.c.id)).scalar() or 0

    yield

    removed: dict[str, int] = {}
    with SessionLocal() as db:
        # 先关外键检查：库里有少量只在 DDL 层声明、模型中没有对应 ForeignKey 的
        # 引用，纯靠拓扑序删不干净；测试库是一次性的，删完再打开即可。
        db.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        try:
            for table in tables:
                if table.name == "alert_events":
                    # 开发库与并行的容器评估循环共享：容器会持续创建真实告警
                    # 事件(rule_id 指向既有规则，id 会超过本会话的 baseline)。
                    # 按 id 快照无差别删除会误删真实告警(2026-09-17 实测)。
                    # 只回收「本会话新建规则」名下的事件与规则已删空的孤儿行
                    # (rule_id IS NULL,测试删规则后 FK 置空的残留)，真实告警
                    # 行(rule_id ≤ alert_rules baseline)一律不动。
                    rules_baseline = baseline.get("alert_rules", 0)
                    result = db.execute(
                        table.delete().where(
                            table.c.id > baseline[table.name],
                            or_(
                                table.c.rule_id.is_(None),
                                table.c.rule_id > rules_baseline,
                            ),
                        )
                    )
                else:
                    result = db.execute(
                        table.delete().where(table.c.id > baseline[table.name])
                    )
                if result.rowcount:
                    removed[table.name] = result.rowcount
            db.commit()
        finally:
            db.execute(text("SET FOREIGN_KEY_CHECKS=1"))
            db.commit()

    if removed:
        detail = ", ".join(f"{name}={count}" for name, count in sorted(removed.items()))
        print(f"\n[test-hygiene] 回收本次运行新建的行: {detail}")
