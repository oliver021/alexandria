from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.errors import DomainError, NotFoundError, ValidationError
from app.services.package_service import PackageService
from app.services.principal import Principal
from app.services.skill_service import SkillService
from app.services.version_service import VersionService
from tests.helpers.factories import Actors, upload_package

UNENCODABLE = [
    pytest.param("\ud800", id="high-surrogate"),
    pytest.param("\udfff", id="low-surrogate"),
    pytest.param("sample-\udc00skill", id="surrogate-inside-a-name"),
    pytest.param(chr(0xD83D) + chr(0xDE00), id="surrogate-pair-as-separate-code-points"),
]


@dataclass(frozen=True)
class World:
    skills: SkillService
    versions: VersionService
    packages: PackageService
    owner: Principal


@pytest_asyncio.fixture
async def world(db_session, blob_store, actors: Actors) -> World:
    packages = PackageService(db_session, blob_store, public_read=True, auto_publish=False)
    await upload_package(packages, actors.owner, "sample-skill", "1.0.0", publish=True)
    await upload_package(packages, actors.owner, "sample-skill", "1.1.0", publish=True)
    return World(
        skills=SkillService(db_session, public_read=True),
        versions=VersionService(db_session, public_read=True),
        packages=packages,
        owner=actors.owner,
    )


@contextmanager
def record_parameters(engine: AsyncEngine) -> Iterator[list[Any]]:
    seen: list[Any] = []

    def record(connection, cursor, statement, parameters, context, executemany):
        seen.append(parameters)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def has_surrogate(value: Any) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    if isinstance(value, dict):
        return any(has_surrogate(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(has_surrogate(item) for item in value)
    return False


Call = Callable[[World, str], Any]

ENTRY_POINTS: dict[str, tuple[Call, type[DomainError]]] = {
    "skills.search.q": (lambda w, bad: w.skills.search(None, q=bad), ValidationError),
    "skills.search.q-second-term": (
        lambda w, bad: w.skills.search(None, q=f"sample {bad}"),
        ValidationError,
    ),
    "skills.search.tags-list": (lambda w, bad: w.skills.search(None, tags=[bad]), ValidationError),
    "skills.search.tags-second-item": (
        lambda w, bad: w.skills.search(None, tags=["alpha", bad]),
        ValidationError,
    ),
    "skills.search.tags-string": (lambda w, bad: w.skills.search(None, tags=bad), ValidationError),
    "skills.search.category": (
        lambda w, bad: w.skills.search(None, category=bad),
        ValidationError,
    ),
    "skills.search.kind": (lambda w, bad: w.skills.search(None, kind=bad), ValidationError),
    "skills.search.status": (lambda w, bad: w.skills.search(None, status=bad), ValidationError),
    "skills.search.sort": (lambda w, bad: w.skills.search(None, sort=bad), ValidationError),
    "skills.get.ref": (lambda w, bad: w.skills.get(None, bad), NotFoundError),
    "skills.hide.ref": (lambda w, bad: w.skills.hide(w.owner, bad), NotFoundError),
    "skills.restore.ref": (lambda w, bad: w.skills.restore(w.owner, bad), NotFoundError),
    "skills.tags.kind": (lambda w, bad: w.skills.tags(None, kind=bad), ValidationError),
    "skills.categories.kind": (lambda w, bad: w.skills.categories(None, kind=bad), ValidationError),
    "versions.list.ref": (lambda w, bad: w.versions.list(None, bad), NotFoundError),
    "versions.get.ref": (lambda w, bad: w.versions.get(None, bad, "latest"), NotFoundError),
    "versions.get.version": (
        lambda w, bad: w.versions.get(None, "sample-skill", bad),
        NotFoundError,
    ),
    "versions.publish.ref": (
        lambda w, bad: w.versions.publish(w.owner, bad, "1.0.0"),
        NotFoundError,
    ),
    "versions.publish.version": (
        lambda w, bad: w.versions.publish(w.owner, "sample-skill", bad),
        NotFoundError,
    ),
    "versions.deprecate.ref": (
        lambda w, bad: w.versions.deprecate(w.owner, bad, "1.0.0"),
        NotFoundError,
    ),
    "versions.deprecate.version": (
        lambda w, bad: w.versions.deprecate(w.owner, "sample-skill", bad),
        NotFoundError,
    ),
    "packages.upload.version": (
        lambda w, bad: w.packages.upload(w.owner, b"not inspected", version=bad),
        ValidationError,
    ),
    "packages.manifest.ref": (lambda w, bad: w.packages.manifest(None, bad), NotFoundError),
    "packages.manifest.version": (
        lambda w, bad: w.packages.manifest(None, "sample-skill", bad),
        NotFoundError,
    ),
    "packages.entrypoint.ref": (lambda w, bad: w.packages.entrypoint(None, bad), NotFoundError),
    "packages.entrypoint.version": (
        lambda w, bad: w.packages.entrypoint(None, "sample-skill", bad),
        NotFoundError,
    ),
    "packages.read_file.ref": (
        lambda w, bad: w.packages.read_file(None, bad, "1.0.0", "SKILL.md"),
        NotFoundError,
    ),
    "packages.read_file.version": (
        lambda w, bad: w.packages.read_file(None, "sample-skill", bad, "SKILL.md"),
        NotFoundError,
    ),
    "packages.read_file.path": (
        lambda w, bad: w.packages.read_file(None, "sample-skill", "1.0.0", bad),
        ValidationError,
    ),
    "packages.open_raw.ref": (
        lambda w, bad: w.packages.open_raw(None, bad, "1.0.0", "SKILL.md"),
        NotFoundError,
    ),
    "packages.open_raw.version": (
        lambda w, bad: w.packages.open_raw(None, "sample-skill", bad, "SKILL.md"),
        NotFoundError,
    ),
    "packages.open_raw.path": (
        lambda w, bad: w.packages.open_raw(None, "sample-skill", "1.0.0", bad),
        ValidationError,
    ),
    "packages.download_info.ref": (
        lambda w, bad: w.packages.download_info(None, bad),
        NotFoundError,
    ),
    "packages.download_info.version": (
        lambda w, bad: w.packages.download_info(None, "sample-skill", bad),
        NotFoundError,
    ),
    "packages.open_download.ref": (
        lambda w, bad: w.packages.open_download(None, bad),
        NotFoundError,
    ),
    "packages.open_download.version": (
        lambda w, bad: w.packages.open_download(None, "sample-skill", bad),
        NotFoundError,
    ),
}


@pytest.mark.parametrize("bad", UNENCODABLE)
@pytest.mark.parametrize("entry_point", list(ENTRY_POINTS))
async def test_text_that_python_accepts_but_utf8_cannot_encode_is_a_domain_error(
    world, engine, entry_point, bad
):
    call, expected = ENTRY_POINTS[entry_point]

    with record_parameters(engine) as parameters, pytest.raises(DomainError) as raised:
        await call(world, bad)

    assert isinstance(raised.value, expected)
    assert not isinstance(raised.value, UnicodeError)
    assert not has_surrogate(parameters)


@pytest.mark.parametrize("bad", UNENCODABLE)
@pytest.mark.parametrize("entry_point", list(ENTRY_POINTS))
async def test_a_rejected_input_leaves_the_session_and_the_catalog_usable(world, entry_point, bad):
    call, _ = ENTRY_POINTS[entry_point]
    with pytest.raises(DomainError):
        await call(world, bad)

    detail = await world.skills.get(None, "sample-skill")
    versions = await world.versions.list(None, "sample-skill")
    assert (detail.latest_version, [item.version for item in versions]) == (
        "1.1.0",
        ["1.1.0", "1.0.0"],
    )
    assert (await world.skills.search(None, q="sample")).total == 1


@pytest.mark.parametrize("bad", UNENCODABLE)
async def test_path_with_unencodable_text_keeps_its_invalid_path_reason(world, bad):
    for operation in (world.packages.read_file, world.packages.open_raw):
        with pytest.raises(ValidationError) as raised:
            await operation(None, "sample-skill", "1.0.0", bad)
        assert raised.value.reason == "INVALID_PATH"


async def test_valid_international_text_is_still_searched_and_resolved(world):
    page = await world.skills.search(None, q="árbol 日本語 🌳", tags=["ñ"], category="café")
    assert page.total == 0
    with pytest.raises(NotFoundError):
        await world.skills.get(None, "árbol-日本語-🌳")
    with pytest.raises(NotFoundError):
        await world.versions.get(None, "sample-skill", "1.0.0-ünï")
