from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.models import (
    ApiToken,
    Base,
    Skill,
    SkillKind,
    SkillStatus,
    SkillVersion,
    User,
    UserRole,
    VersionStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POSTGRES_URL = "postgresql+asyncpg://registry:secret@localhost:5432/alexandria"
HEAD = "002_package_registry"
INITIAL = "001_initial"


def alembic(database_url: str, *args: str) -> str:
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "JWT_SECRET": "migration-test-secret",
        "ALEXANDRIA_ENV": "development",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    return result.stdout


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def migrate(path: Path, *args: str) -> None:
    alembic(sqlite_url(path), *args)


def sync_engine(path: Path) -> sa.Engine:
    return sa.create_engine(f"sqlite:///{path}")


def flatten(differences: list[Any]) -> list[Any]:
    flat: list[Any] = []
    for difference in differences:
        if isinstance(difference, list):
            flat.extend(flatten(difference))
        else:
            flat.append(difference)
    return flat


def schema_differences(path: Path) -> list[Any]:
    engine = sync_engine(path)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            return flatten(compare_metadata(context, Base.metadata))
    finally:
        engine.dispose()


def schema_signature(path: Path) -> dict[str, dict[str, Any]]:
    engine = sync_engine(path)
    try:
        inspector = sa.inspect(engine)
        signature: dict[str, dict[str, Any]] = {}
        for table in sorted(inspector.get_table_names()):
            if table == "alembic_version":
                continue
            signature[table] = {
                "columns": sorted(
                    (column["name"], str(column["type"]), column["nullable"])
                    for column in inspector.get_columns(table)
                ),
                "indexes": sorted(
                    (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                    for index in inspector.get_indexes(table)
                ),
                "uniques": sorted(
                    tuple(unique["column_names"])
                    for unique in inspector.get_unique_constraints(table)
                ),
                "foreign_keys": sorted(
                    (
                        tuple(fk["constrained_columns"]),
                        fk["referred_table"],
                        tuple(fk["referred_columns"]),
                    )
                    for fk in inspector.get_foreign_keys(table)
                ),
                "primary_key": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
            }
        return signature
    finally:
        engine.dispose()


def table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        connection.close()
    return {name for (name,) in rows}


def current_revision(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        connection.close()


def execute_sql(path: Path, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
    connection = sqlite3.connect(path)
    try:
        for statement, parameters in statements:
            connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def fetch_all(path: Path, statement: str) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(statement).fetchall()
    finally:
        connection.close()


INSERT_USER = (
    "INSERT INTO users (id, username, email, password_hash, role, is_active) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
INSERT_SKILL = (
    "INSERT INTO skills (id, name, display_name, description, owner_id, status) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
INSERT_LEGACY_VERSION = (
    "INSERT INTO skill_versions (id, skill_id, version, capabilities, endpoints, metadata, "
    "tags, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
INSERT_TAG = "INSERT INTO skill_tags (skill_id, tag) VALUES (?, ?)"
INSERT_TOKEN = (
    "INSERT INTO api_tokens (id, user_id, name, prefix, token_hash, scope) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
INSERT_VERSION = (
    "INSERT INTO skill_versions (id, skill_id, version, status, blob_sha256, size_bytes, "
    "uncompressed_bytes, file_count, entrypoint, display_name, description, tags, metadata, "
    "manifest, search_text, uploaded_by) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
IMPORT_CHECK = (
    "import app.models; from app.db.base import Base; print(','.join(sorted(Base.metadata.tables)))"
)

CREATE_ALL_SCHEMA = """
CREATE TABLE users (
    id VARCHAR(36) NOT NULL,
    username VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(9) NOT NULL,
    is_active BOOLEAN NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_users_email ON users (email);
CREATE UNIQUE INDEX ix_users_username ON users (username);
CREATE TABLE skills (
    id VARCHAR(36) NOT NULL,
    name VARCHAR(255) NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    owner_id VARCHAR(36) NOT NULL,
    status VARCHAR(11) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(owner_id) REFERENCES users (id)
);
CREATE INDEX idx_skill_owner ON skills (owner_id);
CREATE INDEX idx_skill_status ON skills (status);
CREATE UNIQUE INDEX ix_skills_name ON skills (name);
CREATE TABLE skill_versions (
    id VARCHAR(36) NOT NULL,
    skill_id VARCHAR(36) NOT NULL,
    version VARCHAR(50) NOT NULL,
    capabilities TEXT,
    endpoints TEXT,
    metadata TEXT,
    tags TEXT,
    status VARCHAR(10) NOT NULL,
    published_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(skill_id) REFERENCES skills (id)
);
CREATE INDEX idx_skill_version_skill ON skill_versions (skill_id);
CREATE INDEX idx_skill_version_status ON skill_versions (status);
"""

LEGACY_ROWS: list[tuple[str, tuple[Any, ...]]] = [
    (INSERT_USER, ("u-admin", "root", "root@example.com", "hash", "ADMIN", 1)),
    (INSERT_USER, ("u-pub", "alice", "alice@example.com", "hash", "PUBLISHER", 1)),
    (INSERT_SKILL, ("s-draft", "draft-skill", "Draft Skill", "legacy draft", "u-pub", "DRAFT")),
    (
        INSERT_SKILL,
        ("s-pub", "published-skill", "Published Skill", "legacy published", "u-admin", "PUBLISHED"),
    ),
    (
        INSERT_SKILL,
        ("s-hid", "hidden-skill", "Hidden Skill", "legacy hidden", "u-pub", "UNPUBLISHED"),
    ),
    (INSERT_LEGACY_VERSION, ("v-1", "s-pub", "1.0.0", "[]", "[]", "{}", "[]", "PUBLISHED")),
]


@pytest.fixture(scope="module")
def head_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("head") / "registry.db"
    migrate(path, "upgrade", "head")
    return path


@pytest.fixture(scope="module")
def initial_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("initial") / "registry.db"
    migrate(path, "upgrade", INITIAL)
    return path


@pytest.fixture(scope="module")
def legacy_database(tmp_path_factory: pytest.TempPathFactory, initial_database: Path) -> Path:
    path = tmp_path_factory.mktemp("legacy") / "registry.db"
    shutil.copy(initial_database, path)
    execute_sql(path, LEGACY_ROWS)
    return path


@pytest.fixture(scope="module")
def migrated_legacy_database(
    tmp_path_factory: pytest.TempPathFactory, legacy_database: Path
) -> Path:
    path = tmp_path_factory.mktemp("migrated-legacy") / "registry.db"
    shutil.copy(legacy_database, path)
    migrate(path, "upgrade", "head")
    return path


@pytest.fixture(scope="module")
def downgraded_database(tmp_path_factory: pytest.TempPathFactory, head_database: Path) -> Path:
    path = tmp_path_factory.mktemp("downgraded") / "registry.db"
    shutil.copy(head_database, path)
    migrate(path, "downgrade", INITIAL)
    return path


def test_migration_history_is_linear_with_a_single_head():
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == [HEAD]
    assert [revision.revision for revision in script.walk_revisions()] == [HEAD, INITIAL]
    assert script.get_revision(HEAD).down_revision == INITIAL
    assert script.get_revision(INITIAL).down_revision is None


def test_upgrade_head_creates_exactly_the_documented_tables(head_database):
    assert table_names(head_database) == {
        "alembic_version",
        "users",
        "skills",
        "skill_tags",
        "skill_versions",
        "api_tokens",
    }
    assert current_revision(head_database) == HEAD


def test_migrated_schema_does_not_differ_from_the_models(head_database):
    assert schema_differences(head_database) == []


def test_migrated_primary_keys_match_the_models(head_database):
    engine = sync_engine(head_database)
    try:
        inspector = sa.inspect(engine)
        migrated = {
            table: tuple(inspector.get_pk_constraint(table)["constrained_columns"])
            for table in Base.metadata.tables
        }
    finally:
        engine.dispose()

    expected = {
        name: tuple(column.name for column in table.primary_key.columns)
        for name, table in Base.metadata.tables.items()
    }
    assert migrated == expected
    assert migrated["skill_tags"] == ("skill_id", "tag")


def test_server_defaults_of_the_new_columns_match_the_models(head_database):
    engine = sync_engine(head_database)
    try:
        inspector = sa.inspect(engine)
        reflected = {
            (table, column["name"]): column["default"]
            for table in ("skill_versions", "skill_tags", "api_tokens", "skills")
            for column in inspector.get_columns(table)
        }
    finally:
        engine.dispose()

    for table in ("skill_versions", "skill_tags", "api_tokens"):
        for column in Base.metadata.tables[table].columns:
            has_default = reflected[(table, column.name)] is not None
            assert has_default == (column.server_default is not None), (table, column.name)
    assert reflected[("skills", "kind")] == "'skill'"
    assert reflected[("skill_versions", "root_prefix")] == "''"


def test_schema_comparison_detects_a_stale_schema(initial_database):
    differences = schema_differences(initial_database)

    kinds = {difference[0] for difference in differences}
    added_tables = {
        difference[1].name for difference in differences if difference[0] == "add_table"
    }
    assert differences
    assert {"add_table", "add_column"} <= kinds
    assert {"skill_tags", "api_tokens"} <= added_tables


def test_migrated_skill_versions_has_no_legacy_columns_and_enforces_uniqueness(head_database):
    engine = sync_engine(head_database)
    try:
        inspector = sa.inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("skill_versions")}
        uniques = {
            tuple(unique["column_names"])
            for unique in inspector.get_unique_constraints("skill_versions")
        }
    finally:
        engine.dispose()

    assert not {"capabilities", "endpoints"} & columns
    assert {
        "blob_sha256",
        "size_bytes",
        "uncompressed_bytes",
        "file_count",
        "entrypoint",
        "root_prefix",
        "manifest",
        "search_text",
        "uploaded_by",
        "published_by",
        "deprecated_at",
    } <= columns
    assert ("skill_id", "version") in uniques


def test_downgrade_restores_the_initial_schema(initial_database, downgraded_database):
    assert current_revision(downgraded_database) == INITIAL
    assert table_names(downgraded_database) == {
        "alembic_version",
        "users",
        "skills",
        "skill_versions",
    }
    assert schema_signature(downgraded_database) == schema_signature(initial_database)


def test_upgrading_again_after_a_downgrade_yields_the_same_schema(
    head_database, downgraded_database, tmp_path
):
    path = tmp_path / "again.db"
    shutil.copy(downgraded_database, path)

    migrate(path, "upgrade", "head")

    assert current_revision(path) == HEAD
    assert schema_differences(path) == []
    assert schema_signature(path) == schema_signature(head_database)


def test_legacy_rows_start_with_uppercase_enum_names_and_a_legacy_version(legacy_database):
    assert fetch_all(legacy_database, "SELECT role FROM users ORDER BY username") == [
        ("PUBLISHER",),
        ("ADMIN",),
    ]
    assert fetch_all(legacy_database, "SELECT status FROM skills ORDER BY name") == [
        ("DRAFT",),
        ("UNPUBLISHED",),
        ("PUBLISHED",),
    ]
    assert fetch_all(legacy_database, "SELECT count(*) FROM skill_versions") == [(1,)]


def test_upgrade_lowercases_legacy_enum_values_and_discards_legacy_versions(
    migrated_legacy_database,
):
    path = migrated_legacy_database

    assert fetch_all(path, "SELECT username, role FROM users ORDER BY username") == [
        ("alice", "publisher"),
        ("root", "admin"),
    ]
    assert fetch_all(path, "SELECT name, status, kind FROM skills ORDER BY name") == [
        ("draft-skill", "draft", "skill"),
        ("hidden-skill", "unpublished", "skill"),
        ("published-skill", "published", "skill"),
    ]
    assert fetch_all(
        path, "SELECT name, display_name, description, owner_id FROM skills ORDER BY name"
    ) == [
        ("draft-skill", "Draft Skill", "legacy draft", "u-pub"),
        ("hidden-skill", "Hidden Skill", "legacy hidden", "u-pub"),
        ("published-skill", "Published Skill", "legacy published", "u-admin"),
    ]
    assert fetch_all(
        path, "SELECT count(*) FROM skills WHERE category IS NOT NULL OR latest_version IS NOT NULL"
    ) == [(0,)]
    assert fetch_all(path, "SELECT count(*) FROM skill_versions") == [(0,)]
    assert schema_differences(path) == []


def test_orm_reads_the_rows_that_the_migration_normalized(migrated_legacy_database):
    engine = sync_engine(migrated_legacy_database)
    try:
        with Session(engine) as session:
            roles = {user.username: user.role for user in session.scalars(sa.select(User))}
            skills = {skill.name: skill for skill in session.scalars(sa.select(Skill))}
            tags = skills["published-skill"].tags
    finally:
        engine.dispose()

    assert roles == {"root": UserRole.ADMIN, "alice": UserRole.PUBLISHER}
    assert skills["draft-skill"].status is SkillStatus.DRAFT
    assert skills["published-skill"].status is SkillStatus.PUBLISHED
    assert skills["hidden-skill"].status is SkillStatus.UNPUBLISHED
    assert {skill.kind for skill in skills.values()} == {SkillKind.SKILL}
    assert tags == []


def test_downgrade_with_data_keeps_users_and_skills_and_can_upgrade_again(
    migrated_legacy_database, tmp_path
):
    path = tmp_path / "roundtrip.db"
    shutil.copy(migrated_legacy_database, path)
    execute_sql(
        path,
        [
            (INSERT_TAG, ("s-pub", "docs")),
            (INSERT_TOKEN, ("t-1", "u-pub", "ci", "alx_abcdefgh", "f" * 64, "write")),
            (
                INSERT_VERSION,
                (
                    "v-2",
                    "s-pub",
                    "1.0.0",
                    "published",
                    "a" * 64,
                    10,
                    20,
                    1,
                    "SKILL.md",
                    "Published Skill",
                    "d",
                    "[]",
                    "{}",
                    "[]",
                    "",
                    "u-admin",
                ),
            ),
            ("UPDATE skills SET kind = 'grounding', category = 'ops' WHERE id = 's-pub'", ()),
        ],
    )

    migrate(path, "downgrade", INITIAL)

    assert table_names(path) == {"alembic_version", "users", "skills", "skill_versions"}
    assert fetch_all(path, "SELECT name, status FROM skills ORDER BY name") == [
        ("draft-skill", "draft"),
        ("hidden-skill", "unpublished"),
        ("published-skill", "published"),
    ]
    assert fetch_all(path, "SELECT username, role FROM users ORDER BY username") == [
        ("alice", "publisher"),
        ("root", "admin"),
    ]
    assert fetch_all(path, "SELECT count(*) FROM skill_versions") == [(0,)]

    migrate(path, "upgrade", "head")

    assert fetch_all(path, "SELECT name, kind, category FROM skills ORDER BY name") == [
        ("draft-skill", "skill", None),
        ("hidden-skill", "skill", None),
        ("published-skill", "skill", None),
    ]
    assert fetch_all(path, "SELECT count(*) FROM skill_tags") == [(0,)]
    assert schema_differences(path) == []


def test_upgrade_adopts_a_database_that_was_created_with_create_all(tmp_path):
    path = tmp_path / "adopted.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(CREATE_ALL_SCHEMA)
    finally:
        connection.close()
    execute_sql(
        path,
        [
            (INSERT_USER, ("u-1", "root", "root@example.com", "hash", "ADMIN", 1)),
            (INSERT_SKILL, ("s-1", "adopted", "Adopted", "kept", "u-1", "PUBLISHED")),
        ],
    )
    migrate(path, "stamp", INITIAL)

    migrate(path, "upgrade", "head")

    assert current_revision(path) == HEAD
    assert schema_differences(path) == []
    assert fetch_all(path, "SELECT role FROM users") == [("admin",)]
    assert fetch_all(path, "SELECT name, status, kind FROM skills") == [
        ("adopted", "published", "skill")
    ]


def test_postgresql_upgrade_converts_native_enums_before_dropping_their_types():
    sql = " ".join(alembic(POSTGRES_URL, "upgrade", f"{INITIAL}:{HEAD}", "--sql").split())

    role_cast = "ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(9) USING role::text;"
    status_cast = "ALTER TABLE skills ALTER COLUMN status TYPE VARCHAR(11) USING status::text;"
    assert role_cast in sql
    assert status_cast in sql
    for type_name in ("userrole", "skillstatus", "versionstatus"):
        assert f"DROP TYPE {type_name};" in sql
    assert sql.index(role_cast) < sql.index("DROP TYPE userrole;")
    assert sql.index(status_cast) < sql.index("DROP TYPE skillstatus;")
    assert sql.index("DROP TABLE skill_versions;") < sql.index("DROP TYPE versionstatus;")
    assert sql.index("DROP TYPE skillstatus;") < sql.index("UPDATE skills SET status")
    assert "UPDATE users SET role = lower(role);" in sql
    assert "UPDATE skills SET status = lower(status);" in sql
    assert "CREATE TYPE" not in sql
    assert "ENUM" not in sql
    assert "ALTER TABLE skills ADD COLUMN kind VARCHAR(9) DEFAULT 'skill' NOT NULL;" in sql
    assert "DROP INDEX IF EXISTS idx_skill_name;" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ix_skills_name ON skills (name);" in sql
    assert "CREATE UNIQUE INDEX ix_users_username ON users (username);" in sql
    assert "CREATE UNIQUE INDEX ix_api_tokens_token_hash ON api_tokens (token_hash);" in sql
    assert "FOREIGN KEY(skill_id) REFERENCES skills (id) ON DELETE CASCADE" in sql
    assert "CONSTRAINT uq_skill_versions_skill_id_version UNIQUE (skill_id, version)" in sql
    assert f"UPDATE alembic_version SET version_num='{HEAD}'" in sql


def test_postgresql_downgrade_recreates_native_enums():
    sql = " ".join(alembic(POSTGRES_URL, "downgrade", f"{HEAD}:{INITIAL}", "--sql").split())

    assert "CREATE TYPE userrole AS ENUM ('admin', 'publisher');" in sql
    assert "CREATE TYPE skillstatus AS ENUM ('draft', 'published', 'unpublished');" in sql
    assert "CREATE TYPE versionstatus AS ENUM ('draft', 'published', 'deprecated');" in sql
    assert "ALTER TABLE users ALTER COLUMN role TYPE userrole USING role::userrole;" in sql
    assert (
        "ALTER TABLE skills ALTER COLUMN status TYPE skillstatus USING status::skillstatus;" in sql
    )
    assert sql.index("CREATE TYPE userrole") < sql.index("ALTER COLUMN role TYPE userrole")
    assert sql.index("DROP TABLE skill_versions;") < sql.index("CREATE TABLE skill_versions")
    for table in ("api_tokens", "skill_tags"):
        assert f"DROP TABLE {table};" in sql
    assert f"UPDATE alembic_version SET version_num='{INITIAL}'" in sql


ENUM_COLUMNS = [
    (User, "role", UserRole),
    (Skill, "kind", SkillKind),
    (Skill, "status", SkillStatus),
    (SkillVersion, "status", VersionStatus),
    (ApiToken, "scope", None),
]


@pytest.mark.parametrize(("model", "column", "enum_class"), ENUM_COLUMNS)
def test_enum_columns_store_lowercase_values_without_native_types(model, column, enum_class):
    column_type = model.__table__.c[column].type

    assert column_type.native_enum is False
    stored = list(column_type.enums)
    assert stored == [value.lower() for value in stored]
    if enum_class is not None:
        assert stored == [member.value for member in enum_class]
    else:
        assert stored == ["read", "write"]


def test_model_tables_compile_for_postgresql_without_native_enum_types():
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "ENUM" not in ddl.upper()
        assert "CREATE TYPE" not in ddl.upper()
    users_ddl = str(CreateTable(User.__table__).compile(dialect=postgresql.dialect()))
    assert "role VARCHAR(9) NOT NULL" in users_ddl


def test_base_metadata_is_complete_from_a_bare_models_import():
    result = subprocess.run(
        [sys.executable, "-c", IMPORT_CHECK],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "api_tokens,skill_tags,skill_versions,skills,users"
