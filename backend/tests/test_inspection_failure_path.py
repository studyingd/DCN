"""巡检「命令失败」路径的回归测试。

事故现场（device 9 / SRM，Windows，record 4）：WinRM 连 192.168.1.126:5985 超时，
六项检查全部失败，库里却存成

    cpu     25.0 %  normal      ← 从错误串的 "connect timeout=25" 里捞出来的
    memory  25.0 %  normal
    disk    NULL %  error       ← 唯一如实失败的一项
    uptime  0天0时0分 normal
    services 1 个   warning
    event_logs 1 条 normal

汇总 normal=4 / warning=1 / error=1 → 记录判成 partial（应为 failed）。
前端因此渲染出自相矛盾的一屏：五行假数据标着「正常」，只有磁盘那行是个
孤零零的「%」加错误标签——看起来像磁盘的 UI 坏了，其实它是唯一没错的。

根因两条，都在 _build_item_result 里钉住：
  1. exit_code != 0 时 stdout 为空、stderr 是错误信息，旧代码 `stdout or stderr`
     把错误文本当命令输出喂进了解析器；
  2. 旧代码只用 exit_code 收敛 success，从不收敛 status，解析器给的 normal
     原样进了库，污染计数与整条记录结论。
"""

import pytest

from app.services.inspection import (
    _build_item_result,
    _derive_overall_status,
    _failed_item,
)
from app.services.inspection_commands import CORE_ITEMS

# 库里那条真实 stderr（❌ 在 mysql 客户端下显示为 ????，这里保留原字符）。
WINRM_TIMEOUT = (
    "❌ WinRM (192.168.1.126:5985): HTTPConnectionPool(host='192.168.1.126', "
    "port=5985): Max retries exceeded with url: /wsman (Caused by "
    "ConnectTimeoutError(<HTTPConnection(host='192.168.1.126', port=5985) at "
    "0x10d607a10>, 'Connection to 192.168.1.126 timed out. (connect timeout=25)'))"
)

# _exec_ssh_command 在 TOFU 主机密钥不匹配时返回的错误文本。这条更危险：
# 一个中间人攻击告警会被解析成指标并标成 normal，等于把安全问题静默吞掉。
SSH_MITM = "SSH host key mismatch — possible MITM"

WINDOWS_CORE = CORE_ITEMS["windows"]
LINUX_CORE = CORE_ITEMS["linux"]


# ── 1. 失败项的形状：绝不能带出任何数值 ──


@pytest.mark.parametrize("item_type", WINDOWS_CORE)
def test_windows_core_items_all_fail_on_winrm_timeout(item_type):
    """六项全部如实失败，且 value/unit 一律为空。

    旧行为里 cpu/memory 会变成 25.0 %（normal）、services 变成 1 个（warning）。
    """
    parsed, raw = _build_item_result(item_type, "windows", 1, "", WINRM_TIMEOUT)

    assert parsed["success"] is False
    assert parsed["status"] == "error"
    assert parsed["value"] is None
    assert parsed["unit"] is None
    assert parsed["details"] is None
    assert "WinRM" in parsed["error_message"]
    # 原始输出仍然保留错误文本，方便详情页排查
    assert raw == WINRM_TIMEOUT


@pytest.mark.parametrize("item_type", LINUX_CORE)
def test_linux_core_items_all_fail_on_host_key_mismatch(item_type):
    """SSH 侧同一模式：MITM 告警不得被解析成指标。"""
    parsed, _raw = _build_item_result(item_type, "linux", 1, "", SSH_MITM)

    assert parsed["success"] is False
    assert parsed["status"] == "error"
    assert parsed["value"] is None
    assert "MITM" in parsed["error_message"]


def test_cpu_does_not_pick_up_timeout_number_as_usage():
    """精确钉住 25.0 这个假读数的来源：connect timeout=25。"""
    parsed, _ = _build_item_result("cpu", "windows", 1, "", WINRM_TIMEOUT)
    assert parsed["value"] is None
    assert parsed["status"] == "error"


def test_disk_failure_shape_has_no_dangling_unit():
    """disk 失败时 unit 必须为空。

    解析器自己的失败分支会带 unit="%"，前端据此渲染出悬空的「%」。
    走 _build_item_result 的失败路径时统一清空。
    """
    parsed, _ = _build_item_result("disk", "windows", 1, "", WINRM_TIMEOUT)
    assert parsed["unit"] is None
    assert parsed["value"] is None


def test_nonzero_exit_without_stderr_still_reports_error():
    parsed, _ = _build_item_result("cpu", "linux", 3, "", "")
    assert parsed["success"] is False
    assert parsed["status"] == "error"
    assert "3" in parsed["error_message"]


# ── 2. 成功路径：只解析 stdout ──


def test_success_parses_stdout():
    parsed, raw = _build_item_result("cpu", "windows", 0, "37.5\n", "")
    assert parsed["success"] is True
    assert parsed["value"] == "37.5"
    assert parsed["unit"] == "%"
    assert raw == "37.5\n"


def test_success_ignores_stderr_warning_stream():
    """exit_code=0 但 stderr 有告警时，不得把 stderr 当数据解析。

    PowerShell 常把 Write-Warning / 进度信息写进 std_err；旧代码的
    `stdout or stderr` 在 stdout 为空时会拿这些文本去解析。
    """
    stderr = "WARNING: some counter was not found (connect timeout=88)"
    parsed, raw = _build_item_result("cpu", "windows", 0, "", stderr)

    assert parsed["value"] is None  # 没有 88 这个假读数
    assert parsed["status"] == "error"
    # 原始输出仍展示 stderr，便于排查
    assert raw == stderr


def test_parser_failure_converges_status_to_error():
    """解析器自己报 success=False 时，status 必须跟着变 error。

    否则 normal_count 被污染，_derive_overall_status 会把 failed 判成 partial。
    """
    # df 输出无法解析（表头都没有）→ _parse_disk 返回 success=False
    parsed, _ = _build_item_result("disk", "linux", 0, "totally not df output", "")
    assert parsed["success"] is False
    assert parsed["status"] == "error"


def test_parser_failure_attaches_stderr_when_present():
    parsed, _ = _build_item_result(
        "disk", "linux", 0, "not df output", "df: some warning"
    )
    assert parsed["status"] == "error"
    assert parsed["error_message"] == "df: some warning"


# ── 3. 计数与整条记录结论 ──


def test_all_failed_windows_inspection_is_failed_not_partial():
    """端到端的计数断言：六项全挂必须得出 failed。"""
    counts = {"normal": 0, "warning": 0, "critical": 0, "error": 0}
    for item_type in WINDOWS_CORE:
        parsed, _ = _build_item_result(item_type, "windows", 1, "", WINRM_TIMEOUT)
        counts[parsed["status"]] += 1

    total = sum(counts.values())
    assert total == 6
    # 事故记录里是 normal=4 / warning=1 / error=1
    assert counts == {"normal": 0, "warning": 0, "critical": 0, "error": 6}
    assert _derive_overall_status(counts, total) == "failed"


def test_healthy_windows_inspection_is_completed():
    """对照组：真实成功的输出不能被误判成失败。"""
    outputs = {
        "cpu": "12.5\n",
        "memory": "43.2\n",
        "disk": "DeviceID SizeGB FreeGB UsagePct\nC: 100.0 57.5 42.5\n",
        "uptime": "\nDays Hours Minutes\n---- ----- -------\n   3    12      45\n",
        "services": "",
        "event_logs": "",
    }
    counts = {"normal": 0, "warning": 0, "critical": 0, "error": 0}
    for item_type in WINDOWS_CORE:
        parsed, _ = _build_item_result(item_type, "windows", 0, outputs[item_type], "")
        counts[parsed["status"]] += 1

    assert counts["error"] == 0
    assert _derive_overall_status(counts, len(WINDOWS_CORE)) == "completed"


# ── 4. _failed_item 自身 ──


def test_failed_item_shape():
    item = _failed_item("boom")
    assert item == {
        "success": False,
        "value": None,
        "unit": None,
        "status": "error",
        "details": None,
        "error_message": "boom",
    }


def test_failed_item_truncates_long_message():
    item = _failed_item("x" * 900)
    assert len(item["error_message"]) == 500


def test_failed_item_falls_back_on_empty_message():
    assert _failed_item("")["error_message"] == "命令执行失败"
