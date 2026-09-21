"""巡检结果解析的回归测试。

失败服务是核心巡检(core)的固定项之一，解析错了会同时造成两种事故：健康机器
误报 critical、真有服务挂掉时反而漏报。这里用 systemctl --failed 与 Windows
Service Control Manager 失败事件的真实输出形态钉住计数与状态。

两个平台数的是不同东西，不能混：
  * Linux  —— systemctl 列出的失败单元行(以 "●" 起头)；
  * Windows —— 事件日志里的 SCM 失败事件，输出为 `次数|事件ID|服务名`。
    旧实现枚举「StartType=Automatic 且没在运行」的服务，把 RemoteRegistry、
    spice-agent、sppsvc 这类本来就该停着的服务数成故障，健康虚拟机被误判 critical。
"""

import pytest

from app.services.inspection_parser import parse_item

# systemctl --failed 在没有失败单元时只打印这一行提示，它不是错误信息。
HEALTHY_LINUX = (
    "0 loaded units listed. Pass --all to see loaded but inactive units, too. "
    "To show all installed unit files use 'systemctl list-unit-files'."
)

# 有失败单元时，systemctl 会额外给出表头、LOAD/ACTIVE/SUB 图例和两行尾注，
# 失败单元行本身以 "●" 起头。
ONE_FAILED_LINUX = """  UNIT                        LOAD   ACTIVE SUB    DESCRIPTION
\u25cf postgresql@14-main.service  loaded failed failed PostgreSQL Cluster 14-main

LOAD   = Reflects whether the unit definition was properly loaded.
ACTIVE = The high-level unit activation state, i.e. generalization of SUB.
SUB    = The low-level unit activation state, values depend on unit type.

1 loaded units listed. Pass --all to see loaded but inactive units, too.
To show all installed unit files use 'systemctl list-unit-files'."""

THREE_FAILED_LINUX = """  UNIT                   LOAD   ACTIVE SUB    DESCRIPTION
\u25cf nginx.service          loaded failed failed A high performance web server
\u25cf redis-server.service   loaded failed failed Advanced key-value store
\u25cf data.mount             loaded failed failed /data

LOAD   = Reflects whether the unit definition was properly loaded.
ACTIVE = The high-level unit activation state, i.e. generalization of SUB.
SUB    = The low-level unit activation state, values depend on unit type.

3 loaded units listed. Pass --all to see loaded but inactive units, too.
To show all installed unit files use 'systemctl list-unit-files'."""

# 非 tty 下部分 systemd 版本不打 "●" 标记，计数不能依赖这个前缀。
NO_MARKER_LINUX = """  UNIT             LOAD   ACTIVE SUB    DESCRIPTION
nginx.service      loaded failed failed A high performance web server

1 loaded units listed. Pass --all to see loaded but inactive units, too."""


def _unit_names(result: dict) -> list[str]:
    return [line.split()[0] for line in result["details"]["services"]]


def test_linux_no_failed_service_is_normal():
    result = parse_item("failed_services", HEALTHY_LINUX, "linux")
    assert result["success"] is True
    assert (result["value"], result["unit"], result["status"]) == ("0", "个", "normal")
    assert result["details"]["failed_count"] == 0
    assert result["details"]["services"] == []


def test_linux_single_failed_service_counts_only_the_unit():
    """旧实现会把 3 行图例 + 1 行尾注数成 4 个失败服务，并丢掉真正的单元行。"""
    result = parse_item("failed_services", ONE_FAILED_LINUX, "linux")
    # 1 个失败服务落在 THRESHOLDS 的 warning 档（>=3 才是 critical）
    assert (result["value"], result["status"]) == ("1", "warning")
    assert _unit_names(result) == ["postgresql@14-main.service"]


def test_linux_multiple_failed_services_include_non_service_units():
    result = parse_item("failed_services", THREE_FAILED_LINUX, "linux")
    assert (result["value"], result["status"]) == ("3", "critical")
    assert _unit_names(result) == [
        "nginx.service",
        "redis-server.service",
        "data.mount",
    ]


def test_linux_failed_service_without_bullet_marker():
    result = parse_item("failed_services", NO_MARKER_LINUX, "linux")
    assert (result["value"], result["status"]) == ("1", "warning")
    assert _unit_names(result) == ["nginx.service"]


def test_windows_no_scm_failure_is_normal():
    result = parse_item("services", "", "windows")
    assert (result["value"], result["unit"], result["status"]) == ("0", "个", "normal")
    assert result["details"]["services"] == []
    assert result["details"]["failed_count"] == 0


def test_windows_stopped_automatic_services_are_no_longer_failures():
    """回归：旧实现把「停着的自动服务」当故障，一台健康虚拟机直接被判 critical。

    这三行就是 SRM-APP 那台机器的真实输出：RemoteRegistry 本来就该关(开着
    才是安全风险)、spice-agent 只在用 SPICE 控制台时才有意义、sppsvc 是延迟
    启动的授权服务，干完活就自己退出。旧逻辑数到 3 个 → critical。
    """
    raw = """
Name           DisplayName          Status
----           -----------          ------
RemoteRegistry Remote Registry     Stopped
spice-agent    Spice Agent         Stopped
sppsvc         Software Protection Stopped
"""
    result = parse_item("services", raw, "windows")
    assert result["value"] == "0"
    assert result["status"] == "normal"
    assert result["details"]["services"] == []


def test_windows_single_failing_service_is_warning():
    result = parse_item("services", "2|7000|FooSvc\n", "windows")
    assert (result["value"], result["status"]) == ("1", "warning")
    assert result["details"]["failed_count"] == 1


def test_windows_crash_loop_counts_as_one_service():
    """一个服务崩溃 50 次也是「1 个服务有问题」，不能被放大成 critical。

    发生次数不丢：留在 details 的文案里，严重程度可见。
    """
    result = parse_item("services", "50|7031|sppsvc\n", "windows")
    assert result["value"] == "1"
    assert result["status"] == "warning"
    assert result["details"]["event_count"] == 50
    assert "50 次" in result["details"]["services"][0]


def test_windows_same_service_multiple_event_types_counts_once():
    """7031 与 7034 常常成对出现，它们是同一个服务的一次故障。"""
    result = parse_item("services", "3|7031|Spooler\n1|7034|Spooler\n", "windows")
    assert result["value"] == "1"
    assert result["details"]["event_count"] == 4


def test_windows_three_distinct_services_is_critical():
    raw = "2|7000|Foo\n1|7009|Bar\n1|7031|Baz\n"
    result = parse_item("services", raw, "windows")
    assert result["value"] == "3"
    assert result["status"] == "critical"


def test_windows_details_carry_event_semantics():
    """详情里给中文语义而不是裸事件 ID，否则运维还得去查 7009 是什么。"""
    result = parse_item("services", "1|7009|Bar\n", "windows")
    line = result["details"]["services"][0]
    assert "Bar" in line
    assert "超时" in line


def test_windows_busiest_service_is_listed_first():
    raw = "1|7031|Quiet\n40|7031|Noisy\n"
    result = parse_item("services", raw, "windows")
    assert result["details"]["services"][0].startswith("Noisy")


def test_windows_missing_service_name_is_still_counted():
    """ReplacementStrings 缺失时不能静默丢掉一次真实故障。"""
    result = parse_item("services", "1|7000|\n", "windows")
    assert result["value"] == "1"
    assert "7000" in result["details"]["services"][0]


def test_windows_ignores_malformed_lines():
    raw = "not-a-number|7000|Foo\n1|abc|Bar\ngarbage\n2|7031|Real\n"
    result = parse_item("services", raw, "windows")
    assert result["value"] == "1"
    assert "Real" in result["details"]["services"][0]


def test_windows_service_name_may_contain_pipe():
    """只切前两个分隔符，服务名里的 '|' 不会被当成列边界。"""
    result = parse_item("services", "1|7024|My|Service\n", "windows")
    assert result["value"] == "1"
    assert "My|Service" in result["details"]["services"][0]


def test_windows_details_expose_the_time_window():
    from app.services.inspection_commands import WINDOWS_SERVICE_FAILURE_WINDOW_HOURS

    result = parse_item("services", "1|7000|Foo\n", "windows")
    assert result["details"]["window_hours"] == WINDOWS_SERVICE_FAILURE_WINDOW_HOURS


def _failed_output(count: int) -> str:
    """按 systemctl --failed 的真实排版造 count 个失败单元的输出。"""
    rows = "\n".join(
        f"\u25cf svc{i}.service  loaded failed failed Service {i}" for i in range(count)
    )
    return (
        "  UNIT   LOAD   ACTIVE SUB    DESCRIPTION\n"
        f"{rows}\n"
        "\n"
        "LOAD   = Reflects whether the unit definition was properly loaded.\n"
        "ACTIVE = The high-level unit activation state, i.e. generalization of SUB.\n"
        f"{count} loaded units listed. Pass --all to see loaded but inactive units, too.\n"
        "To show all installed unit files use 'systemctl list-unit-files'."
    )


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "normal"), (1, "warning"), (2, "warning"), (3, "critical"), (5, "critical")],
)
def test_failed_service_severity_follows_thresholds(count, expected):
    """THRESHOLDS 里 failed_services 的分级以前是死配置，现在必须真正生效。"""
    result = parse_item("failed_services", _failed_output(count), "linux")
    assert result["value"] == str(count)
    assert result["details"]["failed_count"] == count
    assert result["status"] == expected


# ══════════════════════════════════════════════════════════
# uptime（Windows）
# ══════════════════════════════════════════════════════════

# 当前命令 `(Get-Date)-$bt | Select-Object Days,Hours,Minutes` 的真实输出形状：
# 表头一行、分隔线一行、数字一行。旧解析器的正则要求「数字在前」，对这种表格
# 永远匹配不上，于是无论机器跑了多久都返回 0天0时0分 并且报 normal。
UPTIME_TABLE = """
Days Hours Minutes
---- ----- -------
   3    12      45
"""


def test_windows_uptime_parses_select_object_table():
    result = parse_item("uptime", UPTIME_TABLE, "windows")
    assert result["success"] is True
    assert result["value"] == "3天12时45分"
    assert result["status"] == "normal"
    assert result["details"]["total_hours"] == 3 * 24 + 12


def test_windows_uptime_table_is_column_order_independent():
    """按表头列序取值，不能假定一定是 Days/Hours/Minutes 的顺序。"""
    raw = """
Minutes Hours Days
------- ----- ----
     45    12    3
"""
    result = parse_item("uptime", raw, "windows")
    assert result["value"] == "3天12时45分"


def test_windows_uptime_under_one_day():
    raw = """
Days Hours Minutes
---- ----- -------
   0     3      20
"""
    result = parse_item("uptime", raw, "windows")
    assert result["success"] is True
    assert result["value"] == "0天3时20分"
    assert result["details"]["total_hours"] == 3


def test_windows_uptime_still_accepts_natural_language():
    """兼容旧形状，避免改了命令输出格式之外的调用方。"""
    result = parse_item("uptime", "3 Days, 12 Hours, 45 Minutes", "windows")
    assert result["success"] is True
    assert result["value"] == "3天12时45分"


def test_windows_uptime_unparsable_output_reports_failure():
    """认不出来时必须如实失败，不能再返回 0天0时0分 + normal。

    这正是 WinRM 超时那台机器上 uptime 显示「0天0时0分 正常」的原因：
    错误信息里没有 Days/Hours/Minutes，三个正则全部落空，却仍然报成功。
    """
    for raw in ("", "   ", "❌ WinRM (10.0.0.1:5985): connect timeout=25"):
        result = parse_item("uptime", raw, "windows")
        assert result["success"] is False, raw
        assert result["value"] is None, raw
        assert result["status"] == "error", raw


# ── 异常日志：sshd 握手噪声过滤 ──

# 用户真实日志摘录：平台监控探针每 ~32s 在被探主机 sshd 里留下一条
# kex_exchange_identification，另有一条扫描器留下的 invalid banner。
_SSHD_NOISE_SAMPLE = """Sep 10 20:46:21 iZt4nbfz sshd[3588144]: error: kex_exchange_identification: Connection closed by remote host
Sep 10 20:46:53 iZt4nbfz sshd[3588272]: error: kex_exchange_identification: Connection closed by remote host
Sep 10 20:49:11 iZt4nbfz sshd[3588780]: error: kex_exchange_identification: banner line contains invalid characters
Sep 10 20:49:33 iZt4nbfz sshd[3588861]: error: kex_exchange_identification: Connection closed by remote host"""


def test_logs_pure_sshd_noise_counts_zero_and_stays_normal():
    """全是探针/扫描噪声时不该报 warning——那不是主机故障。"""
    result = parse_item("logs", _SSHD_NOISE_SAMPLE, "linux")
    assert result["value"] == "0"
    assert result["status"] == "normal"
    assert result["details"]["filtered_noise"] == 4
    assert result["details"]["error_count"] == 0


def test_logs_mixed_counts_only_real_errors():
    real = "Sep 10 21:00:00 iZt4nbfz kernel: Out of memory: Killed process 1234 (java)"
    raw = _SSHD_NOISE_SAMPLE + "\n" + real
    result = parse_item("logs", raw, "linux")
    assert result["value"] == "1"
    assert result["status"] == "normal"
    assert result["details"]["filtered_noise"] == 4
    assert result["details"]["error_count"] == 1


# 用户真实日志摘录：PVE 宿主机上 agent 未安装的 VM，被平台 QGA 轮询
# （失败退避 30min）每半小时打出一条 err 级 guest-ping 超时，24h 持续累积；
# journalctl | tail -20 全被它占满，旧行为计数 20 > 10 → 常年 warning。
_PVE_AGENT_NOISE_SAMPLE = """Sep 21 00:39:52 seeed pvedaemon[65303]: VM 118 qmp command failed - VM 118 qmp command 'guest-ping' failed - got timeout
Sep 21 01:10:00 seeed pvedaemon[4128520]: VM 118 qmp command failed - VM 118 qmp command 'guest-ping' failed - got timeout
Sep 21 02:40:50 seeed pveupdate[169149]: command 'apt-get update' failed: exit code 100
Sep 21 09:11:59 seeed pvedaemon[65303]: VM 118 qmp command failed - VM 118 qmp command 'guest-ping' failed - got timeout"""


def test_logs_pve_agent_ping_noise_does_not_warn():
    """guest-ping 超时是慢性配置状态且由平台轮询自己触发，不该报 warning。"""
    result = parse_item("logs", _PVE_AGENT_NOISE_SAMPLE, "linux")
    assert result["value"] == "1"  # 只剩 apt-get update 这条真实日志
    assert result["status"] == "normal"
    assert result["details"]["filtered_noise"] == 3
    assert result["details"]["error_count"] == 1


def test_logs_pve_noise_alone_keeps_pure_real_error_signal():
    """把噪声拉满 20 条（journalctl | tail -20 的上限）也不能单独触发 warning。"""
    flood = "\n".join(
        "Sep 21 %02d:%02d:00 seeed pvedaemon[65303]: VM 118 qmp command failed "
        "- VM 118 qmp command 'guest-ping' failed - got timeout" % (h, m)
        for h, m in (
            (0, 39),
            (1, 10),
            (1, 40),
            (2, 10),
            (2, 40),
            (3, 10),
            (3, 40),
            (4, 10),
            (4, 40),
            (5, 10),
            (5, 40),
            (6, 11),
            (6, 41),
            (7, 11),
            (7, 41),
            (8, 11),
            (8, 41),
            (9, 11),
            (9, 41),
            (10, 11),
        )
    )
    result = parse_item("logs", flood, "linux")
    assert result["value"] == "0"
    assert result["status"] == "normal"
    assert result["details"]["filtered_noise"] == 20


def test_logs_real_errors_still_warn_over_threshold():
    """真实错误超过阈值仍要 warning——过滤不能把真问题也滤掉。"""
    real = "\n".join(
        f"Sep 10 21:00:{i:02d} host kernel: I/O error, dev sda" for i in range(12)
    )
    raw = _SSHD_NOISE_SAMPLE + "\n" + real
    result = parse_item("logs", raw, "linux")
    assert result["value"] == "12"
    assert result["status"] == "warning"
    assert result["details"]["filtered_noise"] == 4


def test_logs_without_noise_unchanged():
    """无噪声时行为与改动前完全一致（回归）。"""
    raw = "line one\nline two"
    result = parse_item("logs", raw, "linux")
    assert result["value"] == "2"
    assert result["status"] == "normal"
    assert result["details"]["filtered_noise"] == 0


def test_logs_empty_still_normal():
    result = parse_item("logs", "", "linux")
    assert result["value"] == "0"
    assert result["status"] == "normal"


def test_logs_raw_output_preserved_for_security_clues():
    """原始输出保留噪声：扫描 22 端口这类安全线索仍可在「原始输出」里看到。"""
    result = parse_item("logs", _SSHD_NOISE_SAMPLE, "linux")
    assert "kex_exchange_identification" in result["details"]["output"]


# ══════════════════════════════════════════════════════════
# 中文 locale 回归（2026-09-18）
# ══════════════════════════════════════════════════════════
# procps 只翻译 free/top/uptime 的行标签与提示语，表头与缩写（us/sy/id）不译。
# 中文系统的真实输出：free 的行标签是「内存：/交换：」（全角冒号），top 的
# CPU 行冒号变全角「%Cpu(s)：」，uptime 是「平均负载：」。旧解析只认英文
# 关键字，中文机器上 memory/cpu/load 三项全部解析失败 → 巡检误报「错误」。
# 修复双管齐下：命令前置 LC_ALL=C（见 inspection_commands）+ 解析器兼容
# 本地化行。这里钉的是后者——即使环境变量被剥（受限 shell 等）也能解析。

ZH_FREE_M = """              total        used        free      shared  buff/cache   available
内存：      15856        1587         246         264       14022       13674
交换：       2047           0        2047"""

ZH_TOP = """top - 10:30:00 up 30 days,  3:15,  1 user,  load average: 0.08, 0.03, 0.05
任务:  123 total,   1 running, 122 sleeping,   0 stopped,   0 zombie
%Cpu(s)：  0.3 us,  0.3 sy,  0.0 ni, 99.3 id,  0.0 wa,  0.0 hi,  0.1 si,  0.0 st
MiB Mem :  15856.0 total,    246.0 free,   1587.0 used,  14022.0 buff/cache
MiB Swap:   2047.0 total,      0.0 free,   2047.0 used.  13674.0 avail Mem"""

ZH_UPTIME = " 10:30:00 up 30 days,  3:15,  1 user,  平均负载： 0.08, 0.03, 0.05"


def test_memory_parses_chinese_locale_free():
    """中文 locale 的 free -m（内存：/交换：）必须能算出使用率而不是报错。"""
    result = parse_item("memory", ZH_FREE_M, "linux")
    assert result["success"] is True
    assert result["value"] == "10.0"
    assert result["details"]["total_mb"] == 15856
    assert result["details"]["used_mb"] == 1587


def test_memory_english_output_unchanged():
    """英文输出（LC_ALL=C 或英文 locale 机器）行为不变。"""
    raw = (
        "               total        used        free      shared  buff/cache   available\n"
        "Mem:           15856        1587         246         264       14022       13674\n"
        "Swap:           2047           0        2047"
    )
    result = parse_item("memory", raw, "linux")
    assert result["success"] is True
    assert result["value"] == "10.0"


def test_cpu_parses_chinese_locale_top():
    """全角冒号的 %Cpu(s)： 行仍取 idle 补集。"""
    result = parse_item("cpu", ZH_TOP, "linux")
    assert result["success"] is True
    assert result["value"] == "0.7"  # 100 - 99.3


def test_load_parses_chinese_locale_uptime():
    result = parse_item("load", ZH_UPTIME, "linux")
    assert result["success"] is True
    assert result["details"]["load_1"] == 0.08


def test_linux_commands_force_c_locale_for_keyword_parsers():
    """cpu/memory/load 三个被英文关键字解析的命令必须前置 LC_ALL=C。"""
    from app.services.inspection_commands import LINUX_COMMANDS

    for item in ("cpu", "memory", "load"):
        assert LINUX_COMMANDS[item][0].startswith("LC_ALL=C "), item


def test_disk_command_excludes_pseudo_filesystems():
    """磁盘项必须排除伪文件系统，否则恒高的 efivarfs 会误报磁盘警告。

    实测：生产机 / 与 /data 才 14%/1%，但 efivarfs(256K 固件变量)常年 90%，
    _parse_disk 取全量文件系统的 max_usage → 磁盘项误报 warning。
    """
    from app.services.inspection_commands import LINUX_COMMANDS

    cmd = LINUX_COMMANDS["disk"][0]
    for fs in ("tmpfs", "devtmpfs", "efivarfs", "overlay", "squashfs"):
        assert f"-x {fs}" in cmd, fs
