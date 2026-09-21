"""PVE 连接相关 schema。"""

import ipaddress
import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_PVE_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


def _validate_pve_host(value: str) -> str:
    """Validate a host/IP without allowing URL/path injection."""
    host = value.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1].strip()
    if not host:
        raise ValueError("host 不能为空")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _PVE_HOST_RE.fullmatch(host):
            raise ValueError("host 只允许 IP 或主机名,不能包含 URL、路径或特殊字符")
    return host


class PveConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(8006, ge=1, le=65535)
    token_id: str = Field(
        min_length=1, max_length=255, description="形如 root@pam!tokenid"
    )
    token_secret: str = Field(min_length=1, max_length=255)
    verify_ssl: int = Field(1, ge=0, le=1)
    enabled: int = Field(1, ge=0, le=1)
    description: str | None = Field(None, max_length=500)

    @field_validator("token_id")
    @classmethod
    def _check_token_id(cls, v: str) -> str:
        if "!" not in v or "@" not in v:
            raise ValueError("Token ID 格式应为 user@realm!tokenid,如 root@pam!dcn")
        return v

    @field_validator("host")
    @classmethod
    def _check_host(cls, v: str) -> str:
        return _validate_pve_host(v)


class PveConnectionUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    host: str | None = Field(None, min_length=1, max_length=255)
    port: int | None = Field(None, ge=1, le=65535)
    token_id: str | None = Field(None, min_length=1, max_length=255)
    token_secret: str | None = Field(None, min_length=1, max_length=255)
    verify_ssl: int | None = Field(None, ge=0, le=1)
    enabled: int | None = Field(None, ge=0, le=1)
    description: str | None = Field(None, max_length=500)

    @field_validator("host")
    @classmethod
    def _check_host(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_pve_host(v)

    @field_validator("token_id")
    @classmethod
    def _check_token_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if "!" not in v or "@" not in v:
            raise ValueError("Token ID 格式应为 user@realm!tokenid,如 root@pam!dcn")
        return v


class PveConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    host: str
    port: int
    token_id: str
    verify_ssl: int
    enabled: int
    description: str | None = None


class PveGuestBindingUpdate(BaseModel):
    ip_address: str | None = Field(default=None, max_length=255)
    os_system: str | None = Field(default=None, pattern="^(linux|windows|unknown)$")
    ssh_port: int = Field(default=22, ge=1, le=65535)
    winrm_port: int = Field(default=5985, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=255)
    password: str | None = Field(default=None, max_length=1000)
    ssh_key: str | None = Field(default=None, max_length=20000)
    enabled: bool = True


class PveGuestBindingResponse(BaseModel):
    id: int | None = None
    connection_id: int
    guest_type: str
    vmid: int
    ip_address: str | None = None
    os_system: str
    # True 表示 os_system=linux 是排除法兜底假设(未确证),前端应自动探测纠正
    os_assumed: bool = False
    # 持久化的精确 OS 名(QGA 不可用时的兜底显示值,见迁移 0038)
    os_name: str | None = None
    ssh_port: int
    winrm_port: int
    username: str | None = None
    has_password: bool = False
    has_ssh_key: bool = False
    enabled: bool
    configured: bool = False
    last_test_status: str | None = None
    last_test_error: str | None = None
    last_test_at: datetime | None = None
    created_at: datetime | None = None
    qga_enabled: bool = False
    qga_available: bool = False
    qga_ip_address: str | None = None
    qga_os_system: str | None = None
    qga_os_name: str | None = None
    qga_disks: list[dict] = Field(default_factory=list)
    winrm_available: bool = False
