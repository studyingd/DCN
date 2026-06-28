from pydantic import BaseModel, ConfigDict


class ConnectionCreate(BaseModel):
    device_a_id: int
    device_b_id: int
    conn_type: str | None = None  # ethernet/fiber/serial
    bandwidth: str | None = None
    note: str | None = None


class ConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_a_id: int
    device_b_id: int
    conn_type: str | None = None
    bandwidth: str | None = None
    note: str | None = None
    device_a_name: str | None = None
    device_b_name: str | None = None
