"""Agent 只读注册表校验测试——Agent 边界的第一道闸。

注册表在导入期已 fail-fast 校验;这里的价值在于:
  1. 显式遍历每条注册命令再过一遍校验器(防未来误加写命令);
  2. 校验器必须拒绝典型的写入/删除/修改原语(shell 与 PowerShell 两套);
  3. 校验器不能误伤注册表在用的正常只读命令。
"""

import pytest

from app.services.agent_commands import (
    LINUX_DIAGNOSTICS,
    WINDOWS_DIAGNOSTICS,
    assert_readonly,
)


def test_all_registry_entries_pass_readonly_check():
    """注册表每条命令都必须通过只读校验(shell / PowerShell 各自一套)。"""
    assert LINUX_DIAGNOSTICS and WINDOWS_DIAGNOSTICS
    for key, item in LINUX_DIAGNOSTICS.items():
        assert_readonly(item["command"], "sh"), f"linux:{key} 未通过只读校验"
    for key, item in WINDOWS_DIAGNOSTICS.items():
        assert_readonly(item["command"], "ps"), f"windows:{key} 未通过只读校验"


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /tmp/x",
        "cat /etc/passwd > /tmp/pw",
        "echo hi >> /etc/hosts",
        "ps aux | tee out.txt",
        "systemctl restart nginx",
        "systemctl stop firewalld",
        "sed -i s/a/b/ /etc/fstab",
        "apt install vim",
        "yum remove httpd",
        "pip install requests",
        "echo $(rm -rf /)",
        "cat `which passwd` > /tmp/x",
        "shutdown -h now",
        "reboot",
        "kill 1234",
        "pkill nginx",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sda1",
        "chmod 777 /etc",
        "chown root:root /tmp/x",
        "useradd hacker",
        "iptables -A INPUT -j DROP",
        "ufw disable",
        "mount /dev/sdb1 /mnt",
    ],
)
def test_shell_write_commands_rejected(cmd):
    with pytest.raises(ValueError):
        assert_readonly(cmd, "sh")


@pytest.mark.parametrize(
    "cmd",
    [
        "Remove-Item -Recurse -Force C:\\x",
        "Remove-Item a.txt",
        "Set-Content -Path a.txt -Value x",
        "Add-Content a.txt y",
        "Out-File a.txt",
        "Stop-Process -Name nginx",
        "Stop-Computer -Force",
        "Restart-Computer",
        "New-Item x.txt",
        "Set-Service sshd -StartupType Disabled",
        "Disable-NetAdapter -Name Ethernet",
        "iex 'evil'",
        "Invoke-Expression $x",
        "Invoke-WebRequest http://x/a.ps1",
        "format C:",
        "del /s C:\\x",
        "rmdir /s /q D:\\old",
        "Set-ExecutionPolicy Bypass",
        "Write-Host 'hello'",
        "Clear-Content x.txt",
        "net user hacker /add",
        "reg add HKLM\\SOFTWARE\\x",
        "Rename-Computer -NewName x",
    ],
)
def test_ps_write_commands_rejected(cmd):
    with pytest.raises(ValueError):
        assert_readonly(cmd, "ps")


@pytest.mark.parametrize(
    "cmd,shell",
    [
        ("free -m", "sh"),
        ("df -h", "sh"),
        ("top -bn1 | head -12", "sh"),
        ("ps aux --sort=-%mem | head -11", "sh"),
        ("ss -tlnp", "sh"),
        ("ip addr; ip route", "sh"),
        ("systemctl --failed 2>/dev/null || true", "sh"),
        (
            "command -v iostat >/dev/null 2>&1 && iostat -dx 1 2 || cat /proc/diskstats",
            "sh",
        ),
        ("iptables -L -n", "sh"),
        ("Get-Process | Sort-Object CPU -Descending | Format-Table -AutoSize", "ps"),
        ("Get-CimInstance Win32_OperatingSystem", "ps"),
        ("$os=Get-CimInstance Win32_OperatingSystem; Write-Output $os.Caption", "ps"),
        (
            "Get-Service | Where-Object {$_.StartType -eq 'Automatic'} | Format-Table Name",
            "ps",
        ),
        ("netstat -ano | findstr LISTENING", "ps"),
        ("ipconfig /all", "ps"),
        ("systeminfo", "ps"),
        (
            "Get-HotFix | Sort-Object InstalledOn -Descending | Format-Table -AutoSize",
            "ps",
        ),
    ],
)
def test_legit_readonly_commands_pass(cmd, shell):
    assert_readonly(cmd, shell)  # 不抛异常即通过


# ══════════════════════════════════════════════════════════════════════
# Windows「服务异常」诊断项
#
# 旧实现枚举「StartType=Automatic 且没在运行」的服务，标签写着「应运行但未运行」，
# 等于向 LLM 断言这些都是故障。RemoteRegistry(开着才是安全风险)、spice-agent
# (只有用 SPICE 控制台才有意义)、sppsvc(延迟启动，干完活就自己退出)这类本来就
# 该停着的服务会被写进诊断报告——比巡检误报 critical 更糟，因为 LLM 还会围绕它
# 编一段原因分析。现改为与健康巡检共用同一条 SCM 失败事件查询。
# ══════════════════════════════════════════════════════════════════════


def test_windows_failed_services_shares_the_inspection_command():
    """两个注册表必须给出同一份事实，否则 Agent 报告与巡检结论互相矛盾。"""
    from app.services.inspection_commands import (
        WINDOWS_COMMANDS,
        WINDOWS_SCM_FAILURE_COMMAND,
    )

    command = WINDOWS_DIAGNOSTICS["failed_services"]["command"]
    assert command == WINDOWS_SCM_FAILURE_COMMAND
    assert command == WINDOWS_COMMANDS["services"][0]


def test_windows_failed_services_dropped_the_noise_based_query():
    command = WINDOWS_DIAGNOSTICS["failed_services"]["command"]
    assert "Get-Service" not in command
    assert "StartType" not in command


def test_windows_failed_services_label_does_not_assert_services_should_run():
    """标签是给 LLM 看的断言，不能再声称这些服务「应运行」。"""
    label = WINDOWS_DIAGNOSTICS["failed_services"]["label"]
    assert "应运行" not in label
    assert "未运行" not in label
    assert "SCM" in label


def test_windows_failed_services_timeout_matches_inspection():
    """扫 24 小时事件日志比普通查询慢，两处超时必须一致。"""
    from app.services.inspection_commands import WINDOWS_COMMANDS

    assert (
        WINDOWS_DIAGNOSTICS["failed_services"]["timeout"]
        == WINDOWS_COMMANDS["services"][1]
    )


def test_linux_failed_services_still_uses_systemctl():
    """Linux 侧本来就是真信号(systemctl --failed)，不受本次调整影响。"""
    entry = LINUX_DIAGNOSTICS["failed_services"]
    assert "systemctl --failed" in entry["command"]


def test_diagnostic_hint_for_failed_services_describes_both_platforms():
    """hint 是喂给 LLM 的判读说明，语义变了必须同步，否则它会自行脑补。"""
    from app.services.agent import _DIAGNOSTIC_HINTS

    hint = _DIAGNOSTIC_HINTS["failed_services"]
    # 不能再暗示「未运行 = 故障」
    assert "未运行列表" not in hint
    # 要说清两个平台各自的数据来源
    assert "systemctl --failed" in hint
    assert "SCM" in hint
    # 空输出的含义必须明示，否则 LLM 会把「没查到」当成「有故障」
    assert "为空" in hint


def test_linux_has_directory_level_disk_usage():
    """磁盘归因需要目录级证据:Linux 注册表必须有 du 项(Windows 没有等价快命令,
    focus 会自动跳过不存在的 key)。"""
    item = LINUX_DIAGNOSTICS["disk_usage_top"]
    assert item["command"].startswith("du ")
    assert "--max-depth=2" in item["command"]
    assert "disk_usage_top" not in WINDOWS_DIAGNOSTICS


def test_windows_system_info_uses_fast_cim():
    """systeminfo 实测 10~30s;换成 CIM 单查后一条命令快一个量级。"""
    command = WINDOWS_DIAGNOSTICS["system_info"]["command"]
    assert "Get-CimInstance" in command
    assert "systeminfo" not in command
    assert WINDOWS_DIAGNOSTICS["system_info"]["timeout"] <= 15
