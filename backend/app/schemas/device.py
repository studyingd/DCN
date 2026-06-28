from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.validators import validate_ipv4_address, validate_mac_address

DeviceType = Literal["server", "switch", "router", "firewall", "host"]


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="设备名称")
    type: DeviceType = Field(description="设备类型: server/switch/router/firewall/host")
    ip_address: str | None = Field(None, max_length=45, description="IP地址")
    purpose: str | None = Field(None, max_length=500, description="用途")
    os_system: str | None = Field(None, max_length=128, description="操作系统")
    position_u: int | None = Field(None, ge=1, description="U位位置(从1开始)")
    size_u: int | None = Field(None, ge=1, le=42, description="占用U数(1-42)")
    ssh_port: int = Field(22, ge=1, le=65535, description="SSH端口")
    rdp_port: int = Field(3389, ge=1, le=65535, description="RDP端口")
    web_url: str | None = Field(None, max_length=500, description="管理地址")
    mac_address: str | None = Field(None, max_length=17, description="MAC地址")
    owner: str | None = Field(None, max_length=128, description="负责人")
    credential_id: int | None = Field(None, description="关联凭据ID")

    @field_validator("ip_address")
    @classmethod
    def _validate_ip(cls, v: str | None) -> str | None:
        return validate_ipv4_address(v)

    @field_validator("mac_address")
    @classmethod
    def _validate_mac(cls, v: str | None) -> str | None:
        return validate_mac_address(v)


class DeviceUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    type: DeviceType | None = None
    ip_address: str | None = Field(None, max_length=45)
    purpose: str | None = Field(None, max_length=500)
    os_system: str | None = Field(None, max_length=128)
    position_u: int | None = Field(None, ge=1)
    size_u: int | None = Field(None, ge=1, le=42)
    ssh_port: int | None = Field(None, ge=1, le=65535)
    rdp_port: int | None = Field(None, ge=1, le=65535)
    web_url: str | None = Field(None, max_length=500)
    mac_address: str | None = Field(None, max_length=17)
    owner: str | None = Field(None, max_length=128)
    credential_id: int | None = None

    @field_validator("ip_address")
    @classmethod
    def _validate_ip(cls, v: str | None) -> str | None:
        return validate_ipv4_address(v)

    @field_validator("mac_address")
    @classmethod
    def _validate_mac(cls, v: str | None) -> str | None:
        return validate_mac_address(v)


class DeviceBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rack_id: int
    name: str
    type: str
    ip_address: str | None = None
    purpose: str | None = None
    os_system: str | None = None
    status: str = "offline"
    position_u: int | None = None
    size_u: int | None = None
    ssh_port: int = 22
    rdp_port: int = 3389
    web_url: str | None = None
    mac_address: str | None = None
    owner: str | None = None
    credential_id: int | None = None
    created_at: datetime | None = None

    @field_serializer("created_at")
    @classmethod
    def _serialize_dt(cls, v: datetime | None) -> str | None:
        if v is None:
            return None
        s = v.isoformat()
        if v.tzinfo is None:
            return s + "Z"
        return s


class DeviceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rack_id: int
    name: str
    type: str
    ip_address: str | None = None
    purpose: str | None = None
    os_system: str | None = None
    status: str = "offline"
    position_u: int | None = None
    size_u: int | None = None
    ssh_port: int = 22
    rdp_port: int = 3389
    web_url: str | None = None
    mac_address: str | None = None
    owner: str | None = None
    credential_id: int | None = None
    created_at: datetime | None = None

    @field_serializer("created_at")
    @classmethod
    def _serialize_dt(cls, v: datetime | None) -> str | None:
        if v is None:
            return None
        s = v.isoformat()
        if v.tzinfo is None:
            return s + "Z"
        return s


# ── OS Detection schemas ────────────────────────────────────────────


class OSDetectRequest(BaseModel):
    ip_address: str = Field(min_length=7, max_length=45, description="IP 地址")
    credential_id: int | None = Field(
        None, description="凭据ID（可选，用于精确获取 Linux 发行版）"
    )

    @field_validator("ip_address")
    @classmethod
    def _validate_ip(cls, v: str) -> str:
        result = validate_ipv4_address(v)
        if result is None:
            raise ValueError("请输入有效的 IPv4 地址")
        return result


class OSDetectResponse(BaseModel):
    ip_address: str
    os_system: str  # "linux" / "windows" / "switch" / "router" / "unknown"
    os_version: str  # human-readable, e.g. "Ubuntu 22.04"
    ssh_banner: str | None = None
    ssh_port_open: bool = False
    rdp_port_open: bool = False
    smb_port_open: bool = False
    confidence: str  # "high" / "medium" / "low"
    detail: str  # human-readable summary
