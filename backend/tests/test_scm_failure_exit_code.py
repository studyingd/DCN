"""WINDOWS_SCM_FAILURE_COMMAND 的退出码契约。

PowerShell 的 Get-EventLog 在「无匹配事件」时即使带 -ErrorAction
SilentlyContinue 也会把进程退出码置 1(SilentlyContinue 只压错误输出,
不影响 $LASTEXITCODE)。巡检对 exit!=0 一律按命令失败处理,于是健康的
机器(24h 无服务失败事件)反而显示「服务异常: 命令退出码 1」的 error 项
(2026-09-15 STUshareWinserver 实测)。命令尾部必须显式 exit 0。
"""

from app.services.inspection_commands import WINDOWS_SCM_FAILURE_COMMAND
from app.services.inspection_parser import parse_item


def test_scm_command_ends_with_explicit_exit_zero():
    assert WINDOWS_SCM_FAILURE_COMMAND.rstrip().endswith("exit 0")


def test_parse_services_empty_output_is_healthy():
    """命令成功但输出为空 → 0 个失败服务(normal),不是 error。"""
    parsed = parse_item("services", "", "windows")
    assert parsed["success"] is True
    assert parsed["value"] == "0"
    assert parsed["status"] == "normal"
    assert parsed["details"]["failed_count"] == 0
