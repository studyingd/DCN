"""agent_primitives 单元测试:命令拼接钉住、注入拒绝、敏感路径、输出脱敏。

红线:LLM 传入的参数永远只能改变「取证对象」,不能把任何 shell 语法片段
带进命令。所有用例围绕这条不变量。
"""

import json

import pytest

from app.services import agent
from app.services import agent_primitives as ap
from app.services.agent_commands import assert_readonly


class _LinuxDevice:
    is_windows = False
    name = "t-linux"
    ip_address = "127.0.0.10"
    type = "server"
    os_system = "linux"


class _WinDevice:
    is_windows = True
    name = "t-win"
    ip_address = "127.0.0.11"
    type = "host"
    os_system = "windows"


LINUX = _LinuxDevice()
WIN = _WinDevice()


# ── 命令拼接钉住:合法参数 → 精确命令字符串 ──


def test_linux_commands_pinned():
    assert (
        ap.build_command("docker_logs_container", {"name": "web-1"}, LINUX)["command"]
        == "docker logs --tail 200 web-1"
    )
    assert (
        ap.build_command("docker_logs_container", {"name": "web.2", "tail": 50}, LINUX)[
            "command"
        ]
        == "docker logs --tail 50 web.2"
    )
    assert (
        ap.build_command("service_status", {"unit": "nginx.service"}, LINUX)["command"]
        == "systemctl status -l --no-pager -n 30 nginx.service"
    )
    assert (
        ap.build_command(
            "service_journal", {"unit": "docker.socket", "lines": 100}, LINUX
        )["command"]
        == "journalctl -u docker.socket -n 100 --no-pager"
    )
    assert (
        ap.build_command("read_file", {"path": "/var/log/app/log.txt"}, LINUX)[
            "command"
        ]
        == "tail -n 200 -- /var/log/app/log.txt"
    )
    # 带空格的路径由 shlex 安全加引号
    assert (
        ap.build_command("read_file", {"path": "/var/log/my app/log"}, LINUX)["command"]
        == "tail -n 200 -- '/var/log/my app/log'"
    )
    assert (
        ap.build_command("du_path", {"path": "/var/lib", "depth": 2}, LINUX)["command"]
        == "du -xh --max-depth=2 -- /var/lib 2>/dev/null | sort -rh | head -20"
    )
    assert (
        ap.build_command("ps_filter", {"pattern": "nginx"}, LINUX)["command"]
        == "(ps aux | grep -F -- nginx | grep -v grep) || echo 'NO MATCHING PROCESS'"
    )
    assert (
        ap.build_command("tcp_probe", {"host": "192.168.1.1", "port": 3306}, LINUX)[
            "command"
        ]
        == "timeout 5 bash -c '</dev/tcp/192.168.1.1/3306'"
        " && echo 'CONNECT OK' || echo 'CONNECT FAILED'"
    )
    assert (
        ap.build_command("http_probe", {"port": 8080, "path": "/health"}, LINUX)[
            "command"
        ]
        == "curl -sS -o /dev/null -w '%{http_code} %{time_total}s'"
        " --max-time 5 http://127.0.0.1:8080/health"
    )
    assert (
        ap.build_command("find_name", {"path": "/var/log", "name": "messages"}, LINUX)[
            "command"
        ]
        == "find /var/log -maxdepth 4 -name '*messages*' -printf '%s %p\\n'"
        " 2>/dev/null | sort -rn | head -20"
    )


def test_windows_commands_pinned():
    assert (
        ap.build_command("docker_logs_container", {"name": "web-1", "tail": 50}, WIN)[
            "command"
        ]
        == "docker logs --tail 50 web-1"
    )
    assert (
        ap.build_command("read_file", {"path": "C:\\apps\\app\\log.txt"}, WIN)[
            "command"
        ]
        == "Get-Content -LiteralPath 'C:\\apps\\app\\log.txt' -Tail 200"
    )
    # 单引号在 PowerShell 里的转义是翻倍,但白名单字符集根本不允许引号进入
    with pytest.raises(ap.PrimitiveError):
        ap.build_command("read_file", {"path": "C:\\x'y\\log.txt"}, WIN)
    assert (
        ap.build_command("ps_filter", {"pattern": "sqlservr"}, WIN)["command"]
        == "Get-Process -Name '*sqlservr*' -ErrorAction SilentlyContinue"
        " | Format-Table Name,Id,CPU,WS -AutoSize"
    )
    assert (
        ap.build_command("tcp_probe", {"host": "db.internal", "port": 1433}, WIN)[
            "command"
        ]
        == "Test-NetConnection -ComputerName 'db.internal' -Port 1433"
        " | Format-Table ComputerName,RemoteAddress,RemotePort,TcpTestSucceeded -AutoSize"
    )
    assert (
        ap.build_command("win_service_status", {"name": "WinRM"}, WIN)["command"]
        == "Get-Service -Name 'WinRM' | Format-Table Status,Name,DisplayName -AutoSize; "
        "Get-CimInstance Win32_Service -Filter \"Name='WinRM'\" | "
        "Format-Table Name,State,StartMode,StartName,ProcessId,ExitCode -AutoSize"
    )
    assert (
        ap.build_command("http_probe", {"port": 80}, WIN)["command"]
        == "try { $null = Invoke-RestMethod -Uri 'http://127.0.0.1:80/' -TimeoutSec 5;"
        " Write-Output 'HTTP 200 OK' } catch { $r = $_.Exception.Response; if ($r)"
        " { Write-Output ('HTTP ' + [int]$r.StatusCode) } else"
        " { Write-Output ('PROBE FAILED: ' + $_.Exception.Message) } }"
    )
    assert (
        ap.build_command("win_event_filter", {"log": "System", "pattern": "disk"}, WIN)[
            "command"
        ]
        == "Get-EventLog -LogName System -Newest 300 | "
        "Where-Object { $_.Source -like '*disk*' -or $_.Message -like '*disk*' } | "
        "Select-Object -First 25 TimeGenerated,EntryType,Source,"
        "@{N='Msg';E={$_.Message.Substring(0,[math]::Min(100,$_.Message.Length))}} | "
        "Format-Table -AutoSize"
    )
    assert (
        ap.build_command("find_name", {"path": "C:\\inetpub", "name": "log"}, WIN)[
            "command"
        ]
        == "Get-ChildItem -LiteralPath 'C:\\inetpub' -Recurse -Depth 4 "
        "-Filter '*log*' -ErrorAction SilentlyContinue | "
        "Sort-Object Length -Descending | Select-Object -First 20 "
        "@{N='MB';E={[math]::Round($_.Length/1MB,2)}},FullName | Format-Table -AutoSize"
    )


# ── 注入拒绝:元字符/缺参/未知参数/越界 ──


@pytest.mark.parametrize(
    "key,params",
    [
        ("docker_logs_container", {"name": "web; rm -rf /"}),
        ("docker_logs_container", {"name": "web$(id)"}),
        ("docker_logs_container", {"name": "web`id`"}),
        ("docker_logs_container", {"name": ""}),
        ("docker_logs_container", {}),
        ("service_status", {"unit": "nginx.service; poweroff"}),
        ("service_journal", {"unit": "a | b"}),
        ("read_file", {"path": "/etc/passwd; cat /etc/shadow"}),
        ("read_file", {"path": "relative/path.txt"}),
        ("read_file", {"path": "/var/log/$(whoami)/x"}),
        ("read_file", {"path": "/var/log/a`b`"}),
        ("read_file", {"path": "/safe/path", "unknown_arg": 1}),
        ("read_file", {"path": "/safe/path", "lines": 99999}),
        ("read_file", {"path": "/safe/path", "lines": "abc"}),
        ("du_path", {"path": "/var", "depth": 9}),
        ("du_path", {}),
        ("ps_filter", {"pattern": "nginx; poweroff"}),
        ("ps_filter", {"pattern": "a b"}),
        ("ps_filter", {}),
        ("tcp_probe", {"host": "1.2.3.4; rm", "port": 80}),
        ("tcp_probe", {"host": "a/b"}),
        ("tcp_probe", {"host": "$(id).evil"}),
        ("tcp_probe", {"port": 80}),
        ("tcp_probe", {"host": "1.2.3.4"}),
        ("tcp_probe", {"host": "1.2.3.4", "port": 0}),
        ("tcp_probe", {"host": "1.2.3.4", "port": 70000}),
        ("tcp_probe", {"host": "1.2.3.4", "port": "abc"}),
        ("http_probe", {"port": 0}),
        ("http_probe", {"port": 80, "path": "/x?q=1"}),  # path 禁参数
        ("http_probe", {"port": 80, "path": "health"}),  # 必须以 / 开头
        ("http_probe", {"port": 80, "path": "/a b"}),
        ("http_probe", {}),
        ("find_name", {"path": "/var", "name": "x*y"}),  # 通配符禁入
        ("find_name", {"path": "/var", "name": "a'b"}),
        ("find_name", {"path": "/var/log/$(x)", "name": "a"}),
        ("find_name", {"name": "a"}),
        ("win_event_filter", {"log": "Security", "pattern": "x"}),  # 枚举外
        ("win_event_filter", {"log": "System", "pattern": "x;y"}),
        ("win_event_filter", {"log": "System"}),
    ],
)
def test_illegal_params_rejected(key, params):
    with pytest.raises(ap.PrimitiveError):
        ap.build_command(key, params, LINUX)


def test_platform_mismatch_rejected():
    # Linux 专属原语在 Windows 目录里不存在(反之亦然)
    with pytest.raises(ap.PrimitiveError):
        ap.build_command("service_status", {"unit": "nginx.service"}, WIN)
    with pytest.raises(ap.PrimitiveError):
        ap.build_command("du_path", {"path": "/var"}, WIN)
    with pytest.raises(ap.PrimitiveError):
        ap.build_command("win_service_status", {"name": "WinRM"}, LINUX)
    with pytest.raises(ap.PrimitiveError):
        ap.build_command("win_event_filter", {"log": "System", "pattern": "x"}, LINUX)


# ── 敏感路径拒绝清单 ──


@pytest.mark.parametrize(
    "path",
    [
        "/etc/shadow",
        "/etc/shadow-",
        "/etc/gshadow",
        "/etc/sudoers",
        "/root/.ssh/id_rsa",
        "/home/u/.ssh/id_ed25519",
        "/etc/ssl/private/server.pem",
        "/etc/ssl/private/server.key",
        "/root/.ssh/authorized_keys",
        "/srv/app/.env",
        "/srv/app/.env.production",
        "/srv/secrets/db.yaml",
        "/root/.docker/config.json",
        "/srv/app/.git/config",
        "/proc/kcore",
        "/dev/sda",
        "/var/backups/creds.kdbx",
    ],
)
def test_sensitive_paths_denied(path):
    with pytest.raises(ap.PrimitiveError, match="敏感"):
        ap.build_command("read_file", {"path": path}, LINUX)
    with pytest.raises(ap.PrimitiveError, match="敏感"):
        ap.build_command("du_path", {"path": path}, LINUX)


def test_dotdot_rejected():
    with pytest.raises(ap.PrimitiveError):
        ap.build_command("read_file", {"path": "/var/log/../shadow"}, LINUX)


# ── 拼装产物恒过只读校验(防未来模板写错) ──


def test_built_commands_pass_assert_readonly():
    combos = [
        ("docker_logs_container", {"name": "web", "tail": 500}, LINUX, "sh"),
        ("service_status", {"unit": "user@1000.service"}, LINUX, "sh"),
        ("service_journal", {"unit": "nginx.service", "lines": 20}, LINUX, "sh"),
        ("read_file", {"path": "/var/log/my app/log", "lines": 20}, LINUX, "sh"),
        ("du_path", {"path": "/srv/app", "depth": 3}, LINUX, "sh"),
        ("ps_filter", {"pattern": "qemu-ga"}, LINUX, "sh"),
        ("tcp_probe", {"host": "db.internal", "port": 65535}, LINUX, "sh"),
        ("http_probe", {"port": 8123, "path": "/api"}, LINUX, "sh"),
        ("find_name", {"path": "/var/log", "name": "mes sages"}, LINUX, "sh"),
        ("docker_logs_container", {"name": "web"}, WIN, "ps"),
        ("read_file", {"path": "C:\\apps log\\x.txt"}, WIN, "ps"),
        ("tcp_probe", {"host": "10.0.0.9", "port": 443}, WIN, "ps"),
        ("win_service_status", {"name": "Windows Audio"}, WIN, "ps"),
        ("http_probe", {"port": 80}, WIN, "ps"),
        ("win_event_filter", {"log": "Application", "pattern": "MSSQL"}, WIN, "ps"),
        ("find_name", {"path": "C:\\inetpub", "name": "日志"}, WIN, "ps"),
    ]
    for key, params, device, shell in combos:
        command = ap.build_command(key, params, device)["command"]
        assert_readonly(command, shell)  # 不抛即通过


# ── 输出脱敏 ──


def test_redact_output():
    text = (
        "MYSQL_ROOT_PASSWORD=hunter2\n"
        '"token": "eyJhbGciOi.abc",\n'
        "api_key = sk-1234567890\n"
        "Authorization: Bearer eyJhbG.abc.def\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpA...\n-----END RSA PRIVATE KEY-----\n"
        "正常行 uptime 5 days"
    )
    out = ap.redact_output(text)
    assert "hunter2" not in out
    assert "sk-1234567890" not in out
    assert "eyJhbGciOi.abc" not in out
    assert "Bearer eyJhbG.abc.def" not in out
    assert "MIIEpA" not in out
    assert "MYSQL_ROOT_PASSWORD=***" in out
    assert '"token": ***' in out
    assert "api_key = ***" in out
    assert "Authorization: ***" in out
    assert "正常行 uptime 5 days" in out


def test_redact_output_empty():
    assert ap.redact_output("") == ""


# ── 目录形态 ──


def test_catalog_shapes():
    linux = ap.catalog_for(LINUX)
    assert linux["read_file"] == {
        "label": "读取指定文件尾部",
        "timeout": 15,
        "parameterized": True,
    }
    assert set(linux) == {
        "docker_logs_container",
        "docker_inspect_container",
        "docker_exec_read",
        "service_status",
        "service_journal",
        "read_file",
        "du_path",
        "ps_filter",
        "tcp_probe",
        "http_probe",
        "find_name",
        "process_detail",
        "list_dir",
    }
    win = ap.catalog_for(WIN)
    assert set(win) == {
        "docker_logs_container",
        "docker_inspect_container",
        "docker_exec_read",
        "read_file",
        "ps_filter",
        "tcp_probe",
        "http_probe",
        "win_event_filter",
        "find_name",
        "win_service_status",
        "process_detail",
        "list_dir",
    }
    # 目录条目绝不携带命令——命令只在校验通过后拼装
    assert "command" not in linux["read_file"]


# ── dispatch 集成:参数穿透、脱敏、按参数去重、审计留痕 ──


def _call(key, params=None):
    args = {"key": key}
    if params is not None:
        args["params"] = params
    return {
        "id": "c1",
        "function": {"name": "run_diagnostic", "arguments": json.dumps(args)},
    }


def test_dispatch_parameterized(monkeypatch):
    device = _LinuxDevice()
    captured = {}

    def fake_exec(db, dev, command, timeout, session=None):
        captured["command"] = command
        captured["timeout"] = timeout
        return 0, "DB_PASSWORD=topsecret\nok", ""

    monkeypatch.setattr(agent, "_exec_diagnostic", fake_exec)
    catalog = ap.catalog_for(device)
    steps: list[dict] = []
    executed: set[str] = set()

    out = json.loads(
        agent._dispatch(
            None,
            device,
            catalog,
            _call("read_file", {"path": "/srv/app/log"}),
            steps,
            executed_keys=executed,
        )
    )
    assert captured["command"] == "tail -n 200 -- /srv/app/log"
    assert out["output"] == "DB_PASSWORD=***\nok"  # 脱敏对 LLM 同样生效
    assert steps[-1]["params"] == {"path": "/srv/app/log"}
    assert len(executed) == 1

    # 同 key 不同参数 = 不同证据,不去重
    agent._dispatch(
        None,
        device,
        catalog,
        _call("read_file", {"path": "/srv/app/other.log"}),
        steps,
        executed_keys=executed,
    )
    assert len(executed) == 2

    # 同 key 同参数 = 重复,拒绝
    out2 = json.loads(
        agent._dispatch(
            None,
            device,
            catalog,
            _call("read_file", {"path": "/srv/app/log"}),
            steps,
            executed_keys=executed,
        )
    )
    assert "error" in out2
    assert len(executed) == 2


def test_dispatch_rejects_illegal_params(monkeypatch):
    device = _LinuxDevice()

    def fake_exec(*a, **k):  # pragma: no cover - 不应被执行
        raise AssertionError("非法参数不应触达执行层")

    monkeypatch.setattr(agent, "_exec_diagnostic", fake_exec)
    catalog = ap.catalog_for(device)
    steps: list[dict] = []
    out = json.loads(
        agent._dispatch(
            None,
            device,
            catalog,
            _call("read_file", {"path": "/etc/shadow"}),
            steps,
        )
    )
    assert "参数校验失败" in out["error"]
    assert steps[-1]["ok"] is False


def test_process_detail_and_list_dir_and_exec_read_pinned():
    """第四批原语(2026-10-30,run 843 报告的「未验证项」补证据链):
    process_detail 回答「python -u bridge.py 归谁」(cwd/父进程/完整命令行),
    list_dir 回答「目录里有什么/配置在哪」, docker_exec_read 回答「容器内
    配置文件内容」。命令钉死、路径过敏感拒绝清单。"""
    assert (
        ap.build_command("process_detail", {"pid": "4242"}, LINUX)["command"]
        == "echo '## detail'; ps -fp 4242; echo '## cwd'; readlink /proc/4242/cwd; "
        "echo '## parent'; ps -o ppid=,user=,comm= -p 4242"
    )
    assert (
        ap.build_command("list_dir", {"path": "/opt/sensecraft"}, LINUX)["command"]
        == "ls -la /opt/sensecraft"
    )
    # 带空格路径必须被引号包住(shlex.quote 仅在必要时加引号)
    assert (
        ap.build_command("list_dir", {"path": "/opt/my app"}, LINUX)["command"]
        == "ls -la '/opt/my app'"
    )
    assert (
        ap.build_command(
            "docker_exec_read",
            {"name": "sensecraft", "path": "/app/config.json"},
            LINUX,
        )["command"]
        == "docker exec sensecraft cat /app/config.json"
    )
    # 敏感路径在容器内同样拒绝
    for bad in (
        {"path": "/etc/shadow"},
        {"path": "/root/.ssh/id_rsa"},
    ):
        with pytest.raises(ap.PrimitiveError):
            ap.build_command("docker_exec_read", {"name": "c1", **bad}, LINUX)
    # PID 越界/非法字符拒绝
    for bad_pid in ("-1", "abc", "99999999"):
        with pytest.raises(ap.PrimitiveError):
            ap.build_command("process_detail", {"pid": bad_pid}, LINUX)

    # run 893 回归:容器名含形似写原语的片段(sync/restart-)不误拒
    # (参数占位符替换后再过静态屏;PS 侧名字以动词开头也靠这层兜底)
    for dev in (LINUX, WIN):
        built = ap.build_command(
            "docker_inspect_container",
            {"name": "argus-production-document-sync-worker-1"},
            dev,
        )
        assert built["command"] == (
            "docker inspect argus-production-document-sync-worker-1"
        )
    assert (
        ap.build_command("docker_inspect_container", {"name": "restart-worker-1"}, WIN)[
            "command"
        ]
        == "docker inspect restart-worker-1"
    )
