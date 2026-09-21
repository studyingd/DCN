from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

# ── Item-level result ──


class InspectionItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    record_id: int
    item_type: str
    success: bool
    value: str | None = None
    unit: str | None = None
    status: str  # normal / warning / critical / error
    details: dict | None = None
    raw_output: str | None = None
    error_message: str | None = None
    command_used: str | None = None
    executed_at: datetime | None = None
    duration_ms: int | None = None

    @field_serializer("executed_at")
    @classmethod
    def _serialize_dt(cls, v: datetime | None) -> str | None:
        if v is None:
            return None
        s = v.isoformat()
        if v.tzinfo is None:
            return s + "Z"
        return s


# ── Record-level (without items) ──


class InspectionRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    # PVE 虚拟机巡检记录 device_id 为 NULL(迁移 0036),必须允许 None,
    # 否则 model_validate 直接 ValidationError → 详情接口 500
    device_id: int | None = None
    device_name: str
    device_ip: str | None = None
    target_type: str
    # 巡检时留档的精确 OS 名(迁移 0037;历史记录为 None)
    os_name: str | None = None
    mode: str
    status: str
    total_items: int = 0
    normal_count: int = 0
    warning_count: int = 0
    critical_count: int = 0
    error_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    triggered_by: int | None = None
    batch_id: str | None = None

    @field_serializer("started_at", "finished_at")
    @classmethod
    def _serialize_dt(cls, v: datetime | None) -> str | None:
        if v is None:
            return None
        s = v.isoformat()
        if v.tzinfo is None:
            return s + "Z"
        return s


# ── Record with items (detail view) ──


class InspectionRecordDetail(InspectionRecordResponse):
    items: list[InspectionItemResponse] = []


# ── Run request ──


class InspectionRunRequest(BaseModel):
    device_ids: list[int] = Field(min_length=1, max_length=500)
    mode: str = "standard"  # standard / custom / quick / core
    items: list[str] | None = None  # only for mode=custom
    timeout: int = Field(default=30, ge=1, le=300)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=4096)
    ssh_key: str | None = Field(default=None, max_length=16384)
    credential_id: int | None = None

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        if v not in ("standard", "custom", "quick", "core"):
            raise ValueError("mode must be 'standard', 'custom', 'quick', or 'core'")
        return v

    @field_validator("device_ids")
    @classmethod
    def validate_device_ids(cls, v):
        if not v:
            raise ValueError("请至少选择一台设备")
        return v


# ── Paginated history response ──


class InspectionRecordListResponse(BaseModel):
    items: list[InspectionRecordResponse]
    total: int
    page: int
    page_size: int


# ── Items catalog (for custom mode UI) ──


class InspectionItemInfo(BaseModel):
    type: str
    label: str
    quick: bool = False  # whether included in quick mode


class InspectionItemsCatalogResponse(BaseModel):
    target_type: str
    items: list[InspectionItemInfo]


# ── 分指标告警阈值（前端按 item_type 查表配色）──
#
# items 的 key 是巡检项 item_type（cpu / memory / disk / failed_services），
# 不是指标页那套 cpu_pct / mem_pct / disk_max_pct 字段名——两套命名空间之间的映射
# 由前端负责。数值原样来自 app/services/inspection_parser.py 的 THRESHOLDS，
# 后端不在别处再存一份，免得同一个百分比又出现两处真相。


class MetricThreshold(BaseModel):
    warning: float
    critical: float


class MetricThresholdsResponse(BaseModel):
    items: dict[str, MetricThreshold]


# ── Report statistics ──


class InspectionReportResponse(BaseModel):
    total_inspections: int = 0
    total_items_checked: int = 0
    overall_health: dict = {}  # {normal_pct, warning_pct, critical_pct, error_pct}
    by_target_type: dict = {}  # {linux: {count, warning_rate, critical_rate}, ...}
    top_warnings: list[dict] = []
    trend: list[dict] = []
