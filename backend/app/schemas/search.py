"""顶栏全局搜索(设备 + PVE 虚拟机)。"""

from pydantic import BaseModel


class SearchDeviceItem(BaseModel):
    id: int
    name: str
    type: str
    status: str
    ip_address: str | None = None
    room_id: int
    room_name: str
    rack_id: int
    rack_name: str


class SearchGuestItem(BaseModel):
    connection_id: int
    connection_name: str
    guest_type: str
    vmid: int
    name: str
    node: str | None = None
    status: str = "unknown"
    ip_address: str | None = None
    last_known_ip: str | None = None


class SearchResponse(BaseModel):
    devices: list[SearchDeviceItem]
    guests: list[SearchGuestItem]
