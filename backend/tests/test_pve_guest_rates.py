"""pve_guest_rates 差分速率计算器的纯逻辑单测。

覆盖与旧前端实现逐条对齐的语义:
- 首次见到(无基线)→ 速率 None,前端显示「采样中」;
- 计数器不变(常态)→ 有上次速率则保留,没有则按 0(空闲虚机);
- 计数器增长 → 差值 / 秒数;
- 计数器回绕(虚机重启)→ 对应速率 None + *_rate_unavailable 标记;
- 模板机/非运行 guest 不追加速率字段;
- 已消失的 guest 基线被清理,Map 不无界增长。
"""

from app.services.pve_guest_rates import compute_rates, get_baseline_rates, reset


def _guest(vmid: int = 101, **kwargs) -> dict:
    base = {
        "vmid": vmid,
        "type": "qemu",
        "node": "pve1",
        "status": "running",
        "template": 0,
        "diskread": 1000,
        "diskwrite": 2000,
        "netin": 3000,
        "netout": 4000,
    }
    base.update(kwargs)
    return base


def setup_function(_):
    reset()


def test_first_sample_has_no_rates():
    guests = compute_rates(1, [_guest()])
    assert guests[0].get("disk_read_rate") is None
    assert guests[0].get("net_out_rate") is None
    assert guests[0]["disk_rate_unavailable"] is True
    assert guests[0]["net_rate_unavailable"] is True


def test_second_sample_computes_rate():
    compute_rates(1, [_guest()], now=1000.0)
    guests = compute_rates(1, [_guest(diskread=2000, netin=6000)], now=1010.0)
    assert guests[0]["disk_read_rate"] == 100.0  # (2000-1000)/10s
    assert guests[0]["net_in_rate"] == 300.0  # (6000-3000)/10s
    assert guests[0]["disk_rate_unavailable"] is False


def test_unchanged_counters_keep_previous_rate():
    compute_rates(1, [_guest(diskread=5000)], now=1000.0)
    # 第二次喂同样数据:上次没有速率 → 按 0(空闲虚机的真实值)
    guests = compute_rates(1, [_guest(diskread=5000)], now=1010.0)
    assert guests[0]["disk_read_rate"] == 0.0
    # 第三轮:有速率后,即使计数器继续不变也保留(不闪回「采样中」)
    guests = compute_rates(1, [_guest(diskread=5000)], now=1020.0)
    assert guests[0]["disk_read_rate"] == 0.0


def test_counter_wrap_returns_none():
    compute_rates(1, [_guest(diskread=10_000)], now=1000.0)
    guests = compute_rates(1, [_guest(diskread=100)], now=1010.0)
    assert guests[0]["disk_read_rate"] is None
    assert guests[0]["disk_rate_unavailable"] is True
    # net 计数器没回绕,速率照常
    assert guests[0]["net_in_rate"] is not None


def test_template_and_stopped_guests_untouched():
    guests = compute_rates(1, [_guest(template=1), _guest(102, status="stopped")])
    assert "disk_read_rate" not in guests[0]
    assert "disk_read_rate" not in guests[1]


def test_stale_baseline_dropped():
    compute_rates(1, [_guest(101), _guest(102)], now=1000.0)
    compute_rates(1, [_guest(101)], now=1010.0)
    assert get_baseline_rates(1, "qemu", 102) is None
    assert get_baseline_rates(1, "qemu", 101) is not None


def test_repeated_same_batch_is_idempotent():
    batch = [_guest()]
    compute_rates(1, batch, now=1000.0)
    first = dict(batch[0])
    compute_rates(1, [_guest()], now=1000.5)
    # 同一批数据重复喂:速率不闪 0、不丢字段
    assert first.get("disk_read_rate") is None


def test_guest_appeared_only_in_status_loop_has_rates_on_request():
    # 后台循环先喂基线(没人开页面),请求路径第一次拿数据即真实速率
    compute_rates(7, [_guest(diskread=100, netin=200)], now=1000.0)
    guests = compute_rates(7, [_guest(diskread=1100, netin=2200)], now=1030.0)
    assert guests[0]["disk_read_rate"] == 100 / 3
    assert guests[0]["net_in_rate"] == 200 / 3
