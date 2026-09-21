"""业务监控 API 的 Pydantic schema。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.validators import validate_outbound_url


def _validate_http_url(v: str) -> str:
    return validate_outbound_url(v)


# ── 业务 ──


class BusinessCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None


class BusinessUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = None


class BusinessBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    created_at: datetime | None = None


class BusinessHealthItem(BusinessBrief):
    """业务列表项:带服务器(含 PVE 虚拟机)/接口在线统计与聚合健康状态。"""

    server_total: int = 0
    server_online: int = 0
    interface_total: int = 0
    interface_up: int = 0
    health: str = "unknown"  # healthy / degraded / down / unknown


class ServerInBusiness(BaseModel):
    """业务下的服务器条目 —— 机房设备与 PVE 虚拟机/容器统一展示。

    ``kind='device'`` 为 ``devices`` 记录，移除时用 ``device_id``;
    ``kind='pve'`` 为 PVE guest，移除时用 ``link_id``(关联行 id)。
    状态取自 monitor 实时结果或 PVE ``/cluster/resources`` 内存快照;
    ``reachable=0`` 表示所属 PVE 连接不可达，此时 ``status='unknown'``
    (界面显示"状态未知"，且不计入业务健康度分母)。
    """

    kind: str = "device"  # device / pve
    device_id: int | None = None
    link_id: int | None = None
    name: str
    ip_address: str | None = None
    status: str = "offline"  # device: online/offline;pve: running/stopped/unknown
    online: int = 0
    reachable: int = 1
    # 以下字段仅 kind == "pve" 有值
    connection_id: int | None = None
    connection_name: str | None = None
    guest_type: str | None = None  # qemu
    vmid: int | None = None
    node: str | None = None
    os_system: str | None = None
    cpu: float | None = None
    mem: int | None = None
    maxmem: int | None = None


class PveGuestRef(BaseModel):
    """关联入参:PVE guest 的稳定身份。"""

    connection_id: int = Field(ge=1)
    # LXC 已移除:只接受 qemu(旧客户端传 lxc 会 422,属预期)。
    guest_type: str = Field(pattern="^qemu$")
    vmid: int = Field(ge=0)
    name: str | None = Field(None, max_length=255)


class PveGuestBatch(BaseModel):
    items: list[PveGuestRef] = Field(min_length=1, max_length=200)

    @field_validator("items")
    @classmethod
    def _dedupe(cls, values: list[PveGuestRef]) -> list[PveGuestRef]:
        seen: set[tuple[int, str, int]] = set()
        unique: list[PveGuestRef] = []
        for item in values:
            key = (item.connection_id, item.guest_type, item.vmid)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique


class PveGuestCandidate(BaseModel):
    """可关联的 PVE 虚拟机/容器候选项。"""

    connection_id: int
    connection_name: str
    guest_type: str
    vmid: int
    name: str
    node: str | None = None
    status: str = "unknown"
    ip_address: str | None = None
    os_system: str | None = None
    cpu: float | None = None
    mem: int | None = None
    maxmem: int | None = None
    reachable: int = 1


class InterfaceInBusiness(BaseModel):
    interface_id: int
    name: str
    url: str
    method: str
    expected_status: int
    timeout: int = 10
    enabled: int
    request_headers: dict[str, str] = Field(default_factory=dict)
    auth_type: str = "none"
    auth_username: str | None = None
    has_auth_secret: bool = False
    request_body: str | None = None
    response_contains: str | None = None
    description: str | None = None
    up: int = 0
    status_code: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    checked_at: str | None = None


class BusinessDetail(BusinessBrief):
    servers: list[ServerInBusiness] = []
    interfaces: list[InterfaceInBusiness] = []
    server_total: int = 0
    server_online: int = 0
    interface_total: int = 0
    interface_up: int = 0
    health: str = "unknown"


class IdBatch(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)

    @field_validator("ids")
    @classmethod
    def _check_ids(cls, values: list[int]) -> list[int]:
        if any(value < 1 for value in values):
            raise ValueError("ID 必须为正整数")
        return list(dict.fromkeys(values))


# ── 接口 ──


class InterfaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=500)
    method: str = "GET"
    expected_status: int = Field(200, ge=100, le=599)
    timeout: int = Field(10, ge=1, le=60)
    enabled: int = 1
    request_headers: dict[str, str] = Field(default_factory=dict)
    auth_type: str = "none"
    auth_username: str | None = Field(None, max_length=255)
    auth_password: str | None = None
    auth_token: str | None = None
    request_body: str | None = Field(None, max_length=10000)
    response_contains: str | None = Field(None, max_length=1000)
    description: str | None = Field(None, max_length=500)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        return _validate_http_url(v)

    @field_validator("method")
    @classmethod
    def _check_method(cls, v: str) -> str:
        m = v.upper()
        if m not in ("GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS"):
            raise ValueError("不支持的 HTTP 方法")
        return m

    @field_validator("auth_type")
    @classmethod
    def _check_auth_type(cls, v: str) -> str:
        v = v.lower()
        if v not in ("none", "basic", "bearer"):
            raise ValueError("认证类型必须是 none、basic 或 bearer")
        return v


class InterfaceUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    url: str | None = Field(None, min_length=1, max_length=500)
    method: str | None = None
    expected_status: int | None = Field(None, ge=100, le=599)
    timeout: int | None = Field(None, ge=1, le=60)
    enabled: int | None = None
    request_headers: dict[str, str] | None = None
    auth_type: str | None = None
    auth_username: str | None = Field(None, max_length=255)
    auth_password: str | None = None
    auth_token: str | None = None
    request_body: str | None = Field(None, max_length=10000)
    response_contains: str | None = Field(None, max_length=1000)
    description: str | None = Field(None, max_length=500)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        return _validate_http_url(v) if v is not None else v

    @field_validator("method")
    @classmethod
    def _check_method(cls, v: str | None) -> str | None:
        if v is None:
            return v
        m = v.upper()
        if m not in ("GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS"):
            raise ValueError("不支持的 HTTP 方法")
        return m

    @field_validator("auth_type")
    @classmethod
    def _check_update_auth_type(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.lower()
        if v not in ("none", "basic", "bearer"):
            raise ValueError("认证类型必须是 none、basic 或 bearer")
        return v
