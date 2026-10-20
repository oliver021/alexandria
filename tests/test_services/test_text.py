from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from app import text as text_module
from app.text import is_valid_text

SERVICES_WITHOUT_AUTH = (
    "access",
    "catalog",
    "mappers",
    "package_inspector",
    "package_service",
    "skill_service",
    "version_service",
)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_ordinary_unicode_and_the_empty_string_are_valid():
    assert is_valid_text("pässwörd✓ 日本語")
    assert is_valid_text("\U0001f600")
    assert is_valid_text("")


@pytest.mark.parametrize(
    "value",
    [
        "a\x00b",
        "\x00",
        "a\ud800b",
        "\udfff",
        chr(0xD83D) + chr(0xDE00),
    ],
    ids=["nul-inside", "only-nul", "high-surrogate", "low-surrogate", "split-surrogate-pair"],
)
def test_nul_and_unencodable_surrogates_are_invalid(value):
    assert not is_valid_text(value)


@pytest.mark.parametrize("value", [None, b"bytes", 12345, ["list"], object(), bytearray(b"x")])
def test_anything_that_is_not_a_string_is_invalid(value):
    assert not is_valid_text(value)


def test_the_module_is_a_leaf_that_imports_nothing_from_the_application():
    imported = imported_modules(Path(text_module.__file__))

    assert {name for name in imported if name.startswith("app")} == set()


@pytest.mark.parametrize("name", SERVICES_WITHOUT_AUTH)
def test_the_catalog_services_do_not_depend_on_the_auth_package(name):
    module = importlib.import_module(f"app.services.{name}")

    imported = imported_modules(Path(module.__file__))

    assert {
        item for item in imported if item == "app.auth" or item.startswith("app.auth.")
    } == set()


def test_the_services_and_the_auth_provider_share_the_same_validator():
    from app.auth import local
    from app.services import catalog, user_service

    assert catalog.is_valid_text is is_valid_text
    assert user_service.is_valid_text is is_valid_text
    assert local.is_valid_text is is_valid_text
