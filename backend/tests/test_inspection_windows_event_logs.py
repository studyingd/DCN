"""Windows 事件日志（event_logs）防误报测试。

判定口径（系统性方案，不再靠逐个补噪声表）：
  1. 命令侧只取最近 24 小时；
  2. 解析器按「来源+事件ID」聚类，爆发 N 次只算 1 类，按类数 > 5 判 warning；
  3. 每天复发的慢性噪声（TPM-WMI 1801 / Schannel 36874 / DCOM 10010 等）按噪声表
     剔除，filtered_noise 记账；raw_output 保留全部原始输出。
"""

from app.services.inspection_parser import _parse_event_logs

# 用户实测输出的新格式等价物：时间|来源|事件ID|摘要
DCOM_10010 = "2026-09-09 15:35:03|DCOM|10010|The description for Event ID '10010' in Source 'DCOM' cannot be found."
SCM_7030 = (
    "2026-09-08 14:36:24|Service Control Manager|7030|"
    "Printer Extensions and Notifications 服务标记为交互服务。但是系统配置成不允许交互服务。"
)
SCM_7023_SPOOLER = "2026-09-08 14:34:29|Service Control Manager|7023|Spooler 服务因下列错误而停止: %%2147942414"
SCM_7023_IPHLPSVC = "2026-09-08 14:34:29|Service Control Manager|7023|IP Helper 服务因下列错误而停止: %%21"
REAL_ERROR = "2026-09-09 15:40:00|Application Error|1000|故障应用程序名称: svchost.exe 异常代码: 0xc0000005"
TPM_WMI_1801 = (
    "2026-09-11 03:40:22|Microsoft-Windows-TPM-WMI|1801|"
    "需要更新安全启动 CA/密钥。此设备签名信息包含在此处。 DeviceAttributes： BaseBoardManufacturer:;FirmwareManufacturer:SeaBIOS"
)
SCHANNEL_36874 = (
    "2026-08-23 10:58:49|Schannel|36874|"
    "从远程客户端应用程序收到一个 TLS 1.1 连接请求，但没有任何受客户端应用程序支持的密码套件是受服务器支持的。TLS 连接请求失败。"
)


def _distinct_errors(n: int) -> list[str]:
    """造 n 类不同的真实错误（不同来源+事件ID）。"""
    return [
        f"2026-09-09 15:40:{i:02d}|Source{i}|{1000 + i}|错误摘要 {i}" for i in range(n)
    ]


def test_known_noise_is_filtered_and_counted():
    """用户实测那批慢性噪声应全部被剔除，状态回落 normal。"""
    raw = "\n".join([DCOM_10010] * 5 + [SCM_7030, SCM_7023_IPHLPSVC, SCM_7023_SPOOLER])
    parsed = _parse_event_logs(raw, "windows")
    assert parsed["value"] == "0"
    assert parsed["status"] == "normal"
    assert parsed["details"]["error_count"] == 0
    assert parsed["details"]["filtered_noise"] == 8


def test_tpm_wmi_1801_is_noise():
    """SeaBIOS 虚拟机的安全启动 CA/密钥提示，虚拟化平台通病。"""
    parsed = _parse_event_logs("\n".join([TPM_WMI_1801] * 5), "windows")
    assert parsed["value"] == "0"
    assert parsed["status"] == "normal"
    assert parsed["details"]["filtered_noise"] == 5


def test_schannel_36874_burst_is_noise():
    """远端低版本 TLS 握手失败爆发整体剔除，不占采样窗口。"""
    raw = "\n".join([SCHANNEL_36874] * 32 + [REAL_ERROR])
    parsed = _parse_event_logs(raw, "windows")
    assert parsed["value"] == "1"
    assert parsed["status"] == "normal"
    assert parsed["details"]["filtered_noise"] == 32
    assert "Schannel" not in parsed["details"]["output"]


def test_burst_aggregates_to_single_class():
    """同一事件爆发 N 次只算 1 类，并带次数前缀——无需认识这个事件 ID。"""
    raw = "\n".join([REAL_ERROR] * 40)
    parsed = _parse_event_logs(raw, "windows")
    assert parsed["value"] == "1"
    assert parsed["unit"] == "类"
    assert parsed["status"] == "normal"
    assert parsed["details"]["output"].startswith("40× ")


def test_many_distinct_classes_warn():
    """24h 内出现 6 类不同错误，是真出问题。"""
    parsed = _parse_event_logs("\n".join(_distinct_errors(6)), "windows")
    assert parsed["value"] == "6"
    assert parsed["status"] == "warning"
    assert parsed["details"]["filtered_noise"] == 0


def test_five_distinct_classes_stay_normal():
    parsed = _parse_event_logs("\n".join(_distinct_errors(5)), "windows")
    assert parsed["value"] == "5"
    assert parsed["status"] == "normal"


def test_7023_unknown_service_or_code_is_kept():
    """7023 只有已知自恢复服务 + 已知错误码才滤，其它组合必须保留（宁可多报）。"""
    unknown_svc = "2026-09-08 14:34:29|Service Control Manager|7023|w3wp 服务因下列错误而停止: %%21"
    unknown_code = "2026-09-08 14:34:29|Service Control Manager|7023|Spooler 服务因下列错误而停止: %%55"
    parsed = _parse_event_logs("\n".join([unknown_svc, unknown_code]), "windows")
    # 同属 (SCM, 7023) 聚成 1 类，但都不能进噪声表
    assert parsed["value"] == "1"
    assert parsed["details"]["filtered_noise"] == 0


def test_unparseable_lines_fail_open():
    """无法按新格式解析的行每行单独成类，不漏报。"""
    raw = "TimeGenerated Source Msg\n一些旧格式的残留输出\n\n"
    parsed = _parse_event_logs(raw, "windows")
    assert parsed["value"] == "2"
    assert parsed["details"]["filtered_noise"] == 0


def test_classes_capped_at_20():
    parsed = _parse_event_logs("\n".join(_distinct_errors(25)), "windows")
    assert parsed["value"] == "20"
    assert parsed["details"]["error_count"] == 20


def test_class_sample_is_newest_and_sorted_by_count():
    """同类样本取最新一条（命令按时间倒序输出），展示按次数降序。"""
    few = "2026-09-09 15:40:00|RareSrc|42|稀罕错误"
    many_new = "2026-09-09 15:50:00|FreqSrc|7|频繁错误(新)"
    many_old = "2026-09-09 01:00:00|FreqSrc|7|频繁错误(旧)"
    raw = "\n".join([many_new, few, many_old, many_old])
    parsed = _parse_event_logs(raw, "windows")
    lines = parsed["details"]["output"].splitlines()
    assert lines[0].startswith("3× 2026-09-09 15:50:00|FreqSrc|7|")
    assert lines[1] == few
