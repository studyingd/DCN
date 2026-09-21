"""接口探测器(interface_prober)的消抖与立即探测行为。

钉住 2026-09-17 的两项:
- 周期轮消抖:单次失败沿用上一轮落库结果(不写不翻灯),连续 2 次才落
  down,恢复 up 单次即生效——与 monitor 的 OFFLINE_CONFIRM_SCANS 同语义。
- 立即探测(trigger_immediate_probe):同步路由线程经事件循环触发;探测
  器未运行时静默跳过,周期循环兜底。
"""

from app.services import interface_prober as prober


def _result(up: int) -> dict:
    return {
        "up": up,
        "status_code": 200 if up else None,
        "latency_ms": 10,
        "error": None if up else "connect timeout",
    }


def test_cycle_single_failure_is_withheld(monkeypatch):
    """周期轮首次失败不落库(沿用上一轮结果),不翻灯。"""
    monkeypatch.setattr(prober, "_fail_streaks", {})
    kept = prober._filter_debounced([(1, _result(0))], immediate=False)
    assert kept == []
    assert prober._fail_streaks == {1: 1}


def test_cycle_second_consecutive_failure_confirms_down(monkeypatch):
    monkeypatch.setattr(prober, "_fail_streaks", {})
    prober._filter_debounced([(1, _result(0))], immediate=False)
    kept = prober._filter_debounced([(1, _result(0))], immediate=False)
    assert len(kept) == 1
    assert kept[0][0] == 1 and kept[0][1]["up"] == 0
    assert prober._fail_streaks[1] == 2


def test_cycle_recovery_is_immediate(monkeypatch):
    """失败计数未满 2 次时恢复 up,结果立即写入并清零计数。"""
    monkeypatch.setattr(prober, "_fail_streaks", {1: 1})
    kept = prober._filter_debounced([(1, _result(1))], immediate=False)
    assert len(kept) == 1 and kept[0][1]["up"] == 1
    assert 1 not in prober._fail_streaks


def test_cycle_intermittent_failure_never_flips(monkeypatch):
    """失败/成功交替永远不落 down(交替抖动不产生闪烁)。"""
    monkeypatch.setattr(prober, "_fail_streaks", {})
    for _ in range(4):
        prober._filter_debounced([(1, _result(0))], immediate=False)
        prober._filter_debounced([(1, _result(1))], immediate=False)
    assert prober._fail_streaks == {}


def test_cycle_prunes_streaks_for_removed_interfaces(monkeypatch):
    """接口删除/停用后残留计数必须被周期轮清掉(内存防泄漏)。"""
    monkeypatch.setattr(prober, "_fail_streaks", {9: 1})
    prober._filter_debounced([(1, _result(1))], immediate=False)
    assert 9 not in prober._fail_streaks


def test_immediate_probe_writes_failure_directly(monkeypatch):
    """立即探测(刚保存的配置)不消抖:失败直接写入,给用户真实反馈。"""
    monkeypatch.setattr(prober, "_fail_streaks", {})
    kept = prober._filter_debounced([(1, _result(0))], immediate=True)
    assert len(kept) == 1 and kept[0][1]["up"] == 0
    # 但计数照样累积:下一轮周期失败即确认(连续第 2 次)
    assert prober._fail_streaks == {1: 1}


def test_immediate_probe_success_clears_streak(monkeypatch):
    monkeypatch.setattr(prober, "_fail_streaks", {1: 1})
    prober._filter_debounced([(1, _result(1))], immediate=True)
    assert prober._fail_streaks == {}


def test_trigger_immediate_probe_noop_without_loop():
    """探测器未运行(无事件循环)时静默跳过,不抛异常。"""
    assert prober._loop is None or prober._loop.is_closed()
    prober.trigger_immediate_probe([1, 2])  # 不应抛错


def test_trigger_immediate_probe_ignores_empty_ids(monkeypatch):
    """空/无效 id 列表直接跳过,不投递协程。"""

    class _Loop:
        def is_closed(self) -> bool:
            return False

        def run_coroutine_threadsafe(self, coro, loop):
            raise AssertionError("不应投递协程")

    monkeypatch.setattr(prober, "_loop", _Loop())
    prober.trigger_immediate_probe([])
    prober.trigger_immediate_probe([0, None])
