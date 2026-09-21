"""PVE guest IO 速率的进程内差分计算器。

``/cluster/resources`` 返回的 disk/net 计数器是**开机以来的累计值**，速率必须由
相邻两次采样的差分派生。旧实现把基线存在浏览器组件里(``previousGuestCounters``)，
每次打开页面都要空转一轮"采样中";现在基线挪到后端进程——常驻采集循环一直喂
样本,请求路径拿到的 guest 直接带速率,打开页面即显示真实值。

基线为 None(后端刚重启、guest 首次入列)时速率字段为 null,前端显示「采样中」;
计数器回绕(虚机重启)速率同样为 null,这是"速率不可知"而非数据缺失。
多副本部署时各副本各自攒基线,互不干扰,无需共享存储。
"""

import threading
import time

# (connection_id, guest_type, vmid) -> 基线(含上次算出的速率)
_baselines: dict[tuple[int, str, int], dict] = {}
_lock = threading.Lock()

_RATE_FIELDS = ("disk_read_rate", "disk_write_rate", "net_in_rate", "net_out_rate")


def compute_rates(
    connection_id: int, guests: list[dict], *, now: float | None = None
) -> list[dict]:
    """对一批 cluster/resources 原始 guest 记录差分出速率。

    就地修改并返回同一批 dict:每个运行中的 guest 追加 4 个速率字段与
    ``net_rate_unavailable`` / ``disk_rate_unavailable`` 标记;模板机、非运行
    guest、首次见到的 guest 不追加速率字段(前端按「采样中/不适用」展示)。
    幂等:同批数据重复喂,计数器不变时保留上次速率,不会归零闪烁。
    """
    current_keys: set[tuple[int, str, int]] = set()
    now = now if now is not None else time.time()
    for raw in guests:
        vmid = raw.get("vmid")
        if vmid is None:
            continue
        key = (int(connection_id), str(raw.get("type") or ""), int(vmid))
        current_keys.add(key)
        if raw.get("template") or raw.get("status") != "running":
            continue

        current = {
            "timestamp": now,
            "diskread": int(raw.get("diskread") or 0),
            "diskwrite": int(raw.get("diskwrite") or 0),
            "netin": int(raw.get("netin") or 0),
            "netout": int(raw.get("netout") or 0),
        }
        with _lock:
            previous = _baselines.get(key)
            rates = _diff(previous, current)
            _baselines[key] = {**current, **rates}

        for field, value in rates.items():
            raw[field] = value
        raw["disk_rate_unavailable"] = rates["disk_read_rate"] is None
        raw["net_rate_unavailable"] = rates["net_in_rate"] is None

    _drop_stale(connection_id, current_keys)
    return guests


def get_baseline_rates(connection_id: int, guest_type: str, vmid: int) -> dict | None:
    """单个 guest 上次算出的速率(不含累计计数器);无基线返回 None。"""
    key = (int(connection_id), str(guest_type), int(vmid))
    with _lock:
        baseline = _baselines.get(key)
        if not baseline or all(baseline.get(f) is None for f in _RATE_FIELDS):
            return None
        return {f: baseline[f] for f in _RATE_FIELDS}


def reset() -> None:
    """清空基线(测试用)。"""
    with _lock:
        _baselines.clear()


def _drop_stale(connection_id: int, current_keys: set[tuple[int, str, int]]) -> None:
    """清理该连接下已不存在的 guest 基线,防止 Map 无界增长。"""
    prefix = int(connection_id)
    with _lock:
        for key in [k for k in _baselines if k[0] == prefix and k not in current_keys]:
            _baselines.pop(key, None)


def _diff(previous: dict | None, current: dict) -> dict:
    """差分四次采样算速率;语义与旧前端实现逐条对齐。"""
    if previous is None:
        return {f: None for f in _RATE_FIELDS}

    seconds = max(current["timestamp"] - previous["timestamp"], 1.0)

    def rate(counter: str) -> float | None:
        delta = current[counter] - previous[counter]
        if delta < 0:
            # 计数器回绕(虚机重启):速率不可知
            return None
        return delta / seconds

    counters_unchanged = all(
        current[c] == previous[c] for c in ("diskread", "diskwrite", "netin", "netout")
    )
    if counters_unchanged:
        # PVE 计数器约 10s 才刷新一档,与轮询间隔几乎同频,"不变"是常态而非 0:
        # 有上次速率则保留,没有则按 0 算(空闲虚机速率本来就该是 0)。
        rates = {}
        for f in _RATE_FIELDS:
            prev_rate = previous.get(f)
            rates[f] = prev_rate if prev_rate is not None else 0.0
        return rates

    return {
        "disk_read_rate": rate("diskread"),
        "disk_write_rate": rate("diskwrite"),
        "net_in_rate": rate("netin"),
        "net_out_rate": rate("netout"),
    }
