from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.validators import validate_ipv4_address

# 机房管理只纳管服务器类资产,设备类型只有 server / cloud_server / host 三种。
DeviceType = Literal["server", "cloud_server", "host"]


class DeviceCredentialInput(BaseModel):
    """Request-only remote credentials saved together with a device."""

    username: str | None = Field(None, max_length=128)
    password: str | None = Field(None, max_length=512)
    ssh_key: str | None = Field(None, max_length=16384)

    @field_validator("username", mode="before")
    @classmethod
    def _trim_username(cls, v: str | None) -> str | None:
        if isinstance(v, str):
            return v.strip() or None
        return v


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="设备名称")
    type: DeviceType = Field(description="设备类型: server/cloud_server/host")
    ip_address: str | None = Field(None, max_length=45, description="IP地址")
    os_system: str | None = Field(None, max_length=128, description="操作系统")
    position_u: int | None = Field(None, ge=1, description="U位位置(从1开始)")
    size_u: int | None = Field(None, ge=1, le=42, description="占用U数(1-42)")
    ssh_port: int = Field(22, ge=1, le=65535, description="SSH端口")
    rdp_port: int = Field(3389, ge=1, le=65535, description="RDP端口")
    winrm_port: int = Field(5985, ge=1, le=65535, description="WinRM端口(Windows)")
    remote_credential: DeviceCredentialInput | None = Field(
        None, description="设备远程连接凭据（保存设备时自动创建/更新）"
    )

    @field_validator("ip_address")
    @classmethod
    def _validate_ip(cls, v: str | None) -> str | None:
        return validate_ipv4_address(v)


class DeviceUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    type: DeviceType | None = None
    ip_address: str | None = Field(None, max_length=45)
    os_system: str | None = Field(None, max_length=128)
    position_u: int | None = Field(None, ge=1)
    size_u: int | None = Field(None, ge=1, le=42)
    ssh_port: int | None = Field(None, ge=1, le=65535)
    rdp_port: int | None = Field(None, ge=1, le=65535)
    winrm_port: int | None = Field(None, ge=1, le=65535)
    remote_credential: DeviceCredentialInput | None = Field(
        None, description="设备远程连接凭据（保存设备时自动创建/更新）"
    )

    @field_validator("ip_address")
    @classmethod
    def _validate_ip(cls, v: str | None) -> str | None:
        return validate_ipv4_address(v)


class DeviceBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rack_id: int
    name: str
    type: str
    ip_address: str | None = None
    os_system: str | None = None
    status: str = "offline"
    position_u: int | None = None
    size_u: int | None = None
    ssh_port: int = 22
    rdp_port: int = 3389
    winrm_port: int = 5985
    credential_username: str | None = None
    has_credential: bool = False
    created_at: datetime | None = None

    @field_serializer("status")
    @classmethod
    def _serialize_status(cls, v: str) -> str:
        # Device health has exactly two states. Treat legacy/unknown values
        # (including the removed "maintenance" state) as offline until the
        # monitor confirms the device is reachable.
        return "online" if str(v).lower() == "online" else "offline"

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
    os_system: str | None = None
    status: str = "offline"
    position_u: int | None = None
    size_u: int | None = None
    ssh_port: int = 22
    rdp_port: int = 3389
    winrm_port: int = 5985
    credential_username: str | None = None
    has_credential: bool = False
    # Windows 且最近一轮指标采集明确失败(WinRM 确认不通);由路由层填充,
    # from_attributes 模型上恒为 None——告警中心等选择器据此置灰目标。
    metrics_failed: bool | None = None
    created_at: datetime | None = None

    @field_serializer("status")
    @classmethod
    def _serialize_status(cls, v: str) -> str:
        return "online" if str(v).lower() == "online" else "offline"

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
    device_id: int | None = Field(
        None, ge=1, description="编辑设备时用于校验已绑定凭据"
    )
    username: str | None = Field(None, max_length=128)
    password: str | None = Field(None, max_length=512)
    winrm_port: int | None = Field(
        None, ge=1, le=65535, description="WinRM 端口(可选,默认取全局配置)"
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
    os_system: str  # "linux" / "windows" / "unknown"
    os_version: str  # human-readable, e.g. "Ubuntu 22.04"
    ssh_banner: str | None = None
    ssh_port_open: bool = False
    winrm_port_open: bool = False
    rdp_port_open: bool = False
    smb_port_open: bool = False
    confidence: str  # "high" / "medium" / "low"
    detail: str  # human-readable summary
