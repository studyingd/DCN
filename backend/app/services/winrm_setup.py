"""Generate a scoped WinRM bootstrap script for a Windows target."""

from __future__ import annotations

import ipaddress
import socket


def _local_source_ip(target_ip: str) -> str:
    """Return the DCN host address selected for traffic to ``target_ip``.

    UDP connect() does not send a packet; it only asks the OS routing table
    which local interface would be used. This keeps the generated script
    correct when DCN is deployed on another host or network.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((target_ip, 5985))
            candidate = sock.getsockname()[0]
            ipaddress.ip_address(candidate)
            if candidate and candidate != "0.0.0.0":
                return candidate
    except OSError:
        pass
    return ""


def is_ipv4(value: str | None) -> bool:
    """严格判定一个字符串是不是合法 IPv4（空值/IPv6/带前缀均为 False）。"""
    text = (value or "").strip()
    if not text:
        return False
    try:
        return ipaddress.ip_address(text).version == 4
    except ValueError:
        return False


def select_ipv4_target(*candidates: str | None) -> str | None:
    """从候选地址里选出第一个可用的 IPv4，按传入顺序优先。

    为什么不能「QGA 有值就用 QGA」：guest_agent_summary 在虚拟机没有 IPv4 时会
    回退到 addresses[0]，也就是一个全局 IPv6。而 WinRM 启用脚本只支持 IPv4，
    于是无条件优先 QGA 会把用户在运维接入里手填的 IPv4 永远遮蔽掉：
    界面明明显示着一个 IP，下载却总是失败。
    """
    for candidate in candidates:
        if is_ipv4(candidate):
            return (candidate or "").strip()
    return None


def build_winrm_setup_script(target_ip: str, winrm_port: int = 5985) -> tuple[str, str]:
    """Build an idempotent PowerShell setup script and its DCN source IP."""
    try:
        target = ipaddress.ip_address(target_ip.strip())
    except ValueError as exc:
        # 带上原始值：QGA 有可能回报带前缀/带空格的地址，不给值就无法排查
        raise ValueError(f"目标 IP 地址无效：{target_ip!r}") from exc
    if target.version != 4:
        raise ValueError(
            f"目标 {target} 是 IPv6，当前 WinRM 启用脚本仅支持 IPv4；"
            "请在虚拟机上启用 IPv4 地址，或在运维接入中手填一个 IPv4"
        )
    if not 1 <= int(winrm_port) <= 65535:
        raise ValueError("WinRM 端口必须在 1-65535 之间")

    source_ip = _local_source_ip(str(target))
    if not source_ip:
        raise ValueError(
            f"无法确定 DCN 到 {target} 的出口 IP（本机路由表里没有到该地址的路径）。"
            "请确认 DCN 容器/主机与该虚拟机网络互通后重试"
        )

    # Values are validated above and inserted as quoted literals only.
    script = f"""# DCN WinRM bootstrap script
# Script version: 6 (Public-network safe, registry-based WSMan settings)
# Generated dynamically for this DCN deployment.
# Security boundary: inbound WinRM is allowed only from DCN source IP {source_ip}.
# Run this script in an elevated PowerShell window on the Windows server/VM.
$ErrorActionPreference = 'Stop'
$DcnSourceIp = '{source_ip}'
$WinRmPort = {int(winrm_port)}

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    throw 'Run this script as Administrator.'
}}

# Enable the service and configure a listener without changing the network
# profile. Enable-PSRemoting refuses to create firewall exceptions on Public
# networks, so this script manages a scoped firewall rule below instead.
Set-Service -Name WinRM -StartupType Automatic
Start-Service -Name WinRM

# Locate the standard WinRM management utility explicitly instead of relying
# on the process PATH.
$WinRm = Join-Path $env:SystemRoot 'System32\\winrm.cmd'
if (-not (Test-Path -LiteralPath $WinRm)) {{
    throw 'winrm.cmd was not found under the Windows System32 directory.'
}}

function Invoke-WinRm([string[]] $Arguments) {{
    & $WinRm @Arguments
    if ($LASTEXITCODE -ne 0) {{
        throw ('WinRM command failed (exit=' + $LASTEXITCODE + '): ' + ($Arguments -join ' '))
    }}
}}

# Recreate the HTTP listener on the port configured in DCN. Existing HTTPS
# listeners are left untouched; the firewall rule below still limits source.
# The delete is best-effort: if no HTTP listener exists yet, winrm.cmd writes a
# WSManFault to stderr. Under $ErrorActionPreference='Stop' a native command's
# stderr is treated as a NativeCommandError and aborts the script, so temporarily
# drop to Continue AND swallow stderr for this one call, then restore.
$_prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $WinRm delete winrm/config/Listener?Address=*+Transport=HTTP 2>$null | Out-Null
$ErrorActionPreference = $_prevEap
$listenerConfig = '@{{Port="' + $WinRmPort + '"}}'
Invoke-WinRm @('create', 'winrm/config/Listener?Address=*+Transport=HTTP', $listenerConfig)

# Keep message-level encryption and disable weaker Basic/unencrypted modes.
# Neither `winrm set` nor the WSMan provider is used here: both can attempt to
# manage the built-in firewall exception and fail on Public network profiles.
$wsmanService = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WSMAN\\Service'
$wsmanAuth = Join-Path $wsmanService 'Auth'
New-Item -Path $wsmanService -Force | Out-Null
New-Item -Path $wsmanAuth -Force | Out-Null
New-ItemProperty -Path $wsmanService -Name 'AllowUnencrypted' -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty -Path $wsmanAuth -Name 'Basic' -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty -Path $wsmanAuth -Name 'Kerberos' -PropertyType DWord -Value 1 -Force | Out-Null
New-ItemProperty -Path $wsmanAuth -Name 'Negotiate' -PropertyType DWord -Value 1 -Force | Out-Null
Restart-Service -Name WinRM -Force

# Disable broad built-in WinRM inbound rules, then allow DCN only.
Get-NetFirewallRule -ErrorAction SilentlyContinue |
    Where-Object {{ $_.Direction -eq 'Inbound' -and ($_.DisplayGroup -like '*Windows Remote Management*' -or $_.DisplayName -like '*WinRM*') }} |
    Disable-NetFirewallRule -ErrorAction SilentlyContinue
$ruleName = 'DCN-WinRM-{int(winrm_port)}'
Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
New-NetFirewallRule -Name $ruleName -DisplayName 'DCN WinRM (DCN only)' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $WinRmPort `
    -RemoteAddress $DcnSourceIp -Profile Any | Out-Null

# Ensure the listener uses the configured port.
Invoke-WinRm @('enumerate', 'winrm/config/listener')
Write-Output "WinRM enabled. Only DCN ($DcnSourceIp) can access TCP/$WinRmPort."
"""
    return script, source_ip
