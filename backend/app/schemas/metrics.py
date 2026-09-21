"""Server metrics API schemas."""

from pydantic import BaseModel


class DiskUsage(BaseModel):
    mount: str
    size_bytes: int
    used_bytes: int
    pct: float


class DeviceMetricsSummary(BaseModel):
    """列表页:单台服务器最新指标摘要。"""

    device_id: int
    device_name: str
    ip_address: str | None = None
    type: str
    os_system: str | None = None
    rack_id: int | None = None
    rack_name: str = ""
    room_id: int | None = None
    room_name: str = ""
    status: str = "offline"  # monitor 的存活状态
    available: bool = False  # 本轮指标是否采集成功
    error: str | None = None
    fetched_at: str | None = None
    source: str | None = None  # ssh / winrm
    cpu_pct: float | None = None
    mem_pct: float | None = None
    disk_max_pct: float | None = None
    load1: float | None = None
    uptime_sec: int | None = None
    net_rx_bps: float | None = None
    net_tx_bps: float | None = None
    disk_read_bps: float | None = None
    disk_write_bps: float | None = None


class DeviceMetricsListResponse(BaseModel):
    items: list[DeviceMetricsSummary]
    total: int


class DeviceMetricsDetail(DeviceMetricsSummary):
    """详情抽屉:最新指标全量 + 分区明细。"""

    mem_used_mb: int | None = None
    mem_total_mb: int | None = None
    load5: float | None = None
    load15: float | None = None
    disks: list[DiskUsage] = []


class MetricSamplePoint(BaseModel):
    ts: str
    cpu_pct: float | None = None
    mem_pct: float | None = None
    disk_max_pct: float | None = None
    load1: float | None = None
    net_rx_bps: float | None = None
    net_tx_bps: float | None = None
    disk_read_bps: float | None = None
    disk_write_bps: float | None = None


class MetricsHistoryResponse(BaseModel):
    device_id: int
    range: str
    points: list[MetricSamplePoint]


class MetricTrendPoint(BaseModel):
    """概览曲线点:只带 CPU / 内存，避免多设备批量返回时载荷过大。"""

    ts: str
    cpu_pct: float | None = None
    mem_pct: float | None = None


class DeviceMetricTrend(BaseModel):
    """单台服务器的 CPU / 内存历史曲线。"""

    device_id: int
    device_name: str
    points: list[MetricTrendPoint] = []


class MetricsTrendResponse(BaseModel):
    """全部纳管设备(server/cloud_server/host)的曲线集合，一次请求返回。"""

    range: str
    total: int
    series: list[DeviceMetricTrend] = []
