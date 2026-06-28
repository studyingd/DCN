from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.device import DeviceBrief


class RoomCreate(BaseModel):
    name: str
    location: str | None = None
    description: str | None = None
    floor_plan: dict | None = None


class RoomUpdate(BaseModel):
    name: str | None = None
    location: str | None = None
    description: str | None = None
    floor_plan: dict | None = None


class RoomResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    location: str | None = None
    description: str | None = None
    floor_plan: dict | None = None
    created_at: datetime | None = None
    rack_count: int = 0


class RoomDetail(RoomResponse):
    racks: list["RackBrief"] = []


class RackBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    room_id: int
    name: str
    type: str
    position_x: float = 0
    position_y: float = 0
    position_z: float = 0
    rotation: float = 0
    capacity_u: int | None = None
    devices: list["DeviceBrief"] = []


RoomDetail.model_rebuild()
