from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="凭据名称")
    username: str = Field(min_length=1, max_length=128, description="用户名")
    password: str | None = Field(None, max_length=512, description="密码")
    ssh_key: str | None = Field(None, max_length=16384, description="SSH私钥")

    @model_validator(mode="after")
    def check_has_secret(self):
        """至少提供密码或SSH密钥之一"""
        if not self.password and not self.ssh_key:
            raise ValueError("必须提供密码或SSH密钥中的至少一项")
        return self


class CredentialUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    username: str | None = Field(None, min_length=1, max_length=128)
    password: str | None = Field(None, max_length=512)
    ssh_key: str | None = Field(None, max_length=16384)


class CredentialResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    username: str
    has_password: bool = False
    has_ssh_key: bool = False
    created_at: datetime | None = None
    device_count: int = 0

    @field_serializer("created_at")
    @classmethod
    def _serialize_dt(cls, v: datetime | None) -> str | None:
        if v is None:
            return None
        s = v.isoformat()
        if v.tzinfo is None:
            return s + "Z"
        return s
