from pydantic import BaseModel, ConfigDict


class RackCreate(BaseModel):
    name: str
    type: str  # 'cabinet' or 'shelf'
    position_x: float = 0
    position_y: float = 0
    position_z: float = 0
    rotation: float = 0
    capacity_u: int | None = None


class RackUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    position_x: float | None = None
    position_y: float | None = None
    position_z: float | None = None
    rotation: float | None = None
    capacity_u: int | None = None


class RackResponse(BaseModel):
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
    device_count: int = 0
