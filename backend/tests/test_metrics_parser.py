"""metrics_parser 单元测试:Linux/Windows 采集输出 → 结构化指标。"""

from app.services.metrics_parser import parse_linux_metrics, parse_windows_metrics

LINUX_SAMPLE = """@@cpu1
cpu  100 0 50 800 20 0 5 0 0 0
@@cpu2
cpu  110 0 60 900 25 0 6 0 0 0
@@mem
MemTotal:       16384000 kB
MemAvailable:    8192000 kB
@@disk
Filesystem     1B-blocks       Used  Available Capacity Mounted on
/dev/sda1      107374182400 53687091200 48318382080      50% /
tmpfs            838860800         0  838860800       0% /run
overlay        10737418240  1073741824 9663676416      10% /var/lib/docker/overlay2/abc
@@diskio
   8       0 sda 5000 100 200000 3000 8000 50 400000 6000 0 2000 9000
   8       1 sda1 999 0 9999 10 999 0 99999 20 0 10 30
   7       0 loop0 1 0 2 0 0 0 0 0 0 0 0
@@load
0.15 0.10 0.06 1/234 5678
@@uptime
123456.78 234567.89
@@net
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 1000 10 0 0 0 0 0 0 1000 10 0 0 0 0 0 0
  eth0: 1000000 100 0 0 0 0 0 0 2000000 200 0 0 0 0 0 0
"""


def test_linux_full_parse():
    r = parse_linux_metrics(LINUX_SAMPLE)
    assert r["ok"] is True
    # d_idle=(900+25)-(800+20)=105,d_total=1196-1070=126 → 16.7%
    assert r["cpu_pct"] == 16.7
    assert r["mem_pct"] == 50.0
    assert r["mem_used_mb"] == 8000
    assert r["mem_total_mb"] == 16000
    # tmpfs / overlay 被过滤,只剩根分区
    assert r["disks"] == [
        {
            "mount": "/",
            "size_bytes": 107374182400,
            "used_bytes": 53687091200,
            "pct": 50.0,
        }
    ]
    assert r["disk_max_pct"] == 50.0
    assert (r["load1"], r["load5"], r["load15"]) == (0.15, 0.10, 0.06)
    assert r["uptime_sec"] == 123456
    # lo 回环不计入
    assert r["net_rx_bytes"] == 1000000
    assert r["net_tx_bytes"] == 2000000
    # 磁盘 IO:sda 整盘计入,sda1 分区与 loop0 跳过;扇区 ×512
    assert r["diskio_read_bytes"] == 200000 * 512
    assert r["diskio_write_bytes"] == 400000 * 512


def test_linux_empty_input():
    r = parse_linux_metrics("")
    assert r["ok"] is False
    assert r["cpu_pct"] is None
    assert r["disks"] == []


def test_linux_garbage_input():
    r = parse_linux_metrics("bash: command not found")
    assert r["ok"] is False


def test_linux_cpu_counter_reset():
    """设备重启导致计数器回退(d_total<0)时 CPU 应为 None 而非负数。"""
    text = "@@cpu1\ncpu  500 0 0 500 0 0 0 0\n@@cpu2\ncpu  10 0 0 10 0 0 0 0"
    r = parse_linux_metrics(text)
    assert r["cpu_pct"] is None


def test_linux_partial_sections_ok():
    """只有 mem 段也算采集成功(其他指标为 None)。"""
    r = parse_linux_metrics("@@mem\nMemTotal: 8192000 kB\nMemAvailable: 4096000 kB")
    assert r["ok"] is True
    assert r["mem_pct"] == 50.0
    assert r["cpu_pct"] is None


WINDOWS_SAMPLE = """cpu_pct=23
mem_total_mb=16384
mem_free_mb=8192
uptime_sec=987654
caption=Microsoft Windows Server 2019 Standard
disk=C:,1099511627776,549755813888
disk=D:,0,0
netif=Ethernet,1024.5,2048.5
netif=Ethernet 2,100,200
diskio=0 C:,1024,2048,3
diskio=_Total,9999,9999,9
"""


def test_windows_full_parse():
    r = parse_windows_metrics(WINDOWS_SAMPLE)
    assert r["ok"] is True
    assert r["cpu_pct"] == 23.0
    assert r["mem_pct"] == 50.0
    assert r["mem_used_mb"] == 8192
    assert r["mem_total_mb"] == 16384
    # D: 容量为 0 被跳过
    assert r["disks"] == [
        {
            "mount": "C:",
            "size_bytes": 1099511627776,
            "used_bytes": 549755813888,
            "pct": 50.0,
        }
    ]
    assert r["disk_max_pct"] == 50.0
    assert r["uptime_sec"] == 987654
    assert r["os_caption"] == "Microsoft Windows Server 2019 Standard"
    # 多网卡速率求和
    assert r["net_rx_bps"] == 1124.5
    assert r["net_tx_bps"] == 2248.5
    # 磁盘 IO:_Total 行跳过,只计物理盘
    assert r["disk_read_bps"] == 1024.0
    assert r["disk_write_bps"] == 2048.0
    # Windows 无 load 指标
    assert r["load1"] is None


def test_windows_empty_input():
    r = parse_windows_metrics("")
    assert r["ok"] is False


def test_windows_partial_mem_only():
    r = parse_windows_metrics("mem_total_mb=8192\nmem_free_mb=4096")
    assert r["ok"] is True
    assert r["mem_pct"] == 50.0
    assert r["cpu_pct"] is None
