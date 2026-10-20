from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import get_origin
from unittest import mock

import pytest
from pydantic import BaseModel, ValidationError

import app.config.settings as settings_module
from app.config.settings import (
    ENV_FIELDS,
    AuthSettings,
    CORSSettings,
    DatabaseSettings,
    LoggingSettings,
    RegistrySettings,
    ServerSettings,
    Settings,
    StorageSettings,
    TokensSettings,
    settings,
)

DOCUMENTED_ENV = (
    "JWT_SECRET",
    "DATABASE_URL",
    "LOGGING_LEVEL",
    "PUBLIC_URL",
    "STORAGE_PATH",
    "PUBLIC_READ",
    "AUTO_PUBLISH",
    "CORS_ORIGINS",
    "ALEXANDRIA_ENV",
)

DEFAULT_DUMP = {
    "environment": "production",
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "reload": True,
        "public_url": "http://localhost:8000",
    },
    "database": {
        "driver": "sqlite",
        "path": "data/alexandria.db",
        "pool_size": 5,
        "max_overflow": 10,
        "pool_timeout": 30.0,
        "auto_migrate": True,
    },
    "storage": {"backend": "local", "path": "data/blobs"},
    "auth": {
        "provider": "local",
        "signup": "disabled",
        "jwt_algorithm": "HS256",
        "jwt_expiry_minutes": 60,
    },
    "registry": {
        "public_read": True,
        "auto_publish": False,
        "limits": {
            "max_package_bytes": 26214400,
            "max_uncompressed_bytes": 104857600,
            "max_file_bytes": 52428800,
            "max_files": 2000,
            "max_inline_read_bytes": 262144,
        },
    },
    "tokens": {"max_per_user": 20},
    "cors": {
        "allowed_origins": ["http://localhost:8000", "http://localhost:5173"],
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    },
    "logging": {"level": "INFO"},
}

SECTIONS = {
    "server": ServerSettings,
    "database": DatabaseSettings,
    "storage": StorageSettings,
    "auth": AuthSettings,
    "registry": RegistrySettings,
    "tokens": TokensSettings,
    "cors": CORSSettings,
    "logging": LoggingSettings,
}

ENV_CASES = [
    ("JWT_SECRET", "s3cret-value", lambda s: s.auth.jwt_secret, "s3cret-value"),
    (
        "DATABASE_URL",
        "postgresql+asyncpg://user:pw@db.internal:5432/alx",
        lambda s: s.database.url,
        "postgresql+asyncpg://user:pw@db.internal:5432/alx",
    ),
    ("LOGGING_LEVEL", "DEBUG", lambda s: s.logging.level, "DEBUG"),
    (
        "PUBLIC_URL",
        "https://registry.example.com",
        lambda s: s.server.public_url,
        "https://registry.example.com",
    ),
    (
        "STORAGE_PATH",
        "/var/lib/alexandria/blobs",
        lambda s: s.storage.path,
        "/var/lib/alexandria/blobs",
    ),
    ("PUBLIC_READ", "false", lambda s: s.registry.public_read, False),
    ("AUTO_PUBLISH", "true", lambda s: s.registry.auto_publish, True),
    (
        "CORS_ORIGINS",
        "https://a.example,https://b.example",
        lambda s: s.cors.allowed_origins,
        ["https://a.example", "https://b.example"],
    ),
    ("ALEXANDRIA_ENV", "development", lambda s: s.environment, "development"),
]


@pytest.fixture(autouse=True)
def base_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "project"
    monkeypatch.setattr(settings_module, "BASE_DIR", path)
    return path


@pytest.fixture
def absent(tmp_path: Path) -> Path:
    return tmp_path / "absent.yml"


@pytest.fixture
def write_config(tmp_path: Path):
    def write(content: str) -> Path:
        path = tmp_path / "config.yml"
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return path

    return write


@pytest.fixture
def write_dotenv(tmp_path: Path):
    def write(content: str) -> Path:
        path = tmp_path / "dotenv-file"
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return path

    return write


@pytest.fixture
def clean_environ():
    with mock.patch.dict(os.environ):
        for name in DOCUMENTED_ENV:
            os.environ.pop(name, None)
        yield os.environ


def defaults(absent: Path) -> Settings:
    return Settings.load(config_path=absent, env={})


def leaf_paths(model: type[BaseModel], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    paths = []
    for name, field in model.model_fields.items():
        annotation = field.annotation
        is_model = (
            isinstance(annotation, type)
            and get_origin(annotation) is None
            and issubclass(annotation, BaseModel)
        )
        if is_model:
            paths.extend(leaf_paths(annotation, (*prefix, name)))
        else:
            paths.append((*prefix, name))
    return paths


def hostile_environment() -> dict[str, str]:
    documented = set(DOCUMENTED_ENV)
    hostile = {}
    for path in leaf_paths(Settings):
        for candidate in {path[-1], "_".join(path), "__".join(path)}:
            for variant in (candidate.upper(), candidate.lower()):
                if variant not in documented:
                    hostile[variant] = "leaked-value"
    hostile["PATH"] = "leaked-value"
    return hostile


def test_module_exports_a_loaded_settings_singleton():
    assert isinstance(settings, Settings)


def test_defaults_without_yaml_or_environment(absent):
    loaded = defaults(absent)

    assert loaded.model_dump() == DEFAULT_DUMP
    assert loaded.auth.jwt_secret == ""
    assert loaded.database.url_override is None


def test_default_limits_match_the_package_contract(absent):
    limits = defaults(absent).registry.limits

    assert limits.max_package_bytes == 25 * 1024 * 1024
    assert limits.max_uncompressed_bytes == 100 * 1024 * 1024
    assert limits.max_file_bytes == 50 * 1024 * 1024
    assert limits.max_files == 2000
    assert limits.max_inline_read_bytes == 256 * 1024


def test_default_sections_do_not_share_mutable_state(absent):
    first = defaults(absent)
    first.cors.allowed_origins.append("https://mutated.example")
    first.registry.limits.max_files = 1

    second = defaults(absent)

    assert "https://mutated.example" not in second.cors.allowed_origins
    assert "https://mutated.example" not in Settings().cors.allowed_origins
    assert second.registry.limits.max_files == 2000


def test_yaml_values_override_every_section(write_config):
    path = write_config(
        """
        environment: development
        server:
          host: 127.0.0.1
          port: 9100
          reload: false
          public_url: "https://registry.example.com"
        database:
          driver: postgresql
          path: other/registry.db
          pool_size: 11
          max_overflow: 4
          pool_timeout: 2.5
          auto_migrate: false
        storage:
          backend: local
          path: /srv/blobs
        auth:
          provider: identityserver
          signup: enabled
          jwt_algorithm: HS512
          jwt_expiry_minutes: 5
        registry:
          public_read: false
          auto_publish: true
          limits:
            max_package_bytes: 1000
            max_uncompressed_bytes: 2000
            max_file_bytes: 1500
            max_files: 7
            max_inline_read_bytes: 99
        tokens:
          max_per_user: 3
        cors:
          allowed_origins: ["https://app.example"]
          allow_credentials: false
          allow_methods: ["GET", "POST"]
          allow_headers: ["Authorization"]
        logging:
          level: ERROR
        """
    )

    loaded = Settings.load(config_path=path, env={})

    assert loaded.model_dump() == {
        "environment": "development",
        "server": {
            "host": "127.0.0.1",
            "port": 9100,
            "reload": False,
            "public_url": "https://registry.example.com",
        },
        "database": {
            "driver": "postgresql",
            "path": "other/registry.db",
            "pool_size": 11,
            "max_overflow": 4,
            "pool_timeout": 2.5,
            "auto_migrate": False,
        },
        "storage": {"backend": "local", "path": "/srv/blobs"},
        "auth": {
            "provider": "identityserver",
            "signup": "enabled",
            "jwt_algorithm": "HS512",
            "jwt_expiry_minutes": 5,
        },
        "registry": {
            "public_read": False,
            "auto_publish": True,
            "limits": {
                "max_package_bytes": 1000,
                "max_uncompressed_bytes": 2000,
                "max_file_bytes": 1500,
                "max_files": 7,
                "max_inline_read_bytes": 99,
            },
        },
        "tokens": {"max_per_user": 3},
        "cors": {
            "allowed_origins": ["https://app.example"],
            "allow_credentials": False,
            "allow_methods": ["GET", "POST"],
            "allow_headers": ["Authorization"],
        },
        "logging": {"level": "ERROR"},
    }


def test_partial_yaml_keeps_the_remaining_defaults(write_config):
    path = write_config(
        """
        registry:
          limits:
            max_files: 10
        """
    )

    loaded = Settings.load(config_path=path, env={})

    expected = {**DEFAULT_DUMP}
    expected["registry"] = {
        **DEFAULT_DUMP["registry"],
        "limits": {**DEFAULT_DUMP["registry"]["limits"], "max_files": 10},
    }
    assert loaded.model_dump() == expected


@pytest.mark.parametrize("content", ["", "\n\n", "---\n"])
def test_empty_yaml_uses_the_defaults(write_config, content):
    loaded = Settings.load(config_path=write_config(content), env={})

    assert loaded.model_dump() == DEFAULT_DUMP


def test_empty_yaml_sections_mean_defaults(write_config):
    path = write_config(
        """
        server:
        registry:
          limits:
        tokens:
        """
    )

    assert Settings.load(config_path=path, env={}).model_dump() == DEFAULT_DUMP


def test_missing_config_file_uses_the_defaults(absent):
    assert not absent.exists()

    assert Settings.load(config_path=absent, env={}) == Settings()


def test_config_path_accepts_strings(write_config):
    path = write_config("server:\n  port: 9300\n")

    assert Settings.load(config_path=str(path), env={}).server.port == 9300


def test_yaml_with_a_non_mapping_top_level_is_rejected(write_config):
    path = write_config("- one\n- two\n")

    with pytest.raises(TypeError, match="mapping"):
        Settings.load(config_path=path, env={})


@pytest.mark.parametrize(
    ("content", "offending_key"),
    [
        ("unexpected_section:\n  a: 1\n", "unexpected_section"),
        ("server:\n  hots: 0.0.0.0\n", "hots"),
        ("registry:\n  default_skill_status: draft\n", "default_skill_status"),
        ("registry:\n  limits:\n    max_file: 5\n", "max_file"),
        ("cors:\n  allowed_origin: []\n", "allowed_origin"),
    ],
    ids=["root", "typo-in-server", "removed-field", "typo-in-limits", "typo-in-cors"],
)
def test_unknown_keys_are_rejected_instead_of_silently_ignored(
    write_config, content, offending_key
):
    path = write_config(content)

    with pytest.raises(ValidationError, match=offending_key):
        Settings.load(config_path=path, env={})


@pytest.mark.parametrize(
    "content",
    [
        "server:\n  port: not-a-number\n",
        "server:\n  reload: maybe\n",
        "database:\n  driver: mysql\n",
        "database:\n  pool_size: 0\n",
        "database:\n  pool_size: -3\n",
        "database:\n  max_overflow: -1\n",
        "database:\n  pool_timeout: 0\n",
        "database:\n  pool_timeout: -5\n",
        "database:\n  pool_timeout: soon\n",
        "storage:\n  backend: s3\n",
        "auth:\n  provider: ldap\n",
        "registry:\n  limits:\n    max_files: 0\n",
        "registry:\n  limits:\n    max_package_bytes: -1\n",
        "registry:\n  limits:\n    max_inline_read_bytes: 0\n",
        "tokens:\n  max_per_user: 0\n",
        "logging:\n  level: LOUD\n",
        "environment: staging\n",
    ],
)
def test_invalid_values_are_rejected(write_config, content):
    path = write_config(content)

    with pytest.raises(ValidationError):
        Settings.load(config_path=path, env={})


def test_the_pool_settings_accept_no_overflow_and_fractional_timeouts(write_config):
    path = write_config("database:\n  pool_size: 1\n  max_overflow: 0\n  pool_timeout: 0.25\n")

    loaded = Settings.load(config_path=path, env={})

    assert (loaded.database.pool_size, loaded.database.max_overflow) == (1, 0)
    assert loaded.database.pool_timeout == 0.25


@pytest.mark.parametrize(
    ("name", "raw", "read", "expected"), ENV_CASES, ids=[case[0] for case in ENV_CASES]
)
def test_each_documented_environment_variable_is_applied(absent, name, raw, read, expected):
    baseline = read(defaults(absent))

    loaded = Settings.load(config_path=absent, env={name: raw})

    assert read(loaded) == expected
    assert baseline != expected


def test_documented_variables_match_the_loader_table():
    from_table = {name for name, _ in ENV_FIELDS} | {"CORS_ORIGINS"}

    assert from_table == set(DOCUMENTED_ENV)


def test_environment_variables_override_yaml_values(write_config):
    path = write_config(
        """
        environment: development
        server:
          public_url: "http://yaml.example"
        storage:
          path: yaml/blobs
        auth:
          jwt_secret: yaml-secret
        registry:
          public_read: true
          auto_publish: false
        logging:
          level: WARNING
        cors:
          allowed_origins: ["http://yaml.example"]
        database:
          path: yaml.db
        """
    )
    env = {
        "ALEXANDRIA_ENV": "production",
        "PUBLIC_URL": "https://env.example",
        "STORAGE_PATH": "/env/blobs",
        "JWT_SECRET": "env-secret",
        "PUBLIC_READ": "false",
        "AUTO_PUBLISH": "true",
        "LOGGING_LEVEL": "CRITICAL",
        "CORS_ORIGINS": "https://env.example",
        "DATABASE_URL": "postgresql+asyncpg://env/db",
    }

    loaded = Settings.load(config_path=path, env=env)

    assert loaded.environment == "production"
    assert loaded.server.public_url == "https://env.example"
    assert loaded.storage.path == "/env/blobs"
    assert loaded.auth.jwt_secret == "env-secret"
    assert loaded.registry.public_read is False
    assert loaded.registry.auto_publish is True
    assert loaded.logging.level == "CRITICAL"
    assert loaded.cors.allowed_origins == ["https://env.example"]
    assert loaded.database.url == "postgresql+asyncpg://env/db"
    assert loaded.database.path == "yaml.db"


def test_yaml_values_survive_when_the_environment_does_not_set_them(write_config):
    path = write_config(
        """
        server:
          public_url: "http://yaml.example"
        logging:
          level: WARNING
        registry:
          public_read: false
        """
    )

    loaded = Settings.load(config_path=path, env={"AUTO_PUBLISH": "true"})

    assert loaded.server.public_url == "http://yaml.example"
    assert loaded.logging.level == "WARNING"
    assert loaded.registry.public_read is False
    assert loaded.registry.auto_publish is True


def test_environment_overlay_works_when_yaml_leaves_a_section_empty(write_config):
    path = write_config("registry:\nlogging:\n")

    loaded = Settings.load(config_path=path, env={"PUBLIC_READ": "false", "LOGGING_LEVEL": "ERROR"})

    assert loaded.registry.public_read is False
    assert loaded.logging.level == "ERROR"


def test_logging_level_environment_variable_takes_effect(absent):
    assert defaults(absent).logging.level == "INFO"

    loaded = Settings.load(config_path=absent, env={"LOGGING_LEVEL": "ERROR"})

    assert loaded.logging.level == "ERROR"


@pytest.mark.parametrize(
    ("raw", "expected"), [("debug", "DEBUG"), (" Warning ", "WARNING"), ("error", "ERROR")]
)
def test_logging_level_is_case_insensitive_in_the_environment(absent, raw, expected):
    assert Settings.load(config_path=absent, env={"LOGGING_LEVEL": raw}).logging.level == expected


def test_logging_level_is_case_insensitive_in_yaml(write_config):
    path = write_config("logging:\n  level: debug\n")

    assert Settings.load(config_path=path, env={}).logging.level == "DEBUG"


@pytest.mark.parametrize("raw", ["VERBOSE", "TRACE", "WARN"])
def test_invalid_logging_level_in_the_environment_fails_loudly(absent, raw):
    with pytest.raises(ValidationError, match="logging.level"):
        Settings.load(config_path=absent, env={"LOGGING_LEVEL": raw})


@pytest.mark.parametrize(
    ("raw", "expected"), [("DEVELOPMENT", "development"), (" Production ", "production")]
)
def test_alexandria_env_is_case_insensitive(absent, raw, expected):
    assert Settings.load(config_path=absent, env={"ALEXANDRIA_ENV": raw}).environment == expected


@pytest.mark.parametrize("raw", ["staging", "dev", "prod", "test"])
def test_alexandria_env_only_accepts_development_or_production(absent, raw):
    with pytest.raises(ValidationError, match="environment"):
        Settings.load(config_path=absent, env={"ALEXANDRIA_ENV": raw})


@pytest.mark.parametrize("variable", ["PUBLIC_READ", "AUTO_PUBLISH"])
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        (" true ", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("off", False),
        (" false ", False),
    ],
)
def test_boolean_variables_accept_the_usual_spellings(absent, variable, raw, expected):
    loaded = Settings.load(config_path=absent, env={variable: raw})

    assert getattr(loaded.registry, variable.lower()) is expected


@pytest.mark.parametrize("variable", ["PUBLIC_READ", "AUTO_PUBLISH"])
@pytest.mark.parametrize("raw", ["maybe", "2", "enabled", "nope"])
def test_unparseable_booleans_fail_instead_of_becoming_false(absent, variable, raw):
    with pytest.raises(ValidationError, match=variable.lower()):
        Settings.load(config_path=absent, env={variable: raw})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://a.example", ["https://a.example"]),
        ("https://a.example,https://b.example", ["https://a.example", "https://b.example"]),
        (" https://a.example , https://b.example ", ["https://a.example", "https://b.example"]),
        ("https://a.example,,https://b.example,", ["https://a.example", "https://b.example"]),
        ("*", ["*"]),
    ],
    ids=["single", "pair", "whitespace", "empty-items", "wildcard"],
)
def test_cors_origins_is_a_comma_separated_list(absent, raw, expected):
    loaded = Settings.load(config_path=absent, env={"CORS_ORIGINS": raw})

    assert loaded.cors.allowed_origins == expected


def test_cors_origins_replaces_the_yaml_list_instead_of_extending_it(write_config):
    path = write_config('cors:\n  allowed_origins: ["http://yaml-one", "http://yaml-two"]\n')

    loaded = Settings.load(config_path=path, env={"CORS_ORIGINS": "https://only.example"})

    assert loaded.cors.allowed_origins == ["https://only.example"]


@pytest.mark.parametrize("raw", ["", "   ", ",", " , ,, "])
def test_blank_cors_origins_keep_the_configured_list(write_config, raw):
    path = write_config('cors:\n  allowed_origins: ["http://yaml-one"]\n')

    loaded = Settings.load(config_path=path, env={"CORS_ORIGINS": raw})

    assert loaded.cors.allowed_origins == ["http://yaml-one"]


def test_blank_environment_values_are_treated_as_unset(absent):
    blank = {name: "   " for name in DOCUMENTED_ENV}
    empty = {name: "" for name in DOCUMENTED_ENV}

    assert Settings.load(config_path=absent, env=blank) == defaults(absent)
    assert Settings.load(config_path=absent, env=empty) == defaults(absent)


def test_values_are_stripped_before_use(absent):
    loaded = Settings.load(
        config_path=absent,
        env={"PUBLIC_URL": "  https://registry.example.com  ", "STORAGE_PATH": " /srv/blobs "},
    )

    assert loaded.server.public_url == "https://registry.example.com"
    assert loaded.storage.path == "/srv/blobs"


@pytest.mark.parametrize(
    "raw", ["http://localhost:8000/", "http://localhost:8000///", "http://localhost:8000"]
)
def test_public_url_never_keeps_a_trailing_slash(absent, raw):
    loaded = Settings.load(config_path=absent, env={"PUBLIC_URL": raw})

    assert loaded.server.public_url == "http://localhost:8000"


def test_public_url_trailing_slash_is_also_removed_from_yaml(write_config):
    path = write_config('server:\n  public_url: "https://registry.example.com/base/"\n')

    loaded = Settings.load(config_path=path, env={})

    assert loaded.server.public_url == "https://registry.example.com/base"


def test_undocumented_names_in_an_injected_environment_are_ignored(absent):
    hostile = {
        "PATH": "/usr/bin:/bin",
        "HOST": "evil.example",
        "PORT": "1",
        "LEVEL": "DEBUG",
        "RELOAD": "false",
        "DRIVER": "postgresql",
        "POOL_SIZE": "99",
        "HOME": "/root",
        "ENVIRONMENT": "development",
        "URL": "https://evil.example",
        "SIGNUP": "enabled",
        "PROVIDER": "identityserver",
    }

    loaded = Settings.load(config_path=absent, env=hostile)

    assert loaded.model_dump() == DEFAULT_DUMP


def test_hostile_environment_covers_the_names_that_used_to_leak():
    hostile = hostile_environment()

    assert {"PATH", "HOST", "PORT", "LEVEL", "RELOAD", "path", "level", "SERVER__HOST"} <= set(
        hostile
    )
    assert not set(DOCUMENTED_ENV) & set(hostile)


def test_process_environment_names_do_not_leak_into_any_field(absent, write_dotenv, clean_environ):
    clean_environ.update(hostile_environment())
    assert clean_environ["PATH"] == "leaked-value"

    loaded = Settings.load(config_path=absent, dotenv_path=write_dotenv(""))

    assert loaded.model_dump() == DEFAULT_DUMP
    assert loaded.database.path == "data/alexandria.db"
    assert loaded.server.port == 8000
    assert loaded.logging.level == "INFO"


def test_real_system_path_variable_is_not_read_as_the_database_path(
    absent, write_dotenv, clean_environ
):
    assert os.environ["PATH"] != "data/alexandria.db"

    loaded = Settings.load(config_path=absent, dotenv_path=write_dotenv(""))

    assert loaded.database.path == "data/alexandria.db"
    assert loaded.storage.path == "data/blobs"


@pytest.mark.parametrize("section", list(SECTIONS))
def test_sections_built_directly_ignore_the_process_environment(section, clean_environ):
    clean_environ.update(hostile_environment())

    assert SECTIONS[section]().model_dump() == DEFAULT_DUMP[section]


def test_root_settings_built_directly_ignore_the_process_environment(clean_environ):
    clean_environ.update(hostile_environment())

    assert Settings().model_dump() == DEFAULT_DUMP


@pytest.mark.parametrize("model", [Settings, *SECTIONS.values()])
def test_models_are_plain_pydantic_models_not_settings_classes(model):
    assert issubclass(model, BaseModel)
    assert all(cls.__name__ != "BaseSettings" for cls in model.__mro__)


def test_real_process_environment_is_read_when_no_environment_is_injected(
    absent, write_dotenv, clean_environ
):
    clean_environ["LOGGING_LEVEL"] = "ERROR"
    clean_environ["PUBLIC_READ"] = "false"

    loaded = Settings.load(config_path=absent, dotenv_path=write_dotenv(""))

    assert loaded.logging.level == "ERROR"
    assert loaded.registry.public_read is False


def test_dotenv_file_supplies_values_missing_from_the_environment(
    absent, write_dotenv, clean_environ
):
    dotenv = write_dotenv(
        """
        JWT_SECRET=from-file
        LOGGING_LEVEL=WARNING
        PUBLIC_URL=https://file.example
        """
    )

    loaded = Settings.load(config_path=absent, dotenv_path=dotenv)

    assert loaded.auth.jwt_secret == "from-file"
    assert loaded.logging.level == "WARNING"
    assert loaded.server.public_url == "https://file.example"
    assert clean_environ["JWT_SECRET"] == "from-file"


def test_dotenv_never_overrides_the_real_environment(absent, write_dotenv, clean_environ):
    clean_environ["JWT_SECRET"] = "from-real-environment"
    clean_environ["PUBLIC_READ"] = "false"
    dotenv = write_dotenv(
        """
        JWT_SECRET=from-file
        PUBLIC_READ=true
        AUTO_PUBLISH=true
        """
    )

    loaded = Settings.load(config_path=absent, dotenv_path=dotenv)

    assert loaded.auth.jwt_secret == "from-real-environment"
    assert loaded.registry.public_read is False
    assert loaded.registry.auto_publish is True
    assert clean_environ["JWT_SECRET"] == "from-real-environment"
    assert clean_environ["PUBLIC_READ"] == "false"


def test_blank_dotenv_values_do_not_replace_yaml_values(write_config, write_dotenv, clean_environ):
    path = write_config('server:\n  public_url: "https://yaml.example"\n')
    dotenv = write_dotenv("PUBLIC_URL=\nJWT_SECRET=\n")

    loaded = Settings.load(config_path=path, dotenv_path=dotenv)

    assert loaded.server.public_url == "https://yaml.example"
    assert loaded.auth.jwt_secret == ""


def test_missing_dotenv_file_is_not_an_error(absent, tmp_path, clean_environ):
    loaded = Settings.load(config_path=absent, dotenv_path=tmp_path / "no-such.env")

    assert loaded.model_dump() == DEFAULT_DUMP


def test_missing_dotenv_file_is_not_an_error_with_an_injected_environment(absent, tmp_path):
    loaded = Settings.load(
        config_path=absent, env={"LOGGING_LEVEL": "ERROR"}, dotenv_path=tmp_path / "no-such.env"
    )

    assert loaded.logging.level == "ERROR"


def test_dotenv_with_injected_environment_fills_gaps_without_touching_the_process(
    absent, write_dotenv
):
    dotenv = write_dotenv(
        """
        JWT_SECRET=from-file
        PUBLIC_URL=https://file.example
        LOGGING_LEVEL=DEBUG
        """
    )
    snapshot = dict(os.environ)

    loaded = Settings.load(config_path=absent, env={"LOGGING_LEVEL": "ERROR"}, dotenv_path=dotenv)

    assert loaded.auth.jwt_secret == "from-file"
    assert loaded.server.public_url == "https://file.example"
    assert loaded.logging.level == "ERROR"
    assert dict(os.environ) == snapshot


def test_injected_environment_ignores_the_default_dotenv(absent, base_dir):
    base_dir.mkdir()
    (base_dir / ".env").write_text("JWT_SECRET=from-base-dir-dotenv\n", encoding="utf-8")
    snapshot = dict(os.environ)

    loaded = Settings.load(config_path=absent, env={})

    assert loaded.auth.jwt_secret == ""
    assert dict(os.environ) == snapshot


def test_default_config_and_dotenv_are_resolved_from_the_base_directory(base_dir, clean_environ):
    base_dir.mkdir()
    (base_dir / "config.yml").write_text("server:\n  port: 7001\n", encoding="utf-8")
    (base_dir / ".env").write_text(
        "PUBLIC_URL=https://from-base-dir.example\nLOGGING_LEVEL=ERROR\n", encoding="utf-8"
    )

    loaded = Settings.load()

    assert loaded.server.port == 7001
    assert loaded.server.public_url == "https://from-base-dir.example"
    assert loaded.logging.level == "ERROR"


def test_defaults_apply_when_the_base_directory_has_no_config_files(base_dir, clean_environ):
    base_dir.mkdir()

    assert Settings.load().model_dump() == DEFAULT_DUMP


def test_database_url_prefers_the_environment_over_driver_and_path(write_config):
    path = write_config("database:\n  driver: postgresql\n  path: ignored.db\n")

    loaded = Settings.load(config_path=path, env={"DATABASE_URL": "sqlite+aiosqlite:///x/y.db"})

    assert loaded.database.url == "sqlite+aiosqlite:///x/y.db"


def test_blank_database_url_falls_back_to_the_configured_database(tmp_path):
    database = tmp_path / "state" / "alx.db"
    config = tmp_path / "config.yml"
    config.write_text(f'database:\n  path: "{database}"\n', encoding="utf-8")

    loaded = Settings.load(config_path=config, env={"DATABASE_URL": "  "})

    assert loaded.database.url == f"sqlite+aiosqlite:///{database}"


def test_sqlite_url_uses_an_absolute_path_and_creates_the_parent_directory(tmp_path):
    database = tmp_path / "state" / "nested" / "alx.db"
    config = tmp_path / "config.yml"
    config.write_text(f'database:\n  path: "{database}"\n', encoding="utf-8")
    loaded = Settings.load(config_path=config, env={})
    assert not database.parent.exists()

    url = loaded.database.url

    assert url == f"sqlite+aiosqlite:///{database}"
    assert database.parent.is_dir()


def test_relative_sqlite_path_resolves_from_the_base_directory(absent, base_dir):
    url = defaults(absent).database.url

    assert url == f"sqlite+aiosqlite:///{base_dir / 'data' / 'alexandria.db'}"
    assert (base_dir / "data").is_dir()


def test_postgresql_driver_without_url_keeps_the_localhost_default(write_config):
    path = write_config("database:\n  driver: postgresql\n")

    loaded = Settings.load(config_path=path, env={})

    assert loaded.database.url == "postgresql+asyncpg://localhost:5432/alexandria"


def test_secrets_are_hidden_from_repr_and_serialization(absent):
    loaded = Settings.load(
        config_path=absent,
        env={
            "JWT_SECRET": "super-secret-signing-key",
            "DATABASE_URL": "postgresql+asyncpg://admin:hunter2@db.internal/alx",
        },
    )

    exposed = " ".join(
        [
            repr(loaded),
            str(loaded),
            repr(loaded.auth),
            repr(loaded.database),
            str(loaded.model_dump()),
            loaded.model_dump_json(),
        ]
    )

    assert "super-secret-signing-key" not in exposed
    assert "hunter2" not in exposed
    assert loaded.auth.jwt_secret == "super-secret-signing-key"
    assert "hunter2" in loaded.database.url


def test_storage_root_keeps_absolute_paths_and_resolves_relative_ones_from_base_dir(base_dir):
    assert StorageSettings(path="/srv/blobs").root == Path("/srv/blobs")
    assert StorageSettings(path="data/blobs").root == base_dir / "data" / "blobs"


def test_storage_root_expands_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert StorageSettings(path="~/blobs").root == tmp_path / "blobs"


def test_storage_root_does_not_create_directories(tmp_path):
    target = tmp_path / "never" / "created"

    assert StorageSettings(path=str(target)).root == target
    assert not target.exists()
    assert not target.parent.exists()


def test_storage_path_variable_feeds_the_root(absent, tmp_path):
    loaded = Settings.load(config_path=absent, env={"STORAGE_PATH": str(tmp_path / "from-env")})

    assert loaded.storage.root == tmp_path / "from-env"
