from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.validators import validate_password_strength


class UserLogin(BaseModel):
    username: str = Field(min_length=1, max_length=64, description="用户名")
    password: str = Field(min_length=1, max_length=128, description="密码")


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64, description="用户名至少2个字符")
    password: str = Field(min_length=8, max_length=128, description="密码至少8个字符")
    display_name: str | None = Field(None, max_length=128, description="显示名称")
    role_id: int | None = Field(None, description="角色ID")
    is_active: Literal[0, 1] = Field(1, description="是否启用: 0-禁用 1-启用")

    @field_validator("password")
    @classmethod
    def validate_pwd(cls, v: str) -> str:
        return validate_password_strength(v)


class UserUpdate(BaseModel):
    username: str | None = Field(
        None, min_length=2, max_length=64, description="用户名至少2个字符"
    )
    password: str | None = Field(
        None, min_length=8, max_length=128, description="密码至少8个字符"
    )
    display_name: str | None = Field(None, max_length=128)
    role_id: int | None = None
    is_active: Literal[0, 1] | None = None

    @field_validator("password")
    @classmethod
    def validate_pwd(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_password_strength(v)
        return v


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""

    old_password: str = Field(min_length=1, description="当前密码")
    new_password: str = Field(
        min_length=8, max_length=128, description="新密码至少8个字符"
    )

    @field_validator("new_password")
    @classmethod
    def validate_new_pwd(cls, v: str) -> str:
        return validate_password_strength(v)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    display_name: str | None = None
    is_active: int = 1
    role_id: int | None = None
    role_name: str | None = None
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


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    must_change_password: bool = False
