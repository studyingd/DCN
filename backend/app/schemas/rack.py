from pydantic import BaseModel, ConfigDict


class RackCreate(BaseModel):
    name: str
    type: str  # 'cabinet' or 'shelf'
    capacity_u: int | None = None


class RackUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    capacity_u: int | None = None


class RackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    room_id: int
    name: str
    type: str
    capacity_u: int | None = None
    device_count: int = 0
