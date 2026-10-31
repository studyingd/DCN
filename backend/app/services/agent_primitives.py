"""Agent 参数化取证原语——LLM 只传数据,命令由服务端模板拼装。

与 agent_commands.py 的固定注册表是同一条红线:LLM 永不接触 shell 自由
输入。这里的放宽仅是「同一类取证可以带受控参数」(指定容器日志/指定服务
状态与日志/指定文件尾部/指定目录占用),参数全部走白名单正则与数值区间,
任何含 shell/PowerShell 元字符的参数在拼装前即被拒绝;拼装产物再过一遍
agent_commands.assert_readonly 兜底(正常永远不触发,防未来模板写错)。

只读 ≠ 可以外泄:诊断报告会落 agent_runs、进飞书卡片——read_file/du_path
叠加敏感路径拒绝清单(私钥/shadow/.env/registry 凭据等);所有原语输出由
agent._dispatch 统一经 redact_output 脱敏(口令类赋值/私钥块/Authorization)。
"""

from __future__ import annotations

import re
import shlex

from app.services.agent_commands import assert_readonly


class PrimitiveError(ValueError):
    """参数校验失败(参数非法/缺失/命中敏感路径)。"""


# ── 参数白名单正则:拒绝一切 shell/PowerShell 元字符 ──

# docker 容器名:字母数字开头,允许 . _ -,≤64 字符
_CONTAINER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,63}$")
# systemd unit(nginx.service / docker.socket / user@1000.service)
_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@\-]{1,120}$")
# Linux 绝对路径:白名单字符集(含空格与 CJK 目录名),无引号/变量/替换/通配
_LINUX_PATH_RE = re.compile(r"^/[A-Za-z0-9_./@+()\u4e00-\u9fff \-]{0,254}$")
# Windows 绝对路径(C:\... 或 C:\...),同样白名单字符集
_WIN_PATH_RE = re.compile(r"^[A-Za-z]:[\\/][A-Za-z0-9_ .()\u4e00-\u9fff\\/\-]{0,254}$")

# 进程名片段(过滤用,不含空格/元字符)
_PATTERN_RE = re.compile(r"^[A-Za-z0-9_.\-+]{1,40}$")
# TCP 探测目标:IPv4 或主机名(IPv6 字面量与 bash /dev/tcp 的 host:port 语法冲突)
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{2,253}$")
# Windows 服务名(可含空格,如 Windows Management Instrumentation)
_WIN_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.\- ]{1,80}$")
# http_probe 的 path:以 / 开头的 URL 路径片段(禁参数与元字符)
_HTTP_PATH_RE = re.compile(r"^/[A-Za-z0-9._~\-]{0,199}$")
# 文件名片段(找文件用;通配符由模板注入,字符集禁 * ? 引号)
_FNAME_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff ]{1,60}$")
# Windows 事件过滤子串(来源/消息,可含空格)
_EVENT_PATTERN_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff ]{1,60}$")

# 行数参数统一区间
_TAIL_MIN, _TAIL_MAX = 20, 500

# ── 敏感路径拒绝清单(read_file/du_path;只读不等于可以外泄) ──
_SENSITIVE_PATH_RES = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(^|/)g?shadow",
        r"(^|/)sudoers",
        r"id_(rsa|dsa|ecdsa|ed25519)",
        r"\.pem$|\.key$|\.pfx$|\.kdbx$",
        r"authorized_keys$|known_hosts",
        r"(^|/)\.env",
        r"(^|/)secrets?(/|$)",
        r"credential",
        r"docker/config\.json$",  # registry 凭据
        r"\.git/config$|\.kube/config$|\.aws/|\.docker/",
        r"system32/config/",
        r"unattended[^/]*\.xml$|sysprep[^/]*\.xml$",
        r"^/proc/kcore|^/proc/keys|^/dev/|^/sys/firmware",
    )
)


def _deny_sensitive(path: str) -> None:
    for pattern in _SENSITIVE_PATH_RES:
        if pattern.search(path.replace("\\", "/")):
            raise PrimitiveError(f"路径命中敏感文件清单({pattern.pattern}),已拒绝读取")


def _require_str(
    params: dict, name: str, pattern: re.Pattern, what: str, required: bool = True
) -> str | None:
    value = params.get(name)
    if value is None or str(value).strip() == "":
        if required:
            raise PrimitiveError(f"缺少必填参数 {name}({what})")
        return None
    value = str(value)
    if not pattern.match(value):
        raise PrimitiveError(f"参数 {name} 非法({what});只接受受限字符集")
    return value


def _require_int(
    params: dict, name: str, low: int, high: int, default: int | None = None
) -> int:
    raw = params.get(name, default)
    if raw is None:
        raise PrimitiveError(f"缺少必填参数 {name}(整数 {low}~{high})")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise PrimitiveError(f"参数 {name} 必须是 {low}~{high} 的整数") from None
    if not low <= value <= high:
        raise PrimitiveError(f"参数 {name} 超出范围,允许 {low}~{high}")
    return value


def _reject_dotdot(path: str) -> None:
    if ".." in path.replace("\\", "/").split("/"):
        raise PrimitiveError("路径不允许包含 .. 段")


# ── Linux(sh)原语构建器 ──


def _sh_docker_logs(params: dict) -> str:
    name = _require_str(params, "name", _CONTAINER_RE, "docker 容器名")
    tail = _require_int(params, "tail", _TAIL_MIN, _TAIL_MAX, 200)
    return f"docker logs --tail {tail} {name}"


def _sh_docker_inspect(params: dict) -> str:
    name = _require_str(params, "name", _CONTAINER_RE, "docker 容器名")
    return f"docker inspect {name}"


def _ps_docker_inspect(params: dict) -> str:
    name = _require_str(params, "name", _CONTAINER_RE, "docker 容器名")
    return f"docker inspect {name}"


# 进程 PID:纯数字,≤7 位
_PID_RE = re.compile(r"^[0-9]{1,7}$")


def _sh_docker_exec_read(params: dict) -> str:
    name = _require_str(params, "name", _CONTAINER_RE, "docker 容器名")
    path = _require_str(params, "path", _LINUX_PATH_RE, "容器内文件绝对路径")
    _reject_dotdot(path)
    _deny_sensitive(path)
    return f"docker exec {name} cat {shlex.quote(path)}"


def _ps_docker_exec_read(params: dict) -> str:
    name = _require_str(params, "name", _CONTAINER_RE, "docker 容器名")
    path = _require_str(params, "path", _LINUX_PATH_RE, "容器内文件绝对路径")
    _reject_dotdot(path)
    _deny_sensitive(path)
    # Windows 宿主上的 docker 跑的也是 Linux 容器,路径按 Linux 规则校验
    return f"docker exec {name} cat {shlex.quote(path)}"


def _sh_process_detail(params: dict) -> str:
    pid = _require_str(params, "pid", _PID_RE, "进程 PID")
    return (
        "echo '## detail'; "
        f"ps -fp {pid}; echo '## cwd'; readlink /proc/{pid}/cwd; "
        f"echo '## parent'; ps -o ppid=,user=,comm= -p {pid}"
    )


def _ps_process_detail(params: dict) -> str:
    pid = _require_str(params, "pid", _PID_RE, "进程 PID")
    return (
        "Get-CimInstance Win32_Process -Filter "
        f"'ProcessId={pid}' | Format-List Name,ProcessId,ParentProcessId,CommandLine,ExecutablePath"
    )


def _sh_list_dir(params: dict) -> str:
    path = _require_str(params, "path", _LINUX_PATH_RE, "Linux 目录路径")
    _reject_dotdot(path)
    _deny_sensitive(path)
    return f"ls -la {shlex.quote(path)}"


def _ps_list_dir(params: dict) -> str:
    path = _require_str(params, "path", _WIN_PATH_RE, "Windows 目录路径")
    _reject_dotdot(path)
    _deny_sensitive(path)
    return f"Get-ChildItem -LiteralPath {_ps_quote(path)} | Format-Table Mode,LastWriteTime,Length,Name -AutoSize"


def _sh_service_status(params: dict) -> str:
    unit = _require_str(params, "unit", _UNIT_RE, "systemd unit 名")
    return f"systemctl status -l --no-pager -n 30 {shlex.quote(unit)}"


def _sh_service_journal(params: dict) -> str:
    unit = _require_str(params, "unit", _UNIT_RE, "systemd unit 名")
    lines = _require_int(params, "lines", _TAIL_MIN, _TAIL_MAX, 200)
    return f"journalctl -u {shlex.quote(unit)} -n {lines} --no-pager"


def _sh_read_file(params: dict) -> str:
    path = _require_str(params, "path", _LINUX_PATH_RE, "Linux 绝对路径")
    lines = _require_int(params, "lines", _TAIL_MIN, _TAIL_MAX, 200)
    _reject_dotdot(path)
    _deny_sensitive(path)
    return f"tail -n {lines} -- {shlex.quote(path)}"


def _sh_du_path(params: dict) -> str:
    path = _require_str(params, "path", _LINUX_PATH_RE, "Linux 绝对路径")
    depth = _require_int(params, "depth", 1, 3, 1)
    _reject_dotdot(path)
    _deny_sensitive(path)
    return (
        f"du -xh --max-depth={depth} -- {shlex.quote(path)} 2>/dev/null"
        " | sort -rh | head -20"
    )


def _sh_ps_filter(params: dict) -> str:
    pattern = _require_str(params, "pattern", _PATTERN_RE, "进程名片段")
    # -F 字面量匹配 + 引号包裹:pattern 永远是被查找的数据,不是语法
    return (
        f"(ps aux | grep -F -- {shlex.quote(pattern)} | grep -v grep)"
        " || echo 'NO MATCHING PROCESS'"
    )


def _sh_tcp_probe(params: dict) -> str:
    host = _require_str(params, "host", _HOST_RE, "主机名或 IPv4")
    port = _require_int(params, "port", 1, 65535)
    # 输入重定向打开 /dev/tcp 伪设备只建立连接不写数据;5s 超时兜底
    return (
        f"timeout 5 bash -c '</dev/tcp/{host}/{port}'"
        " && echo 'CONNECT OK' || echo 'CONNECT FAILED'"
    )


def _sh_http_probe(params: dict) -> str:
    port = _require_int(params, "port", 1, 65535)
    path = params.get("path", "/")
    if not _HTTP_PATH_RE.match(str(path)):
        raise PrimitiveError("参数 path 非法(以 / 开头的受限字符集)")
    # 只取状态码与耗时,body 丢弃(-o /dev/null 是参数不是重定向)——
    # 防大响应/敏感内容进上下文。仅限回环:放开任意 URL=数据外发通道。
    return (
        f"curl -sS -o /dev/null -w '%{{http_code}} %{{time_total}}s'"
        f" --max-time 5 http://127.0.0.1:{port}{path}"
    )


def _sh_find_name(params: dict) -> str:
    path = _require_str(params, "path", _LINUX_PATH_RE, "Linux 绝对路径")
    name = _require_str(params, "name", _FNAME_RE, "文件名片段")
    _reject_dotdot(path)
    _deny_sensitive(path)
    # 模板固定不带 find 的动作标志(-exec/-delete/-fprint 等即任意执行/写文件),
    # 通配 * 由模板注入,-printf 纯只读输出,按大小倒序 TOP20。
    return (
        f"find {shlex.quote(path)} -maxdepth 4 -name '*{name}*' -printf '%s %p\\n'"
        " 2>/dev/null | sort -rn | head -20"
    )


# ── Windows(PowerShell)原语构建器 ──


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ps_docker_logs(params: dict) -> str:
    name = _require_str(params, "name", _CONTAINER_RE, "docker 容器名")
    tail = _require_int(params, "tail", _TAIL_MIN, _TAIL_MAX, 200)
    return f"docker logs --tail {tail} {name}"


def _ps_read_file(params: dict) -> str:
    path = _require_str(params, "path", _WIN_PATH_RE, "Windows 绝对路径")
    lines = _require_int(params, "lines", _TAIL_MIN, _TAIL_MAX, 200)
    _reject_dotdot(path)
    _deny_sensitive(path)
    return f"Get-Content -LiteralPath {_ps_quote(path)} -Tail {lines}"


def _ps_ps_filter(params: dict) -> str:
    pattern = _require_str(params, "pattern", _PATTERN_RE, "进程名片段")
    # pattern 字符集不含引号/通配符,两侧 * 由模板固定注入(子串匹配语义)
    return (
        f"Get-Process -Name '*{pattern}*' -ErrorAction SilentlyContinue"
        " | Format-Table Name,Id,CPU,WS -AutoSize"
    )


def _ps_tcp_probe(params: dict) -> str:
    host = _require_str(params, "host", _HOST_RE, "主机名或 IPv4")
    port = _require_int(params, "port", 1, 65535)
    return (
        f"Test-NetConnection -ComputerName {_ps_quote(host)} -Port {port}"
        " | Format-Table ComputerName,RemoteAddress,RemotePort,TcpTestSucceeded -AutoSize"
    )


def _ps_service_status(params: dict) -> str:
    name = _require_str(params, "name", _WIN_SERVICE_RE, "Windows 服务名")
    safe = name.replace("'", "''")  # 字符集已禁引号,双保险
    return (
        f"Get-Service -Name {_ps_quote(name)} | Format-Table Status,Name,DisplayName -AutoSize; "
        f"Get-CimInstance Win32_Service -Filter \"Name='{safe}'\" | "
        "Format-Table Name,State,StartMode,StartName,ProcessId,ExitCode -AutoSize"
    )


def _ps_http_probe(params: dict) -> str:
    port = _require_int(params, "port", 1, 65535)
    path = params.get("path", "/")
    if not _HTTP_PATH_RE.match(str(path)):
        raise PrimitiveError("参数 path 非法(以 / 开头的受限字符集)")
    # Invoke-RestMethod 不在写原语黑名单(Invoke-WebRequest/iwr/curl 均被禁且
    # 不开口子);非 2xx 抛异常,从 Exception.Response 取状态码;只输出状态文本
    # 不输出 body。同样仅限回环。
    return (
        "try { $null = Invoke-RestMethod -Uri "
        f"'http://127.0.0.1:{port}{path}'"
        " -TimeoutSec 5; Write-Output 'HTTP 200 OK' } "
        "catch { $r = $_.Exception.Response; if ($r) "
        "{ Write-Output ('HTTP ' + [int]$r.StatusCode) } else "
        "{ Write-Output ('PROBE FAILED: ' + $_.Exception.Message) } }"
    )


def _ps_event_filter(params: dict) -> str:
    log = params.get("log")
    if log not in ("System", "Application"):
        raise PrimitiveError("参数 log 只允许 System 或 Application")
    pattern = _require_str(params, "pattern", _EVENT_PATTERN_RE, "来源/消息子串")
    return (
        f"Get-EventLog -LogName {log} -Newest 300 | "
        f"Where-Object {{ $_.Source -like '*{pattern}*' -or $_.Message -like '*{pattern}*' }} | "
        "Select-Object -First 25 TimeGenerated,EntryType,Source,"
        "@{N='Msg';E={$_.Message.Substring(0,[math]::Min(100,$_.Message.Length))}} | "
        "Format-Table -AutoSize"
    )


def _ps_find_name(params: dict) -> str:
    path = _require_str(params, "path", _WIN_PATH_RE, "Windows 绝对路径")
    name = _require_str(params, "name", _FNAME_RE, "文件名片段")
    _reject_dotdot(path)
    _deny_sensitive(path)
    return (
        f"Get-ChildItem -LiteralPath {_ps_quote(path)} -Recurse -Depth 4 "
        f"-Filter '*{name}*' -ErrorAction SilentlyContinue | "
        "Sort-Object Length -Descending | Select-Object -First 20 "
        "@{N='MB';E={[math]::Round($_.Length/1MB,2)}},FullName | Format-Table -AutoSize"
    )


# ── 原语注册表(与 agent_commands 注册表同构,多一个参数化标记) ──

_SH_PRIMITIVES: dict[str, dict] = {
    "docker_logs_container": {
        "label": "指定容器完整日志",
        "timeout": 30,
        "param_names": {"name", "tail"},
        "build": _sh_docker_logs,
    },
    "docker_inspect_container": {
        "label": "指定容器 inspect 详情",
        "timeout": 15,
        "param_names": {"name"},
        "build": _sh_docker_inspect,
    },
    "docker_exec_read": {
        "label": "读容器内指定文件",
        "timeout": 20,
        "param_names": {"name", "path"},
        "build": _sh_docker_exec_read,
    },
    "process_detail": {
        "label": "指定进程详情与归属",
        "timeout": 10,
        "param_names": {"pid"},
        "build": _sh_process_detail,
    },
    "list_dir": {
        "label": "列目录内容",
        "timeout": 10,
        "param_names": {"path"},
        "build": _sh_list_dir,
    },
    "service_status": {
        "label": "指定 systemd 服务状态",
        "timeout": 15,
        "param_names": {"unit"},
        "build": _sh_service_status,
    },
    "service_journal": {
        "label": "指定 systemd 服务日志",
        "timeout": 25,
        "param_names": {"unit", "lines"},
        "build": _sh_service_journal,
    },
    "read_file": {
        "label": "读取指定文件尾部",
        "timeout": 15,
        "param_names": {"path", "lines"},
        "build": _sh_read_file,
    },
    "du_path": {
        "label": "指定路径目录占用",
        "timeout": 30,
        "param_names": {"path", "depth"},
        "build": _sh_du_path,
    },
    "ps_filter": {
        "label": "按名称过滤进程",
        "timeout": 15,
        "param_names": {"pattern"},
        "build": _sh_ps_filter,
    },
    "tcp_probe": {
        "label": "TCP 连通探测",
        "timeout": 15,
        "param_names": {"host", "port"},
        "build": _sh_tcp_probe,
    },
    "http_probe": {
        "label": "本机 HTTP 健康探测",
        "timeout": 15,
        "param_names": {"port", "path"},
        "build": _sh_http_probe,
    },
    "find_name": {
        "label": "按名称找文件",
        "timeout": 40,
        "param_names": {"path", "name"},
        "build": _sh_find_name,
    },
}

_PS_PRIMITIVES: dict[str, dict] = {
    "docker_logs_container": {
        "label": "指定容器完整日志",
        "timeout": 30,
        "param_names": {"name", "tail"},
        "build": _ps_docker_logs,
    },
    "docker_inspect_container": {
        "label": "指定容器 inspect 详情",
        "timeout": 15,
        "param_names": {"name"},
        "build": _ps_docker_inspect,
    },
    "docker_exec_read": {
        "label": "读容器内指定文件",
        "timeout": 20,
        "param_names": {"name", "path"},
        "build": _ps_docker_exec_read,
    },
    "process_detail": {
        "label": "指定进程详情与归属",
        "timeout": 10,
        "param_names": {"pid"},
        "build": _ps_process_detail,
    },
    "list_dir": {
        "label": "列目录内容",
        "timeout": 10,
        "param_names": {"path"},
        "build": _ps_list_dir,
    },
    "read_file": {
        "label": "读取指定文件尾部",
        "timeout": 15,
        "param_names": {"path", "lines"},
        "build": _ps_read_file,
    },
    "ps_filter": {
        "label": "按名称过滤进程",
        "timeout": 15,
        "param_names": {"pattern"},
        "build": _ps_ps_filter,
    },
    "tcp_probe": {
        "label": "TCP 连通探测",
        "timeout": 30,
        "param_names": {"host", "port"},
        "build": _ps_tcp_probe,
    },
    "http_probe": {
        "label": "本机 HTTP 健康探测",
        "timeout": 20,
        "param_names": {"port", "path"},
        "build": _ps_http_probe,
    },
    "win_event_filter": {
        "label": "Windows 事件日志过滤",
        "timeout": 25,
        "param_names": {"log", "pattern"},
        "build": _ps_event_filter,
    },
    "find_name": {
        "label": "按名称找文件",
        "timeout": 40,
        "param_names": {"path", "name"},
        "build": _ps_find_name,
    },
    "win_service_status": {
        "label": "指定 Windows 服务状态",
        "timeout": 20,
        "param_names": {"name"},
        "build": _ps_service_status,
    },
}


def catalog_for(device) -> dict[str, dict]:
    """原语的目录形态:并入 agent_commands 目录后随 _select_catalog 一起筛选。

    只暴露 label/timeout/parameterized——命令不存在,直到参数校验通过后才拼装。
    """
    table = _PS_PRIMITIVES if device.is_windows else _SH_PRIMITIVES
    return {
        key: {
            "label": spec["label"],
            "timeout": spec["timeout"],
            "parameterized": True,
        }
        for key, spec in table.items()
    }


def build_command(key: str, params, device) -> dict:
    """校验参数并拼装命令;任何不合法(缺失/越界/元字符/敏感路径)即抛
    PrimitiveError,不会拼出任何 shell 片段。"""
    table = _PS_PRIMITIVES if device.is_windows else _SH_PRIMITIVES
    spec = table.get(key)
    if spec is None:
        raise PrimitiveError(f"取证项 {key!r} 在目标平台不可用")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise PrimitiveError("params 必须是对象(参数名到值的映射)")
    unknown = set(params) - spec["param_names"]
    if unknown:
        raise PrimitiveError(
            f"未知参数 {sorted(unknown)};允许的参数: {sorted(spec['param_names'])}"
        )
    command = spec["build"](params)
    # 兜底:参数已过白名单,拼装产物理应恒为只读;若未来模板写错在此拦截。
    # 参数值(容器名/路径)本身可能含形似写原语的子串(run 893:容器名
    # document-sync-worker 的 sync 被切成独立词而误拒),直接筛成品会误伤
    # 合法参数——把已校验参数值替换成中性占位符后再过静态屏:模板层的写入
    # 企图仍被拦(占位符不掩盖模板结构),参数子串不再参与判定。
    screened = command
    for value in sorted(
        {str(v) for v in params.values() if v is not None}, key=len, reverse=True
    ):
        screened = screened.replace(value, "P0")
    assert_readonly(screened, "ps" if device.is_windows else "sh")
    return {"label": spec["label"], "command": command, "timeout": spec["timeout"]}


# ── 输出脱敏(agent._dispatch 对所有诊断输出统一应用) ──

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?:.|\n)*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
# 口令类赋值:保留键名,值替换为 ***(MYSQL_ROOT_PASSWORD=xx / "token": "xx" 等,
# 兼容 shell/YAML/JSON 风格——键名左侧允许引号,值侧引号随值一起被替换)
_ASSIGN_RE = re.compile(
    r"(?i)([A-Za-z0-9_\-]*(?:password|passwd|secret|token|api[_-]?key|"
    r"access[_-]?key)[A-Za-z0-9_\-]*)([\"']?\s*[=:]\s*)\S{1,200}"
)
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*:\s*)(?:bearer\s+)?\S+")


def redact_output(text: str) -> str:
    """打码输出中的凭据材料:私钥块、口令/token 赋值、Authorization 头。"""
    if not text:
        return text
    text = _PRIVATE_KEY_RE.sub(
        "-----BEGIN PRIVATE KEY-----[已脱敏]-----END PRIVATE KEY-----", text
    )
    text = _ASSIGN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}***", text)
    return text
