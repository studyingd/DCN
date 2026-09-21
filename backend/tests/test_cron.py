"""5 段 cron 解析与下次执行时间计算。

前端构建器只能生成有限形状，但后端接受任意合法表达式（区间、列表、步长），
所以这一层必须自己钉住：解析结果要与比较逻辑一致，否则会出现
「parse_cron 通过、next_cron_time 永远算不出时间」这类看不懂的报错。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.cron import next_cron_time, parse_cron


def _utc(**kw) -> datetime:
    base = datetime(2024, 3, 4, 0, 0, tzinfo=timezone.utc)  # 周一
    return base.replace(**kw)


# ── 字段解析 ──


def test_field_count_is_enforced():
    with pytest.raises(ValueError, match="5 fields"):
        parse_cron("* * * *")
    with pytest.raises(ValueError, match="5 fields"):
        parse_cron("* * * * * *")


def test_star_expands_to_full_range():
    parsed = parse_cron("* * * * *")
    assert parsed["minute"] == set(range(0, 60))
    assert parsed["hour"] == set(range(0, 24))
    assert parsed["day"] == set(range(1, 32))
    assert parsed["month"] == set(range(1, 13))
    assert parsed["weekday"] == set(range(0, 7))


def test_ranges_and_lists():
    """前端构建器生成不了这些形状，但后端必须正确支持（API 可直接传）。"""
    assert sorted(parse_cron("0 2 * * 1-5")["weekday"]) == [1, 2, 3, 4, 5]
    assert sorted(parse_cron("0 2 * * 1,3,5")["weekday"]) == [1, 3, 5]
    assert sorted(parse_cron("0 2,14 * * *")["hour"]) == [2, 14]
    assert sorted(parse_cron("0 2 * * 1-3,6")["weekday"]) == [1, 2, 3, 6]


def test_step_values():
    assert sorted(parse_cron("*/15 * * * *")["minute"]) == [0, 15, 30, 45]
    assert sorted(parse_cron("0 */6 * * *")["hour"]) == [0, 6, 12, 18]


def test_weekday_seven_normalizes_to_sunday():
    """cron 惯例里 0 与 7 都是周日。

    不归一化的话 parse_cron 会成功，但 next_cron_time 比较用的
    (weekday()+1)%7 只落在 0..6，永远匹配不到 7 → 抛
    "No matching time found within 1 year"，前端只能显示一句看不懂的报错。
    """
    assert parse_cron("0 2 * * 7")["weekday"] == {0}
    assert sorted(parse_cron("0 2 * * 5-7")["weekday"]) == [0, 5, 6]


# ── next_cron_time ──


def test_next_cron_time_daily():
    # 2024-03-04 是周一；从 01:00 起算，下一个 02:00 是当天
    assert next_cron_time("0 2 * * *", _utc(hour=1)) == _utc(hour=2)
    # 已经过了 02:00 → 次日
    assert next_cron_time("0 2 * * *", _utc(hour=3)) == _utc(hour=2) + timedelta(days=1)


def test_next_cron_time_every_n_minutes():
    assert next_cron_time("*/15 * * * *", _utc(minute=7)) == _utc(minute=15)
    assert next_cron_time("*/15 * * * *", _utc(minute=15)) == _utc(minute=30)


def test_next_cron_time_weekday_range_skips_weekend():
    """周一至周五：周五晚上之后应落到下周一。"""
    friday_night = datetime(2024, 3, 8, 23, 0, tzinfo=timezone.utc)  # 周五
    assert friday_night.weekday() == 4
    nxt = next_cron_time("0 2 * * 1-5", friday_night)
    assert nxt == datetime(2024, 3, 11, 2, 0, tzinfo=timezone.utc)  # 下周一
    assert nxt.weekday() == 0


def test_next_cron_time_weekday_seven_matches_sunday():
    """归一化之后，`* * 7` 必须真的能在周日触发（回归：以前永远算不出来）。"""
    saturday = datetime(2024, 3, 9, 12, 0, tzinfo=timezone.utc)
    assert saturday.weekday() == 5
    nxt = next_cron_time("0 2 * * 7", saturday)
    assert nxt.weekday() == 6  # Python 的周日
    assert nxt == datetime(2024, 3, 10, 2, 0, tzinfo=timezone.utc)


def test_next_cron_time_seconds_are_dropped():
    """从「下一分钟」开始找，避免把当前这一分钟重复触发。"""
    moment = datetime(2024, 3, 4, 2, 0, 30, tzinfo=timezone.utc)
    nxt = next_cron_time("0 2 * * *", moment)
    assert nxt.second == 0
    assert nxt > moment


def test_next_cron_time_impossible_expression_raises():
    """2 月 30 日不存在——必须报错，而不是静默返回一个错时间。"""
    with pytest.raises(ValueError, match="No matching time"):
        next_cron_time("0 0 30 2 *", _utc())
