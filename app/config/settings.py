from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values, load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    field_validator,
)

BASE_DIR = Path(__file__).resolve().parent.parent.parent

ENV_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("JWT_SECRET", ("auth", "jwt_secret")),
    ("DATABASE_URL", ("database", "url_override")),
    ("LOGGING_LEVEL", ("logging", "level")),
    ("PUBLIC_URL", ("server", "public_url")),
    ("STORAGE_PATH", ("storage", "path")),
    ("PUBLIC_READ", ("registry", "public_read")),
    ("AUTO_PUBLISH", ("registry", "auto_publish")),
    ("ALEXANDRIA_ENV", ("environment",)),
)
CORS_ORIGINS_ENV = "CORS_ORIGINS"


def _without_nulls(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _without_nulls(value) if isinstance(value, Mapping) else value
        for key, value in data.items()
        if value is not None
    }


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TypeError(f"{path}: the top level of the configuration must be a mapping")
    return _without_nulls(data)


def _read_dotenv(path: Path) -> dict[str, str]:
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def _assign(target: dict[str, Any], location: tuple[str, ...], value: Any) -> None:
    for key in location[:-1]:
        target = target.setdefault(key, {})
    target[location[-1]] = value


def _env_overlay(env: Mapping[str, str]) -> dict[str, Any]:
    overlay: dict[str, Any] = {}
    for name, location in ENV_FIELDS:
        value = (env.get(name) or "").strip()
        if value:
            _assign(overlay, location, value)
    raw_origins = (env.get(CORS_ORIGINS_ENV) or "").split(",")
    origins = [origin.strip() for origin in raw_origins if origin.strip()]
    if origins:
        _assign(overlay, ("cors", "allowed_origins"), origins)
    return overlay


def _merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerSettings(_StrictModel):
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = True
    public_url: str = "http://localhost:8000"

    @field_validator("public_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


class DatabaseSettings(_StrictModel):
    driver: Literal["sqlite", "postgresql"] = "sqlite"
    path: str = "data/alexandria.db"
    pool_size: PositiveInt = 5
    max_overflow: NonNegativeInt = 10
    pool_timeout: PositiveFloat = 30.0
    auto_migrate: bool = True
    url_override: str | None = Field(default=None, repr=False, exclude=True)

    @property
    def url(self) -> str:
        if self.url_override:
            return self.url_override
        if self.driver == "postgresql":
            return "postgresql+asyncpg://localhost:5432/alexandria"
        db_path = BASE_DIR / self.path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{db_path}"


class StorageSettings(_StrictModel):
    backend: Literal["local"] = "local"
    path: str = "data/blobs"

    @property
    def root(self) -> Path:
        path = Path(self.path).expanduser()
        return path if path.is_absolute() else BASE_DIR / path


class AuthSettings(_StrictModel):
    provider: Literal["local", "identityserver"] = "local"
    signup: Literal["disabled", "enabled"] = "disabled"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60
    jwt_secret: str = Field(default="", repr=False, exclude=True)


class LimitsSettings(_StrictModel):
    max_package_bytes: PositiveInt = 26214400
    max_uncompressed_bytes: PositiveInt = 104857600
    max_file_bytes: PositiveInt = 52428800
    max_files: PositiveInt = 2000
    max_inline_read_bytes: PositiveInt = 262144


class RegistrySettings(_StrictModel):
    public_read: bool = True
    auto_publish: bool = False
    limits: LimitsSettings = Field(default_factory=LimitsSettings)


class TokensSettings(_StrictModel):
    max_per_user: PositiveInt = 20


class CORSSettings(_StrictModel):
    allowed_origins: list[str] = ["http://localhost:8000", "http://localhost:5173"]
    allow_credentials: bool = True
    allow_methods: list[str] = ["*"]
    allow_headers: list[str] = ["*"]


class LoggingSettings(_StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("level", mode="before")
    @classmethod
    def _uppercase_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class Settings(_StrictModel):
    environment: Literal["development", "production"] = "production"
    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    registry: RegistrySettings = Field(default_factory=RegistrySettings)
    tokens: TokensSettings = Field(default_factory=TokensSettings)
    cors: CORSSettings = Field(default_factory=CORSSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @field_validator("environment", mode="before")
    @classmethod
    def _lowercase_environment(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        dotenv_path: str | Path | None = None,
    ) -> Settings:
        if env is None:
            load_dotenv(dotenv_path or BASE_DIR / ".env", override=False)
            source: Mapping[str, str] = os.environ
        elif dotenv_path is not None:
            source = {**_read_dotenv(Path(dotenv_path)), **env}
        else:
            source = env
        yaml_path = BASE_DIR / "config.yml" if config_path is None else Path(config_path)
        return cls.model_validate(_merge(_read_yaml(yaml_path), _env_overlay(source)))


settings = Settings.load()
