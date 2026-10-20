from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import total_ordering

from app.services.errors import ValidationError

MAX_VERSION_LENGTH = 50

_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)"
_VERSION_PATTERN = re.compile(
    rf"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-({_IDENTIFIER}(?:\.{_IDENTIFIER})*))?"
)
_PREVIEW_LENGTH = 60


class InvalidVersionError(ValidationError):
    pass


def _preview(value: str) -> str:
    if len(value) <= _PREVIEW_LENGTH:
        return repr(value)
    return repr(value[:_PREVIEW_LENGTH]) + "..."


def _identifier_key(identifier: str) -> tuple[int, int, str]:
    if identifier.isdigit():
        return (0, int(identifier), "")
    return (1, 0, identifier)


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def sort_key(self) -> tuple:
        if not self.prerelease:
            return (self.major, self.minor, self.patch, 1, ())
        identifiers = tuple(_identifier_key(item) for item in self.prerelease)
        return (self.major, self.minor, self.patch, 0, identifiers)

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            return core + "-" + ".".join(self.prerelease)
        return core

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self.sort_key() < other.sort_key()


def parse(value: str) -> Version:
    if not isinstance(value, str):
        raise InvalidVersionError(f"Version must be a string, got {type(value).__name__}")
    if len(value) > MAX_VERSION_LENGTH:
        raise InvalidVersionError(f"Version is longer than {MAX_VERSION_LENGTH} characters")
    match = _VERSION_PATTERN.fullmatch(value)
    if match is None:
        if "+" in value:
            raise InvalidVersionError(
                f"Invalid version {_preview(value)}: build metadata ('+') is not supported"
            )
        raise InvalidVersionError(
            f"Invalid version {_preview(value)}: expected MAJOR.MINOR.PATCH with an optional "
            "-prerelease (semver 2.0.0)"
        )
    major, minor, patch, prerelease = match.groups()
    return Version(
        int(major),
        int(minor),
        int(patch),
        tuple(prerelease.split(".")) if prerelease else (),
    )


def is_valid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parse(value)
    except InvalidVersionError:
        return False
    return True


def compare(left: str, right: str) -> int:
    left_key = parse(left).sort_key()
    right_key = parse(right).sort_key()
    return (left_key > right_key) - (left_key < right_key)


def sort_key(value: str) -> tuple:
    return parse(value).sort_key()


def sort_versions(versions: Iterable[str], *, reverse: bool = False) -> list[str]:
    return sorted(versions, key=sort_key, reverse=reverse)


def pick_latest(versions: Iterable[str]) -> str | None:
    parsed = [(parse(value), value) for value in versions]
    stable = [pair for pair in parsed if not pair[0].is_prerelease]
    candidates = stable or parsed
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0].sort_key())[1]
