"""WinRM PowerShell prelude 注入测试。

prelude 管两件不相干的事，必须各自独立守卫：

* **输出编码** —— 中文 Windows 控制台默认 GBK，不设置 UTF-8 时 stdout 解码乱码；
* **进度流抑制** —— 经 WinRM 执行时 PowerShell 会把 Progress 流序列化成 CLIXML
  塞进 stderr。「命令成功但 stdout 为空」往往正是健康结论(例如近 24h 无服务
  失败事件)，调用方一旦回退到 stderr，喂给解析器/LLM 的就是几百字符 XML 噪音。

旧实现只有一个 ``if "OutputEncoding" not in script`` 守卫，把两件事耦合在一起：
调用方自己设了 OutputEncoding，进度抑制就会被一并跳过。
"""

from app.services.winrm import (
    _PS_QUIET_PRELUDE,
    _PS_UTF8_PRELUDE,
    _apply_prelude,
)


def test_both_preludes_injected_for_a_plain_script():
    out = _apply_prelude("Get-Service")
    assert _PS_UTF8_PRELUDE in out
    assert _PS_QUIET_PRELUDE in out
    # prelude 必须在原脚本之前
    assert out.index(_PS_QUIET_PRELUDE) < out.index("Get-Service")


def test_progress_suppression_is_set_to_silently_continue():
    assert _PS_QUIET_PRELUDE == "$ProgressPreference='SilentlyContinue'"


def test_utf8_prelude_skipped_when_caller_sets_encoding():
    script = "[Console]::OutputEncoding=[System.Text.Encoding]::ASCII; Get-Service"
    out = _apply_prelude(script)
    assert _PS_UTF8_PRELUDE not in out
    # 关键：编码被跳过时，进度抑制仍须独立注入
    assert _PS_QUIET_PRELUDE in out


def test_progress_prelude_skipped_when_caller_sets_it():
    script = "$ProgressPreference='Continue'; Write-Progress -Activity x"
    out = _apply_prelude(script)
    assert out.count("ProgressPreference") == 1
    assert _PS_UTF8_PRELUDE in out


def test_no_prelude_when_caller_handles_both():
    script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8\n"
        "$ProgressPreference='SilentlyContinue'\n"
        "Get-Service"
    )
    assert _apply_prelude(script) == script


def test_original_script_is_preserved_verbatim():
    """prelude 只能前置，绝不能改动调用方脚本本身。"""
    script = "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Format-Table"
    out = _apply_prelude(script)
    assert out.endswith(script)


def test_registry_commands_all_get_progress_suppression():
    """注册表里的真实命令都必须被抑制进度流，否则 CLIXML 会污染巡检与 Agent。"""
    from app.services.agent_commands import LINUX_DIAGNOSTICS, WINDOWS_DIAGNOSTICS
    from app.services.inspection_commands import LINUX_COMMANDS, WINDOWS_COMMANDS

    windows = [cmd for cmd, _ in WINDOWS_COMMANDS.values()]
    windows += [item["command"] for item in WINDOWS_DIAGNOSTICS.values()]
    assert windows
    for cmd in windows:
        assert _PS_QUIET_PRELUDE in _apply_prelude(cmd), cmd[:60]

    # Linux 命令走 SSH，不经 prelude；这里只确认没被误改
    for cmd, _ in LINUX_COMMANDS.values():
        assert _apply_prelude(cmd).endswith(cmd)
    assert LINUX_DIAGNOSTICS
