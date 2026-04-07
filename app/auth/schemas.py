from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.booking.schemas import validate_org_slug_value


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class ForgotPasswordBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    email: EmailStr


class ResetPasswordBody(BaseModel):
    token: str = Field(min_length=10, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)
    role: str = Field(default="user")  # admin | user
    email: str | None = Field(None, max_length=320)
    display_name: str = Field(default="", max_length=256)
    #: 新規ユーザーの操作中の組織。slug のみ＝既存組織へ紐付け。表示名＋slug＝組織が無ければ新規作成。
    org_name: str | None = Field(None, max_length=256)
    org_slug: str | None = Field(None, max_length=128)

    @field_validator("org_slug")
    @classmethod
    def org_slug_opt(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        return validate_org_slug_value(s)

    @field_validator("org_name")
    @classmethod
    def org_name_opt(cls, v: str | None) -> str | None:
        if v is None:
            return None
        t = v.strip()
        return t if t else None


class AdminUserOrgPatch(BaseModel):
    """ユーザーごとの操作中の組織。slug のみ＝既存へ紐付け。表示名＋slug で新規作成、または既存の表示名を更新。"""

    org_name: str | None = Field(None, max_length=256)
    org_slug: str | None = Field(None, max_length=128)

    @field_validator("org_slug")
    @classmethod
    def org_slug_opt(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        return validate_org_slug_value(s)

    @field_validator("org_name")
    @classmethod
    def org_name_opt(cls, v: str | None) -> str | None:
        if v is None:
            return None
        t = v.strip()
        return t if t else None


class AdminSetPasswordBody(BaseModel):
    new_password: str = Field(min_length=8, max_length=256)


class AdminDeleteUserBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)


class UserPreferencesPatch(BaseModel):
    default_org_slug: str | None = None
