from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.validators import validate_outbound_url


class WebhookCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=8, max_length=2000)
    provider: str = "generic"
    secret: str | None = Field(default=None, max_length=512)
    events: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return validate_outbound_url(value)


class WebhookUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    url: str | None = Field(default=None, min_length=8, max_length=2000)
    provider: str | None = None
    secret: str | None = Field(default=None, max_length=512)
    events: list[str] | None = None
    headers: dict[str, str] | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return validate_outbound_url(value)


class WebhookResponse(BaseModel):
    id: int
    name: str
    url: str
    provider: str
    events: list[str]
    headers: dict[str, str]
    config: dict[str, Any]
    enabled: bool
    secret_set: bool
    secret_preview: str
    last_test_at: datetime | None = None
    last_test_status: str | None = None
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime


class WebhookTestResponse(BaseModel):
    ok: bool
    status_code: int | None = None
    message: str
    duration_ms: int
    response_preview: str | None = None


class FeishuDirectoryRequest(BaseModel):
    """读取飞书通讯录。

    凭据优先取已保存通道(webhook_id)，新建未保存时用表单里现填的
    app_id / app_secret;Secret 走 POST body，不出现在 URL 或访问日志里。
    """

    webhook_id: int | None = None
    base: str | None = Field(default=None, max_length=255)
    app_id: str | None = Field(default=None, max_length=128)
    app_secret: str | None = Field(default=None, max_length=512)
    department_id: str = Field(default="0", max_length=64)
    page_token: str = Field(default="", max_length=512)
    page_size: int = Field(default=50, ge=1, le=50)


class FeishuDepartmentItem(BaseModel):
    open_department_id: str
    name: str
    parent_department_id: str = ""


class FeishuUserItem(BaseModel):
    open_id: str
    user_id: str = ""
    union_id: str = ""
    name: str
    email: str = ""
    enterprise_email: str = ""
    avatar: str = ""
    department_ids: list[str] = Field(default_factory=list)


class FeishuDepartmentPage(BaseModel):
    items: list[FeishuDepartmentItem]
    page_token: str = ""
    has_more: bool = False
    # True = 应用只被授权了部分通讯录，这里的内容是按授权范围兜底出来的
    scope_limited: bool = False
    # True = 飞书没返回部门名(缺 contact:department.base:readonly)，界面要提示去补权限
    name_scope_missing: bool = False


class FeishuUserPage(BaseModel):
    items: list[FeishuUserItem]
    page_token: str = ""
    has_more: bool = False
    scope_limited: bool = False
    # True = 飞书没返回姓名(缺 contact:user.base:readonly)，列表里只能显示邮箱前缀或 ID
    name_scope_missing: bool = False
