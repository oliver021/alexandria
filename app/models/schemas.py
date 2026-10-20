from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth.password import MAX_PASSWORD_LENGTH

T = TypeVar("T")

KindName = Literal["skill", "grounding"]
SkillStatusName = Literal["draft", "published", "unpublished"]
VersionStatusName = Literal["draft", "published", "deprecated"]
ScopeName = Literal["read", "write"]


class ApiModel(BaseModel):
    @field_validator("*", mode="after")
    @classmethod
    def _ensure_utc(cls, value: Any) -> Any:
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class PaginatedResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class SkillSummary(ApiModel):
    id: str
    name: str
    display_name: str
    description: str
    kind: KindName
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    status: SkillStatusName
    latest_version: str | None = None
    owner_id: str
    created_at: datetime
    updated_at: datetime


class VersionSummary(ApiModel):
    id: str
    skill_id: str
    version: str
    status: VersionStatusName
    sha256: str
    size_bytes: int
    uncompressed_bytes: int
    file_count: int
    entrypoint: str
    display_name: str
    description: str
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    uploaded_by: str
    published_by: str | None = None
    published_at: datetime | None = None
    deprecated_at: datetime | None = None
    created_at: datetime


class VersionDetail(VersionSummary):
    metadata: dict[str, Any] = Field(default_factory=dict)
    download_url: str


class SkillDetail(SkillSummary):
    versions: list[VersionSummary] = Field(default_factory=list)


class ManifestEntryResponse(BaseModel):
    path: str
    size: int
    sha256: str
    media_type: str
    text: bool


class FileListing(BaseModel):
    name: str
    version: str
    files: list[ManifestEntryResponse]


class FileContent(BaseModel):
    path: str
    size: int
    sha256: str
    media_type: str
    text: bool
    content: str | None = None
    truncated: bool = False
    encoding: str = "utf-8"


class EntrypointContent(FileContent):
    name: str
    version: str
    kind: KindName


class UploadResult(BaseModel):
    skill: SkillSummary
    version: VersionSummary


class DownloadInfo(BaseModel):
    name: str
    version: str
    sha256: str
    size_bytes: int
    url: str
    requires_auth: bool


class TaxonomyItem(BaseModel):
    name: str
    count: int


class TokenCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    scope: ScopeName = "read"
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class TokenResponse(ApiModel):
    id: str
    name: str
    prefix: str
    scope: ScopeName
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class TokenCreated(TokenResponse):
    token: str


class LimitsInfo(BaseModel):
    max_package_bytes: int
    max_uncompressed_bytes: int
    max_file_bytes: int
    max_files: int
    max_inline_read_bytes: int


class InfoResponse(BaseModel):
    name: str
    version: str
    public_read: bool
    public_url: str
    mcp_path: str
    limits: LimitsInfo


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    email: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8, max_length=MAX_PASSWORD_LENGTH)
    role: str = "publisher"


class UserResponse(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    email: str
    role: str
    is_active: bool
    created_at: datetime


class LoginRequest(BaseModel):
    username: str
    password: str = Field(..., max_length=MAX_PASSWORD_LENGTH)


class LoginResponse(BaseModel):
    token: str
    user: UserResponse


class ErrorDetail(BaseModel):
    code: str
    message: str
    reason: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
