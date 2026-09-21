"""核心指标巡检模式（mode="core"）的契约测试。

自动化运维的健康巡检不再让用户在 quick / standard / custom 之间挑，而是固定跑一组
「服务器最关心的核心指标」——`CORE_ITEMS`。规模取 quick(5 项) 与 standard(全量 13 项)
之间的平均值：每个 target_type 恰好 6 项，构成都是「4 项性能 + 2 项健康」。

这里钉住四件事:
  1. CORE_ITEMS 自身一致 —— 引用的项都真实存在于对应命令注册表(防止改名后静默漏项);
  2. `get_commands(..., "core")` 的行为 —— 项数与 item_type 集合就是约定的那 6 项;
  3. quick / standard / custom 三种老模式不受影响(巡检中心前端仍在用);
  4. `InspectionRunRequest` 放行 `mode="core"`，非法 mode 依旧被拒。
"""

import pytest
from pydantic import ValidationError

from app.schemas.inspection import InspectionRunRequest
from app.services.inspection_commands import (
    CORE_ITEMS,
    ITEM_LABELS,
    LINUX_COMMANDS,
    QUICK_ITEMS,
    WINDOWS_COMMANDS,
    get_commands,
)

REGISTRIES = {"linux": LINUX_COMMANDS, "windows": WINDOWS_COMMANDS}

# 约定的「4 项性能 + 2 项健康」构成。windows 没有 load / logs，按语义对齐替代:
# uptime↔load、services↔failed_services、event_logs↔logs。
EXPECTED_CORE_ITEMS = {
    "linux": {"cpu", "memory", "disk", "load", "failed_services", "logs"},
    "windows": {"cpu", "memory", "disk", "uptime", "services", "event_logs"},
}


def _item_types(commands: list[dict]) -> set[str]:
    return {entry["item_type"] for entry in commands}


# ── 1. CORE_ITEMS 自身一致 ────────────────────────────────────────────────


def test_core_items_are_real_item_types():
    """CORE_ITEMS 引用的项必须在对应注册表里存在,否则核心巡检会静默少查几项。"""
    for target_type, items in CORE_ITEMS.items():
        available = set(REGISTRIES[target_type])
        unknown = sorted(set(items) - available)
        assert not unknown, f"{target_type} 的核心巡检项不存在: {unknown}"


def test_core_items_only_cover_host_target_types():
    """核心巡检只剩 linux/windows 两类主机,与 QUICK_ITEMS 保持一致。"""
    assert set(CORE_ITEMS) == {"linux", "windows"}, (
        f"CORE_ITEMS 的 target_type 应为 linux/windows，实际: {sorted(CORE_ITEMS)}"
    )


def test_core_items_match_the_agreed_six():
    """每类恰好 6 项,且构成与约定逐一对齐(顺序也算契约,前端按序展示)。"""
    for target_type, expected in EXPECTED_CORE_ITEMS.items():
        actual = CORE_ITEMS[target_type]
        assert len(actual) == 6, f"{target_type} 核心巡检应为 6 项，实际 {len(actual)}"
        assert len(set(actual)) == 6, f"{target_type} 核心巡检存在重复项: {actual}"
        assert set(actual) == expected, (
            f"{target_type} 核心巡检项漂移 —— 多出: {sorted(set(actual) - expected)}; "
            f"缺少: {sorted(expected - set(actual))}"
        )


def test_core_items_all_have_chinese_labels():
    """核心巡检项都会出现在任务步骤名里,没有中文标签就会露出原始英文键。"""
    missing = sorted(
        {item for items in CORE_ITEMS.values() for item in items} - set(ITEM_LABELS)
    )
    assert not missing, f"这些核心巡检项在 ITEM_LABELS 里没有标签: {missing}"


# ── 2. get_commands 的 core 分支 ──────────────────────────────────────────


def test_get_commands_core_linux():
    commands = get_commands("linux", "core")
    assert len(commands) == 6
    assert _item_types(commands) == EXPECTED_CORE_ITEMS["linux"]
    for entry in commands:
        assert entry["command"] == LINUX_COMMANDS[entry["item_type"]][0]
        assert entry["timeout"] == LINUX_COMMANDS[entry["item_type"]][1]


def test_get_commands_core_windows():
    commands = get_commands("windows", "core")
    assert len(commands) == 6
    assert _item_types(commands) == EXPECTED_CORE_ITEMS["windows"]
    for entry in commands:
        assert entry["command"] == WINDOWS_COMMANDS[entry["item_type"]][0]
        assert entry["timeout"] == WINDOWS_COMMANDS[entry["item_type"]][1]


def test_get_commands_core_unknown_target_type_falls_back_to_first_six():
    """CORE_ITEMS 未登记的 target_type 退回注册表前 6 项,规模仍与 core 一致。"""
    commands = get_commands("some_future_os", "core")
    assert len(commands) == 6
    assert _item_types(commands) == set(list(LINUX_COMMANDS)[:6])


def test_get_commands_core_ignores_custom_items():
    """core 是固定指标集,不接受用户再勾选,custom_items 必须被忽略。"""
    commands = get_commands("linux", "core", ["cpu"])
    assert _item_types(commands) == EXPECTED_CORE_ITEMS["linux"]


# ── 3. 老模式不受影响 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("target_type", ["linux", "windows"])
def test_legacy_modes_are_untouched(target_type):
    """巡检中心仍在用 quick/standard/custom,新增 core 不许改动它们的行为。"""
    registry = REGISTRIES[target_type]

    quick = get_commands(target_type, "quick")
    assert len(quick) == 5
    assert _item_types(quick) == set(QUICK_ITEMS[target_type])

    standard = get_commands(target_type, "standard")
    assert _item_types(standard) == set(registry)

    custom = get_commands(target_type, "custom", ["cpu", "not_a_real_item"])
    assert _item_types(custom) == {"cpu"}


# ── 4. 请求校验 ───────────────────────────────────────────────────────────


def test_core_mode_passes_request_validation():
    request = InspectionRunRequest(device_ids=[1], mode="core")
    assert request.mode == "core"


def test_invalid_mode_is_still_rejected():
    with pytest.raises(ValidationError) as excinfo:
        InspectionRunRequest(device_ids=[1], mode="turbo")
    message = str(excinfo.value)
    assert "mode must be" in message
    # 错误文案要同时列出四种合法模式,否则用户不知道 core 可用。
    for allowed in ("standard", "custom", "quick", "core"):
        assert allowed in message


# ── 5. Windows「服务异常」命令契约 ────────────────────────────────────────
#
# 这一项曾经枚举「StartType=Automatic 且没在运行」的服务，等于在数噪音：
# Get-Service 分不出「自动(延迟启动)」，sppsvc / wuauserv 干完活就退出永远在列，
# RemoteRegistry、spice-agent 本来就该停着。一台健康虚拟机轻松凑够 3 个，
# 被 THRESHOLDS(failed_services critical=3) 直接判成 critical。
# 现在改成查 Service Control Manager 的失败事件，这里钉住不许悄悄改回去。


def test_windows_services_command_queries_scm_failure_events():
    from app.services.inspection_commands import SCM_FAILURE_EVENT_IDS

    command = WINDOWS_COMMANDS["services"][0]

    assert "Service Control Manager" in command
    assert "Get-EventLog -LogName System" in command
    for event_id in SCM_FAILURE_EVENT_IDS:
        assert str(event_id) in command, f"事件 {event_id} 没有出现在查询里"


def test_windows_services_command_dropped_the_noise_based_query():
    """不许再退回枚举「停着的自动服务」——那正是误报 critical 的根源。"""
    command = WINDOWS_COMMANDS["services"][0]
    assert "Get-Service" not in command
    assert "StartType" not in command


def test_windows_services_command_is_locale_independent():
    """服务名取自 ReplacementStrings，不能去正则匹配已本地化的 Message。

    中文 Windows 上 Message 是中文，抽服务名的正则会整体失效。
    """
    command = WINDOWS_COMMANDS["services"][0]
    assert "ReplacementStrings" in command
    assert "$_.Message" not in command


def test_windows_services_command_scopes_to_a_time_window():
    """只看最近一段时间，否则三个月前的一次失败会永远挂在报告里。"""
    from app.services.inspection_commands import WINDOWS_SERVICE_FAILURE_WINDOW_HOURS

    command = WINDOWS_COMMANDS["services"][0]
    assert f"AddHours(-{WINDOWS_SERVICE_FAILURE_WINDOW_HOURS})" in command


def test_windows_services_command_emits_the_parser_contract():
    """输出形状必须是 发生次数|事件ID|服务名，解析器按这个契约切分。"""
    command = WINDOWS_COMMANDS["services"][0]
    assert "Group-Object" in command
    assert "'{0}|{1}'" in command


def test_windows_services_item_is_labelled_as_anomaly_not_failure():
    """语义变了，标签也要跟着变：数的是真故障，不是「停着的自动服务」。"""
    assert ITEM_LABELS["services"] == "服务异常"
