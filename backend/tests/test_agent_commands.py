"""Agent 只读注册表校验测试——Agent 边界的第一道闸。

注册表在导入期已 fail-fast 校验;这里的价值在于:
  1. 显式遍历每条注册命令再过一遍校验器(防未来误加写命令);
  2. 校验器必须拒绝典型的写入/删除/修改原语(shell 与 PowerShell 两套);
  3. 校验器不能误伤注册表在用的正常只读命令。
"""

import pytest

from app.services import agent_commands as ac
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
        # 无空格分隔符的真实写命令也必须拒(边界收紧后约回归)
        "echo hi;rm -rf /tmp",
        "echo hi&&mv /tmp/a /tmp/b",
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
        # Format-* 写命令仍拦(豁免名单只有输出格式化家族)
        "Format-Volume -DriveLetter C",
        "Get-Volume | Format-Volume",
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
        # 参数位名字/路径含形似写原语的片段不误伤(run 893:容器名 document-sync-worker
        # 的 sync 被 \b 切成独立词误拒;同类的 restart-worker/curl-proxy/rm-backup)
        ("docker inspect argus-production-document-sync-worker-1", "sh"),
        ("docker logs --tail 40 file-sync-worker-1", "sh"),
        ("ls -la /data/sync", "sh"),
        ("cat /data/rm-backup/config.yaml", "sh"),
        ("docker logs --tail 40 curl-proxy-1", "ps"),
        # Format-List 与 Format-Table 同为输出格式化(纯只读),曾误拦参数化
        # 原语 process_detail 的 Windows 模板(实测:只读校验失败命中写原语
        # 'Format-' 正则)——只读 Format 全家 Table/List/Wide/Custom/Hex 都豁免
        (
            "Get-CimInstance Win32_Process -Filter 'ProcessId=4212' | Format-List Name,ProcessId,CommandLine",
            "ps",
        ),
        ("Get-Service | Format-List -Property Name,Status", "ps"),
        ("Get-Process | Format-Wide -Column 4", "ps"),
    ],
)
def test_legit_readonly_commands_pass(cmd, shell):
    assert_readonly(cmd, shell)  # 不抛异常即通过


def test_docker_processes_degrades_without_ps():
    """run 复盘(7/7 误报):精简镜像容器内无 ps,docker exec 非零退出曾把整步
    拖成 ok:false——PID1 半段证据其实已到手。进程段必须降级为文本标注,
    不让单容器缺 ps 决定整步成败。"""
    cmd = ac.LINUX_DIAGNOSTICS["docker_processes"]["command"]
    assert "|| echo [no ps in $1" in cmd  # 缺 ps 降级标注(免引号文本)
    # 逐容器执行必须经 sh -c 定位参数($1),容器名不内插进脚本体
    assert "sh -c" in cmd and "sh {}" in cmd
    # 模板整体仍过只读屏(注册期同款校验)
    assert_readonly(cmd, "sh")


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


def test_process_list_pinned_commands():
    """全量进程清单:免审固定项(2026-10-30 批次 A)。命令钉死——
    Windows tasklist 无命令行列无敏感参数;Linux 用 comm 不用 args
    (进程参数可能带口令),两者都只提供名称↔PID 映射。"""
    assert WINDOWS_DIAGNOSTICS["process_list"]["command"] == "tasklist /FO CSV /NH"
    assert (
        LINUX_DIAGNOSTICS["process_list"]["command"]
        == "ps -eo pid,user,pcpu,pmem,comm --sort=pid"
    )


def test_messages_error_count_covers_rhel_and_debian_paths():
    """文件式日志错误计数:RHEL(messages)与 Debian(syslog)双路径,
    取首条可读文件;journalctl 持久化缺失时兑底 app_errors。"""
    cmd = LINUX_DIAGNOSTICS["messages_error_count"]["command"]
    assert "/var/log/messages" in cmd
    assert "/var/log/syslog" in cmd
    assert "grep -icE" in cmd
    assert "messages_error_count" not in WINDOWS_DIAGNOSTICS


def test_docker_readonly_inventory_items_pinned():
    """docker 只读指令梳理(2026-10-30 第三批):镜像/网络清单为固定项,
    两侧平台都有守卫(未装 Docker 输出提示而不是报错)。"""
    for table, guard in (
        (LINUX_DIAGNOSTICS, "command -v docker"),
        (WINDOWS_DIAGNOSTICS, "Get-Command docker"),
    ):
        assert "docker images --format" in table["docker_images"]["command"]
        assert "docker network ls" in table["docker_network_ls"]["command"]
        assert guard in table["docker_images"]["command"]
        assert guard in table["docker_network_ls"]["command"]
