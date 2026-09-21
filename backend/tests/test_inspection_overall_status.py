"""整条巡检记录的总状态判定回归测试。

失败服务改成按 THRESHOLDS 分级后，1~2 个失败服务只产生 warning。若总状态仍旧
只认 critical，这类机器会被判成 completed（前端显示「正常」、自动化任务显示成功），
等于把问题静默吞掉。这里钉住 warning 必须让记录落到 partial。
"""

import pytest

from app.services.inspection import _derive_overall_status


def _counts(normal=0, warning=0, critical=0, error=0) -> tuple[dict[str, int], int]:
    counts = {
        "normal": normal,
        "warning": warning,
        "critical": critical,
        "error": error,
    }
    return counts, normal + warning + critical + error


def test_all_normal_is_completed():
    assert _derive_overall_status(*_counts(normal=6)) == "completed"


@pytest.mark.parametrize("warnings", [1, 2, 5])
def test_any_warning_makes_record_partial(warnings):
    """核心场景：6 项里 1 项 warning（如 1 个失败服务）不能算「正常」。"""
    counts, total = _counts(normal=6 - warnings, warning=warnings)
    assert _derive_overall_status(counts, total) == "partial"


def test_single_critical_out_of_six_is_partial():
    assert _derive_overall_status(*_counts(normal=5, critical=1)) == "partial"


def test_critical_majority_is_failed():
    assert _derive_overall_status(*_counts(normal=2, critical=4)) == "failed"


def test_error_and_critical_majority_is_failed():
    assert _derive_overall_status(*_counts(normal=1, critical=2, error=2)) == "failed"


def test_all_error_is_failed():
    assert _derive_overall_status(*_counts(error=6)) == "failed"


def test_no_items_is_failed():
    assert _derive_overall_status(*_counts()) == "failed"


def test_error_wins_over_completed_even_when_minority():
    assert _derive_overall_status(*_counts(normal=5, error=1)) == "partial"
