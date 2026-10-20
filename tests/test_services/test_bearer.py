from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.auth import bearer
from app.auth.bearer import (
    AUTHORIZATION_HEADER,
    DUPLICATE_HEADER_MESSAGE,
    MALFORMED_HEADER_MESSAGE,
    extract_bearer,
)
from app.services.errors import UnauthorizedError

MODULE = Path(bearer.__file__)
MALFORMED = [
    "",
    " ",
    "Bearer",
    "Bearer ",
    "Bearer     ",
    "Basic dXNlcjpwYXNz",
    "Token abc",
    "Bearerabc",
    "Bearer\tabc",
    "Bearer\xa0abc",
    " Bearer abc",
    "Bearer a b",
    "Bearer a  b",
    "Bearer a\tb",
    "Bearer a\nb",
    "Bearer abc def ghi",
    "Digest username=x",
    "alx_abc",
]


def test_no_header_means_an_anonymous_caller():
    assert extract_bearer([]) is None
    assert extract_bearer(()) is None


@pytest.mark.parametrize(
    ("value", "credential"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER abc", "abc"),
        ("BeArEr abc", "abc"),
        ("Bearer   abc", "abc"),
        ("Bearer abc   ", "abc"),
        ("Bearer   abc  ", "abc"),
        ("Bearer alx_" + "x" * 43, "alx_" + "x" * 43),
        ("Bearer header.payload.signature", "header.payload.signature"),
        ("Bearer a=b/c+d_e-f~g", "a=b/c+d_e-f~g"),
    ],
)
def test_one_bearer_header_yields_its_credential(value: str, credential: str):
    assert extract_bearer([value]) == credential


@pytest.mark.parametrize("value", MALFORMED)
def test_a_header_that_is_not_bearer_with_one_credential_is_unauthorized(value: str):
    with pytest.raises(UnauthorizedError) as raised:
        extract_bearer([value])

    assert raised.value.message == MALFORMED_HEADER_MESSAGE
    assert raised.value.code == "UNAUTHORIZED"


@pytest.mark.parametrize(
    "values",
    [
        ["Bearer a", "Bearer b"],
        ["Bearer a", "Bearer a"],
        ["Bearer a", "Basic b"],
        ["", ""],
        ["Bearer a", "Bearer b", "Bearer c"],
        ("Bearer a", "Bearer b"),
    ],
)
def test_more_than_one_header_is_refused_whatever_they_say(values):
    with pytest.raises(UnauthorizedError) as raised:
        extract_bearer(values)

    assert raised.value.message == DUPLICATE_HEADER_MESSAGE


def test_the_messages_are_the_documented_ones():
    assert DUPLICATE_HEADER_MESSAGE == "Exactly one Authorization header is allowed"
    assert MALFORMED_HEADER_MESSAGE == "The Authorization header must be 'Bearer <credential>'"
    assert AUTHORIZATION_HEADER == "authorization"


def test_the_parser_never_returns_an_empty_or_spaced_credential():
    for value in ("Bearer x", "Bearer  x ", "bearer\x20x"):
        credential = extract_bearer([value])
        assert credential == "x"
        assert credential == credential.strip() and len(credential.split()) == 1


def imports_of(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_the_parser_is_a_leaf_that_knows_no_web_framework_and_no_database():
    imported = imports_of(MODULE)

    assert imported <= {"__future__", "collections.abc", "app.services.errors"}
    assert not {name.split(".")[0] for name in imported} & {"fastapi", "starlette", "sqlalchemy"}
