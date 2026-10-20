from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OWNED_FILES = [
    ROOT / "app" / "main.py",
    ROOT / "app" / "logging.py",
    ROOT / "tests" / "conftest.py",
    ROOT / "tests" / "helpers" / "live.py",
    *sorted((ROOT / "tests" / "test_integration").glob("*.py")),
]
FORBIDDEN_IN_MAIN = ("app.repositories", "app.models.skill", "app.models.user", "app.models.token")


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


@pytest.mark.parametrize("path", OWNED_FILES, ids=relative)
def test_the_code_has_no_comments(path):
    source = path.read_text(encoding="utf-8")

    comments = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]

    assert comments == []


@pytest.mark.parametrize("path", OWNED_FILES, ids=relative)
def test_the_code_has_no_docstrings(path):
    documented = [
        getattr(node, "name", "<module>")
        for node in ast.walk(parse(path))
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and ast.get_docstring(node, clean=False) is not None
    ]

    assert documented == []


def test_the_application_module_wires_things_together_without_touching_persistence():
    imported = imported_modules(ROOT / "app" / "main.py")

    assert [name for name in imported if name.startswith(FORBIDDEN_IN_MAIN)] == []
    assert "app.mcp" in imported
    assert "app.mcp.mount_mcp" in imported
    assert "app.mcp.mcp_lifespan" in imported
