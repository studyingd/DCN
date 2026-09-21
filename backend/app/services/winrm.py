"""WinRM channel for Windows devices (pywinrm).

Windows 设备的唯一管理通道——本平台不对 Windows 使用 SSH:
  * 指标采集 / OS 精确识别 / 巡检 / 批量脚本 / 关机重启,全部经此模块执行 PowerShell;
  * 仅使用 NTLM 认证(本地管理员/工作组场景),永不降级到 Basic;
  * 默认 http://host:5985/wsman(消息级加密),WINRM_USE_SSL=true 切 5986(HTTPS);
    SSL 证书默认校验,仅受控内网自签场景设 WINRM_CERT_VALIDATION=false。

pywinrm 为同步阻塞调用,调用方应在线程池中执行(与 paramiko 相同)。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from app.config import (
    WINRM_CERT_VALIDATION,
    WINRM_ENABLED,
    WINRM_PORT,
    WINRM_USE_SSL,
)

if TYPE_CHECKING:
    from app.models.device import Device

logger = logging.getLogger(__name__)

_probe_cache: dict[tuple[str, int], tuple[float, bool]] = {}
_probe_lock = threading.Lock()
# 缓存的是「host:port → 是否 WinRM」的探测结果;guest  churn 或被喷洒大量
# 不同 IP 时条目只增不减,加上限并按最旧者驱逐。
_PROBE_CACHE_MAX = 512


def _probe_cache_set(key: tuple[str, int], result: bool) -> None:
    with _probe_lock:
        if len(_probe_cache) >= _PROBE_CACHE_MAX:
            oldest = min(_probe_cache, key=lambda k: _probe_cache[k][0])
            _probe_cache.pop(oldest, None)
        _probe_cache[key] = (time.monotonic(), result)


# 单次执行的 stdout/stderr 字符上限(与 ssh.MAX_OUTPUT_BYTES 同意图:防爆内存)
MAX_OUTPUT_CHARS = 1_000_000

# 统一脚本输出为 UTF-8——中文 Windows 控制台默认 GBK,不设置时 stdout 解码会乱码。
# 调用方脚本若自行设置 OutputEncoding 则不重复注入。
_PS_UTF8_PRELUDE = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8"

# 关掉 PowerShell 的 Progress 流。
#
# 经 WinRM 执行时，Progress 流会被序列化成 CLIXML(#< CLIXML + 一大段 XML)
# 塞进 stderr。而「命令成功但 stdout 为空」往往正是我们要的健康结论
# (例如近 24h 没有服务失败事件)，调用方一旦回退到 stderr，嗂给解析器/LLM
# 的就是几百字符的 XML 噪音。从源头掉比在六个调用方各自过滤靠得住。
#
# 只影响进度条(Write-Progress)，不影响 stdout、错误流与退出码。
_PS_QUIET_PRELUDE = "$ProgressPreference='SilentlyContinue'"


def _apply_prelude(script: str) -> str:
    """给 PowerShell 脚本加上平台级 prelude。

    两个 prelude 各自独立守卫：它们管的是不相干的两件事(输出编码 / 进度流)，
    不能因为调用方自己设了 OutputEncoding 就把进度抑制也一并跳过。
    """
    prelude = []
    if "OutputEncoding" not in script:
        prelude.append(_PS_UTF8_PRELUDE)
    if "ProgressPreference" not in script:
        prelude.append(_PS_QUIET_PRELUDE)
    if not prelude:
        return script
    return "\n".join(prelude) + "\n" + script


class WinRMError(Exception):
    """WinRM 连接/执行失败(认证、网络、超时、协议错误的统一封装)。

    ``kind``: auth(认证失败)/ connect(从未连上)/ timeout(执行超时)/
    transport(传输层错误)。power 操作据此区分"硬失败"与"命令可能已送达"。
    """

    def __init__(self, message: str, *, kind: str = "transport"):
        super().__init__(message)
        self.kind = kind


def _endpoint(host: str, port: int) -> str:
    scheme = "https" if WINRM_USE_SSL else "http"
    return f"{scheme}://{host}:{port}/wsman"


def probe_winrm_service(host: str, port: int = 5985, timeout: float = 1.2) -> bool:
    """Detect a WSMan/WinRM listener without requiring guest credentials.

    A bare TCP connect can mistake any process on 5985 for WinRM.  An
    unauthenticated WSMan request normally returns 401 with NTLM/Negotiate
    challenges, which is sufficient to hide the bootstrap script safely.
    """
    import requests

    key = (str(host), int(port))
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(key)
        if cached and now - cached[0] < 10:
            return cached[1]

    scheme = "https" if port == 5986 else "http"
    endpoint = f"{scheme}://{host}:{port}/wsman"
    try:
        response = requests.post(
            endpoint,
            data=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                'xmlns:wsmid="http://schemas.dmtf.org/wbem/wsman/identity/1/wsmanidentity.xsd">'
                "<s:Header/><s:Body><wsmid:Identify/></s:Body></s:Envelope>"
            ),
            headers={"Content-Type": "application/soap+xml;charset=UTF-8"},
            timeout=timeout,
            verify=WINRM_CERT_VALIDATION if scheme == "https" else True,
        )
    except requests.RequestException:
        result = False
        _probe_cache_set(key, result)
        return result
    auth = response.headers.get("WWW-Authenticate", "").lower()
    server = response.headers.get("Server", "").lower()
    body = response.text[:2000].lower()
    result = (
        (
            response.status_code == 401
            and any(item in auth for item in ("ntlm", "negotiate", "kerberos"))
        )
        or "microsoft-httpapi" in server
        or "wsmanidentity" in body
    )
    _probe_cache_set(key, result)
    return result


def run_powershell(
    host: str,
    username: str,
    password: str,
    script: str,
    *,
    port: int | None = None,
    timeout: int = 15,
) -> tuple[int, str, str]:
    """在 Windows 主机上经 WinRM 执行 PowerShell 脚本。

    返回 ``(exit_code, stdout, stderr)``;传输/认证/超时失败抛 ``WinRMError``。
    """
    if not WINRM_ENABLED:
        raise WinRMError("WinRM 已被禁用(WINRM_ENABLED=false)", kind="connect")
    try:
        import winrm
        from winrm.exceptions import WinRMOperationTimeoutError, WinRMTransportError
    except ImportError as exc:
        raise WinRMError(
            "pywinrm 未安装,请 pip install pywinrm", kind="connect"
        ) from exc

    script = _apply_prelude(script)

    port = port or WINRM_PORT
    session = winrm.Session(
        _endpoint(host, port),
        auth=(username, password),
        transport="ntlm",
        server_cert_validation="validate" if WINRM_CERT_VALIDATION else "ignore",
        operation_timeout_sec=timeout,
        read_timeout_sec=timeout + 10,  # pywinrm 要求 read > operation
    )

    try:
        result = session.run_ps(script)
    except WinRMTransportError as exc:
        msg = str(exc)
        if "401" in msg:
            raise WinRMError(
                "WinRM 认证失败,请检查凭据(需本地管理员权限)", kind="auth"
            ) from exc
        raise WinRMError(f"WinRM 传输错误: {msg[:200]}", kind="transport") from exc
    except WinRMOperationTimeoutError as exc:
        raise WinRMError(f"WinRM 执行超时({timeout}s)", kind="timeout") from exc
    except Exception as exc:  # requests 连接错误 / socket 超时 / 协议异常等
        raise WinRMError(
            f"无法连接 WinRM ({host}:{port}): {exc}", kind="connect"
        ) from exc

    out = (result.std_out or b"").decode("utf-8", errors="replace").strip()
    err = (result.std_err or b"").decode("utf-8", errors="replace").strip()
    return result.status_code, out[:MAX_OUTPUT_CHARS], err[:MAX_OUTPUT_CHARS]


def run_on_device(
    device: Device,
    username: str,
    password: str,
    script: str,
    *,
    timeout: int = 15,
) -> tuple[int, str, str]:
    """对 Device 执行 PowerShell(端口取 device.winrm_port,缺省用全局配置)。"""
    if not device.ip_address:
        raise WinRMError("设备未配置 IP 地址", kind="connect")
    return run_powershell(
        device.ip_address,
        username,
        password,
        script,
        port=device.winrm_port or WINRM_PORT,
        timeout=timeout,
    )
