from app.services.pve import guest_agent_fsinfo_summary, guest_agent_summary


class FakePveClient:
    def __init__(self, config: dict, agent_data: dict | None = None):
        self.config = config
        self.agent_data = agent_data or {}
        self.agent_calls = 0

    def guest_config(self, *_args):
        return self.config

    def guest_agent(self, *_args):
        self.agent_calls += 1
        return self.agent_data


def test_qga_disabled_uses_empty_fallback_without_agent_call():
    client = FakePveClient({"agent": "enabled=0,fstrim_cloned_disks=1"})

    result = guest_agent_summary(client, "pve", "qemu", 103)

    assert result["qga_enabled"] is False
    assert result["qga_available"] is False
    assert result["qga_ip_address"] is None
    assert client.agent_calls == 0


def test_qga_normalizes_windows_ip_and_real_disk_usage():
    client = FakePveClient(
        {"agent": "enabled=1,fstrim_cloned_disks=1"},
        {
            "available": True,
            "osinfo": {"result": {"name": "Microsoft Windows 10 Pro"}},
            "interfaces": {
                "result": [
                    {"name": "Loopback", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
                    {
                        "name": "Ethernet",
                        "ip-addresses": [
                            {"ip-address": "fe80::1"},
                            {"ip-address": "192.168.1.127"},
                        ],
                    },
                ]
            },
            "fsinfo": {
                "result": [
                    {
                        "name": "C:\\",
                        "mountpoint": "C:\\",
                        "total-bytes": 1000,
                        "used-bytes": 400,
                    }
                ]
            },
        },
    )

    result = guest_agent_summary(client, "pve", "qemu", 103)

    assert result["qga_enabled"] is True
    assert result["qga_available"] is True
    assert result["qga_os_system"] == "windows"
    assert result["qga_os_name"] == "Microsoft Windows 10 Pro"
    assert result["qga_ip_address"] == "192.168.1.127"
    assert result["qga_disks"] == [
        {
            "mount": "C:\\",
            "filesystem_type": None,
            "total_bytes": 1000,
            "used_bytes": 400,
        }
    ]


def test_qga_filters_optical_filesystems():
    client = FakePveClient(
        {"agent": "1"},
        {
            "available": True,
            "interfaces": {"result": []},
            "osinfo": {"result": {"name": "Microsoft Windows"}},
            "fsinfo": {
                "result": [
                    {
                        "mountpoint": "D:\\",
                        "type": "CDFS",
                        "total-bytes": 100,
                        "used-bytes": 100,
                    },
                    {
                        "mountpoint": "C:\\",
                        "type": "NTFS",
                        "total-bytes": 1000,
                        "used-bytes": 100,
                    },
                ]
            },
        },
    )

    result = guest_agent_summary(client, "pve", "qemu", 103)

    assert [disk["mount"] for disk in result["qga_disks"]] == ["C:\\"]


def test_non_qemu_guest_type_never_calls_qemu_guest_agent():
    """LXC 已移除,防御逻辑保留:非 qemu 类型(含 lxc)一律不调 QGA。"""

    client = FakePveClient({"agent": "1"})

    result = guest_agent_summary(client, "pve", "lxc", 104)

    assert result["qga_enabled"] is False
    assert client.agent_calls == 0


def test_qga_ignores_malformed_filesystem_rows_without_losing_other_facts():
    client = FakePveClient(
        {"agent": "1"},
        {
            "available": True,
            "osinfo": {"result": {"name": "Ubuntu"}},
            "interfaces": {"result": []},
            "fsinfo": {
                "result": [
                    {
                        "mountpoint": "/bad",
                        "total-bytes": "not-a-number",
                        "used-bytes": 1,
                    },
                    {
                        "mountpoint": "/",
                        "total-bytes": 100,
                        "used-bytes": "not-a-number",
                    },
                ]
            },
        },
    )

    result = guest_agent_summary(client, "pve", "qemu", 103)

    assert result["qga_available"] is True
    assert result["qga_os_system"] == "linux"
    assert result["qga_disks"] == [
        {
            "mount": "/",
            "filesystem_type": None,
            "total_bytes": 100,
            "used_bytes": None,
        }
    ]


# ── fsinfo-only 摘要(磁盘采集循环专用,2026-09-17) ──


class FakeFsinfoClient:
    """实现 guest_config + guest_agent_fsinfo 的最小假 client,记录调用。"""

    def __init__(
        self, config: dict, fsinfo: any = None, error: Exception | None = None
    ):
        self.config = config
        self.fsinfo = fsinfo
        self.error = error
        self.fsinfo_calls = 0

    def guest_config(self, *_args):
        return self.config

    def guest_agent_fsinfo(self, *_args):
        self.fsinfo_calls += 1
        if self.error is not None:
            raise self.error
        return self.fsinfo


def test_fsinfo_summary_only_calls_fsinfo_command():
    """磁盘循环只调 get-fsinfo 一条命令——不再为 interfaces/osinfo 付 2/3 的
    QGA 调用(50 台 guest 一轮 150 条 → 50 条)。"""
    fsinfo = {
        "result": [
            {
                "mountpoint": "/",
                "type": "ext4",
                "total-bytes": 1000,
                "used-bytes": 250,
            },
            # 光驱介质不算容量数据
            {"mountpoint": "D:\\", "type": "CDFS", "total-bytes": 40, "used-bytes": 40},
        ]
    }
    client = FakeFsinfoClient({"agent": "1"}, fsinfo=fsinfo)
    result = guest_agent_fsinfo_summary(client, "pve", "qemu", 103)
    assert client.fsinfo_calls == 1
    assert result["qga_enabled"] is True
    assert result["qga_available"] is True
    assert result["qga_disks"] == [
        {
            "mount": "/",
            "filesystem_type": "ext4",
            "total_bytes": 1000,
            "used_bytes": 250,
        }
    ]


def test_fsinfo_summary_agent_disabled_skips_call():
    client = FakeFsinfoClient({"agent": "enabled=0"})
    result = guest_agent_fsinfo_summary(client, "pve", "qemu", 103)
    assert client.fsinfo_calls == 0
    assert result == {"qga_enabled": False, "qga_available": False, "qga_disks": []}


def test_fsinfo_summary_error_falls_back_to_unavailable():
    """fsinfo 失败(agent 未运行/超时)→ qga_available=False,调用方走绑定兜底。"""
    from app.services.pve import PveError

    client = FakeFsinfoClient({"agent": "1"}, error=PveError("agent timeout"))
    result = guest_agent_fsinfo_summary(client, "pve", "qemu", 103)
    assert result["qga_enabled"] is True
    assert result["qga_available"] is False
    assert result["qga_disks"] == []


def test_fsinfo_summary_empty_rows_means_unavailable():
    """fsinfo 成功但没有可用盘行 → 按不可用处理(走 SSH/WinRM 兜底)。"""
    client = FakeFsinfoClient({"agent": "1"}, fsinfo={"result": []})
    result = guest_agent_fsinfo_summary(client, "pve", "qemu", 103)
    assert result["qga_available"] is False


def test_fsinfo_summary_non_qemu_never_calls_qemu_agent():
    """LXC 已移除,防御保留:非 qemu 类型不调 QGA。"""
    client = FakeFsinfoClient({"agent": "1"}, fsinfo={"result": []})
    result = guest_agent_fsinfo_summary(client, "p", "lxc", 200)
    assert client.fsinfo_calls == 0
    assert result["qga_available"] is False


def test_fsinfo_summary_reuses_passed_config_without_extra_fetch():
    """调用方已有 config(如详情页拉过)时直接复用,不再多打一次 guest_config。"""
    fsinfo = {
        "result": [
            {"mountpoint": "/", "type": "ext4", "total-bytes": 10, "used-bytes": 5}
        ]
    }
    client = FakeFsinfoClient({"agent": "1"}, fsinfo=fsinfo)

    class _NoConfigClient(FakeFsinfoClient):
        def guest_config(self, *_args):
            raise AssertionError("不应再拉 guest_config")

    result = guest_agent_fsinfo_summary(
        _NoConfigClient({"agent": "1"}, fsinfo=fsinfo),
        "pve",
        "qemu",
        103,
        config={"agent": "1"},
    )
    assert result["qga_available"] is True
