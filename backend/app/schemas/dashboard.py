"""Pydantic v2 response models for the big-screen dashboard API."""

from pydantic import BaseModel


class OverviewResponse(BaseModel):
    room_count: int
    rack_count: int
    device_total: int
    device_online: int
    device_offline: int
    device_maintenance: int
    user_count: int
    online_user_count: int
    pve_guest_total: int = 0
    pve_guest_running: int = 0
    pve_guest_stopped: int = 0
    pve_platform_count: int = 0
    alert_events_7d: int = 0


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
