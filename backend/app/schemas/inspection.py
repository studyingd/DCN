from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

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
    device_id: int
    device_name: str
    device_ip: str | None = None
    target_type: str
    vendor: str | None = None
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
    device_ids: list[int]
    mode: str = "standard"  # standard / custom / quick
    items: list[str] | None = None  # only for mode=custom
    timeout: int = 30
    username: str | None = None
    password: str | None = None
    credential_id: int | None = None
    enable_password: str | None = None  # for Cisco/Ruijie

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        if v not in ("standard", "custom", "quick"):
            raise ValueError("mode must be 'standard', 'custom', or 'quick'")
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


# ── Report statistics ──


class InspectionReportResponse(BaseModel):
    total_inspections: int = 0
    total_items_checked: int = 0
    overall_health: dict = {}  # {normal_pct, warning_pct, critical_pct, error_pct}
    by_target_type: dict = {}  # {network: {count, warning_rate, critical_rate}, ...}
    top_warnings: list[dict] = []
    trend: list[dict] = []
