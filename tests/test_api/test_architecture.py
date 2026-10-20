from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_FILES = sorted((ROOT / "app" / "api").glob("*.py"))
TEST_FILES = sorted((ROOT / "tests" / "test_api").glob("*.py"))
ALLOWED_PERSISTENCE_IMPORTS = {
    "app.db.session",
    "app.db.session.get_db",
    "sqlalchemy.ext.asyncio",
    "sqlalchemy.ext.asyncio.AsyncSession",
}
SESSION_RECEIVERS = {"db", "session"}
SESSION_USERS = [("app/api/deps.py", "release_connection")]


def relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def is_persistence_module(module: str) -> bool:
    parts = module.split(".")
    if parts[0] == "sqlalchemy" or module == "app.repositories":
        return True
    if module.startswith(("app.repositories.", "app.db.")) or module == "app.db":
        return True
    if module == "app.models":
        return True
    return module.startswith("app.models.") and parts[2] != "schemas"


@pytest.mark.parametrize("path", ADAPTER_FILES + TEST_FILES, ids=relative)
def test_the_code_has_no_comments(path):
    source = path.read_text(encoding="utf-8")

    comments = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]

    assert comments == []


@pytest.mark.parametrize("path", ADAPTER_FILES + TEST_FILES, ids=relative)
def test_the_code_has_no_docstrings(path):
    documented = [
        getattr(node, "name", "<module>")
        for node in ast.walk(parse(path))
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and ast.get_docstring(node, clean=False) is not None
    ]

    assert documented == []


@pytest.mark.parametrize("path", ADAPTER_FILES, ids=relative)
def test_the_adapter_never_touches_repositories_or_orm_models(path):
    offending = [
        module
        for module in sorted(imported_modules(path))
        if is_persistence_module(module) and module not in ALLOWED_PERSISTENCE_IMPORTS
    ]

    assert offending == []


def test_the_dependency_wiring_is_the_only_place_that_reaches_for_the_session():
    users = {
        (relative(path), function.name)
        for path in ADAPTER_FILES
        for function in ast.walk(parse(path))
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in SESSION_RECEIVERS
    }

    assert sorted(users) == SESSION_USERS


@pytest.mark.parametrize(
    "module",
    [
        "app.repositories",
        "app.repositories.user_repo",
        "app.repositories.skill_repo",
        "app.models",
        "app.models.user",
        "app.models.skill",
        "app.models.token",
        "app.models.base",
        "sqlalchemy",
        "sqlalchemy.orm",
        "sqlalchemy.ext.asyncio.AsyncEngine",
        "app.db",
        "app.db.engine",
    ],
)
def test_the_persistence_filter_flags_what_an_adapter_must_not_import(module):
    assert is_persistence_module(module)
    assert module not in ALLOWED_PERSISTENCE_IMPORTS


@pytest.mark.parametrize(
    "module",
    [
        "app.models.schemas",
        "app.models.schemas.UserResponse",
        "app.services.auth_service",
        "fastapi",
    ],
)
def test_the_persistence_filter_lets_dtos_and_services_through(module):
    assert not is_persistence_module(module)
