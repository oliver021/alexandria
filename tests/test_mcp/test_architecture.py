from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_FILES = sorted((ROOT / "app" / "mcp").glob("*.py"))
TEST_FILES = sorted((ROOT / "tests" / "test_mcp").glob("*.py"))
FORBIDDEN_IMPORTS = ("app.repositories", "app.models.skill", "app.models.user", "app.models.token")


def relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


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
    imported = imported_modules(path)

    assert [name for name in imported if name.startswith(FORBIDDEN_IMPORTS)] == []


@pytest.mark.parametrize("path", ADAPTER_FILES, ids=relative)
def test_the_adapter_builds_no_queries_and_commits_nothing(path):
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(parse(path))
        if isinstance(node, ast.Call)
    }
    imported = imported_modules(path)

    assert not {"select", "insert", "update", "delete", "execute", "commit", "rollback"} & called
    assert not {name for name in imported if name.startswith("sqlalchemy")} - {
        "sqlalchemy.ext.asyncio",
        "sqlalchemy.ext.asyncio.AsyncSession",
    }


def test_the_obsolete_resources_module_is_gone():
    assert not (ROOT / "app" / "mcp" / "resources.py").exists()


def test_the_adapter_does_not_use_the_removed_fastmcp_api():
    for path in ADAPTER_FILES:
        assert "fastmcp" not in path.read_text(encoding="utf-8").lower()


async def test_no_resources_or_prompts_are_registered_on_the_server():
    from app.mcp.server import build_server

    server = build_server()

    assert await server.list_resources() == []
    assert await server.list_resource_templates() == []
    assert await server.list_prompts() == []
