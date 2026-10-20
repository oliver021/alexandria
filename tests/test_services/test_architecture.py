from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CODE_LAYERS = [
    "app/services",
    "app/auth",
    "app/repositories",
    "app/db",
    "app/models",
    "app/storage",
    "app/config",
    "app/text.py",
    "app/logging.py",
    "scripts/seed.py",
]
WEB_FRAMEWORKS = ("fastapi", "starlette")


def python_files(*layers: str) -> list[Path]:
    files: list[Path] = []
    for layer in layers:
        path = ROOT / layer
        files.extend([path] if path.is_file() else sorted(path.rglob("*.py")))
    return files


def relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


SERVICE_FILES = python_files("app/services")
LAYER_FILES = python_files(*CODE_LAYERS)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("path", LAYER_FILES, ids=relative)
def test_the_code_has_no_comments(path):
    source = path.read_text(encoding="utf-8")

    comments = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]

    assert comments == []


@pytest.mark.parametrize("path", LAYER_FILES, ids=relative)
def test_the_code_has_no_docstrings(path):
    documented = [
        getattr(node, "name", "<module>")
        for node in ast.walk(parse(path))
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and ast.get_docstring(node, clean=False) is not None
    ]

    assert documented == []


@pytest.mark.parametrize("path", SERVICE_FILES, ids=relative)
def test_the_services_do_not_import_a_web_framework(path):
    imported = {
        name.split(".")[0]
        for node in ast.walk(parse(path))
        for name in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
    }

    assert imported.isdisjoint(WEB_FRAMEWORKS)


def raised_names(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(parse(path)):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Attribute):
            names.append(target.attr)
    return names


@pytest.mark.parametrize("path", SERVICE_FILES, ids=relative)
def test_the_services_raise_domain_errors_never_bare_value_errors_or_http_errors(path):
    forbidden = {"ValueError", "HTTPException"}

    assert forbidden.isdisjoint(raised_names(path))


@pytest.mark.parametrize("path", python_files("app/repositories"), ids=relative)
def test_the_repositories_flush_and_refresh_but_never_commit(path):
    called = {
        node.func.attr
        for node in ast.walk(parse(path))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert not {"commit", "rollback"} & called


def test_every_database_access_in_the_services_is_asynchronous():
    for path in SERVICE_FILES:
        tree = parse(path)
        for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            awaited_db = [
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "db"
                and node.attr in {"execute", "commit", "flush", "refresh", "rollback"}
            ]
            assert awaited_db == [], (relative(path), function.name)


APP_FILES = sorted((ROOT / "app").rglob("*.py"))
BEARER_PARSER = ROOT / "app" / "auth" / "bearer.py"
PACKAGE_SERVICE = ROOT / "app" / "services" / "package_service.py"


def splits_on_a_space(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "partition"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == " "
    )


def floor_divides_by_three(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.FloorDiv)
        and isinstance(node.right, ast.Constant)
        and node.right.value == 3
    )


def files_where(predicate) -> list[str]:
    return [
        relative(path)
        for path in APP_FILES
        if any(predicate(node) for node in ast.walk(parse(path)))
    ]


def test_the_authorization_header_is_split_in_one_module_only():
    assert files_where(splits_on_a_space) == [relative(BEARER_PARSER)]


def test_the_base64_length_formula_is_written_in_one_module_only():
    assert files_where(floor_divides_by_three) == [relative(PACKAGE_SERVICE)]
