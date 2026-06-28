"""Pydantic v2 response models for the big-screen dashboard API."""

from pydantic import BaseModel


class OverviewResponse(BaseModel):
    room_count: int
    rack_count: int
    device_total: int
    device_online: int
    device_offline: int
    device_maintenance: int
    connection_count: int
    user_count: int
    online_user_count: int


class DeviceTypeItem(BaseModel):
    type: str
    label: str
    count: int
    online: int
    offline: int
    maintenance: int


class DeviceTypeDistributionResponse(BaseModel):
    distribution: list[DeviceTypeItem]


class RoomSummaryItem(BaseModel):
    id: int
    name: str
    location: str | None
    rack_count: int
    device_count: int
    device_online: int
    device_offline: int
    device_maintenance: int


class RoomSummaryResponse(BaseModel):
    rooms: list[RoomSummaryItem]


class TopologyNode(BaseModel):
    id: int
    name: str
    type: str
    ip: str | None
    status: str
    room_id: int | None
    room_name: str | None


class TopologyEdge(BaseModel):
    source: int
    target: int
    conn_type: str | None
    bandwidth: str | None


class ConnectionTopologyResponse(BaseModel):
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]


class AuditEventItem(BaseModel):
    id: int
    event_type: str
    username: str | None
    device_name: str | None
    command: str | None
    created_at: str


class AuditTimelineResponse(BaseModel):
    recent_events: list[AuditEventItem]
    event_counts_today: dict[str, int]


class LoginTrendItem(BaseModel):
    date: str
    login_count: int


class LoginTrendResponse(BaseModel):
    trend: list[LoginTrendItem]
