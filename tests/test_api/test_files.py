from __future__ import annotations

import hashlib
from urllib.parse import quote

import pytest

from app.config.settings import settings
from app.storage import LocalBlobStore
from tests.helpers.packages import make_grounding_zip, make_skill_zip
from tests.test_api.support import (
    BINARY_PATHS,
    HIDDEN_ENTRY,
    HOSTILE_FILES,
    PACKAGE_FILES,
    PUBLISHED_ENTRY,
    Api,
    Catalog,
    assert_error,
)

BASE = f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0"
CONTENT_FIELDS = {
    "path",
    "size",
    "sha256",
    "media_type",
    "text",
    "content",
    "truncated",
    "encoding",
}
ENTRYPOINT_FIELDS = CONTENT_FIELDS | {"name", "version", "kind"}
MANIFEST_FIELDS = {"path", "size", "sha256", "media_type", "text"}
SCRIPTABLE_FRAGMENTS = ("html", "svg", "javascript", "ecmascript", "xml", "css")
CSP = "default-src 'none'; sandbox"
QUOTED_PATH = 'odd/we"ird.bin'


def as_bytes(content: str | bytes) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else content


def url_of(path: str) -> str:
    return quote(path, safe="/")


@pytest.mark.asyncio
async def test_the_entrypoint_comes_with_the_resolved_name_version_and_kind(
    api: Api, catalog: Catalog
):
    response = await api.get(f"{BASE}/entrypoint")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == ENTRYPOINT_FIELDS
    assert body["path"] == "SKILL.md"
    assert body["name"] == PUBLISHED_ENTRY
    assert body["version"] == "1.0.0"
    assert body["kind"] == "skill"
    assert body["text"] is True
    assert body["truncated"] is False
    assert body["encoding"] == "utf-8"
    assert body["media_type"] == "text/markdown"
    assert body["content"].startswith("---\nname: pub-skill\n")
    assert f"Body of {PUBLISHED_ENTRY}." in body["content"]
    assert body["size"] == len(body["content"].encode())
    assert body["sha256"] == hashlib.sha256(body["content"].encode()).hexdigest()


@pytest.mark.asyncio
async def test_the_entrypoint_of_latest_reports_the_version_it_resolved(api: Api, catalog: Catalog):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/latest/entrypoint")

    assert response.json()["version"] == "1.1.0"
    assert "1.1.0" in response.json()["content"]


@pytest.mark.asyncio
async def test_a_grounding_package_has_an_index_entrypoint(api: Api, catalog: Catalog):
    response = await api.get(f"/skills/{HIDDEN_ENTRY}/versions/1.0.0/entrypoint", "owner")

    body = response.json()
    assert body["path"] == "INDEX.md"
    assert body["kind"] == "grounding"
    assert body["name"] == HIDDEN_ENTRY


@pytest.mark.asyncio
async def test_a_long_entrypoint_is_cut_at_the_inline_limit_on_a_character_boundary(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    body = "é" * 400
    await api.upload(make_skill_zip("wide-skill", "1.0.0", body=body), "owner", publish=True)
    monkeypatch.setattr(settings.registry.limits, "max_inline_read_bytes", 301)

    response = await api.get("/skills/wide-skill/versions/1.0.0/entrypoint")

    result = response.json()
    assert result["truncated"] is True
    assert result["size"] > 301
    assert len(result["content"].encode()) <= 301
    assert result["content"].startswith("---\nname: wide-skill")
    assert "�" not in result["content"]


@pytest.mark.asyncio
async def test_the_file_listing_is_the_manifest_sorted_by_path(api: Api, catalog: Catalog):
    response = await api.get(f"{BASE}/files")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"name", "version", "files"}
    assert body["name"] == PUBLISHED_ENTRY
    assert body["version"] == "1.0.0"
    paths = [item["path"] for item in body["files"]]
    assert paths == sorted(paths)
    assert set(paths) == {"SKILL.md", *PACKAGE_FILES}
    assert all(set(item) == MANIFEST_FIELDS for item in body["files"])


@pytest.mark.asyncio
async def test_the_manifest_describes_each_file_truthfully(api: Api, catalog: Catalog):
    body = (await api.get(f"{BASE}/files")).json()

    by_path = {item["path"]: item for item in body["files"]}
    for path, content in PACKAGE_FILES.items():
        raw = as_bytes(content)
        assert by_path[path]["size"] == len(raw), path
        assert by_path[path]["sha256"] == hashlib.sha256(raw).hexdigest(), path
    for path in BINARY_PATHS:
        assert by_path[path]["text"] is False, path
    assert by_path["docs/guide.md"]["text"] is True
    assert by_path["docs/guide.md"]["media_type"] == "text/markdown"
    assert by_path["scripts/run.py"]["media_type"] == "text/x-python"


@pytest.mark.asyncio
async def test_the_latest_listing_names_the_version_it_resolved(api: Api, catalog: Catalog):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/latest/files")

    assert response.json()["version"] == "1.1.0"


@pytest.mark.asyncio
async def test_a_text_file_is_returned_as_json_with_its_content(api: Api, catalog: Catalog):
    response = await api.get(f"{BASE}/files/docs/guide.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == CONTENT_FIELDS
    assert body["path"] == "docs/guide.md"
    assert body["content"] == "# Guide\n\nHello there\n"
    assert body["text"] is True
    assert body["truncated"] is False
    assert body["size"] == len(body["content"])
    assert body["sha256"] == hashlib.sha256(body["content"].encode()).hexdigest()


@pytest.mark.asyncio
async def test_a_path_with_non_ascii_characters_round_trips(api: Api, catalog: Catalog):
    response = await api.get(f"{BASE}/files/{url_of('notes/ünïcode.txt')}")

    assert response.status_code == 200
    assert response.json()["content"] == "héllo wörld\n"


@pytest.mark.asyncio
async def test_an_encoded_slash_is_the_same_path(api: Api, catalog: Catalog):
    response = await api.get(f"{BASE}/files/docs%2Fguide.md")

    assert response.status_code == 200
    assert response.json()["path"] == "docs/guide.md"


@pytest.mark.asyncio
async def test_an_empty_text_file_has_empty_content(api: Api, catalog: Catalog):
    response = await api.get(f"{BASE}/files/empty.txt")

    body = response.json()
    assert body["text"] is True
    assert body["content"] == ""
    assert body["size"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", BINARY_PATHS)
async def test_a_binary_file_has_no_inline_content(api: Api, catalog: Catalog, path: str):
    response = await api.get(f"{BASE}/files/{url_of(path)}")

    assert response.status_code == 200
    body = response.json()
    assert body["text"] is False
    assert body["content"] is None
    assert body["truncated"] is False
    assert body["size"] == len(as_bytes(PACKAGE_FILES[path]))


@pytest.mark.asyncio
@pytest.mark.parametrize("path", list(HOSTILE_FILES))
async def test_hostile_files_are_inert_inside_a_json_document(
    api: Api, catalog: Catalog, path: str
):
    response = await api.get(f"{BASE}/files/{path}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "html" not in response.headers["content-type"]


@pytest.mark.asyncio
async def test_a_big_text_file_is_truncated_at_the_inline_limit(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    text = "line\n" * 400
    await api.upload(
        make_skill_zip("big-skill", "1.0.0", files={"big.txt": text}), "owner", publish=True
    )
    monkeypatch.setattr(settings.registry.limits, "max_inline_read_bytes", 100)

    response = await api.get("/skills/big-skill/versions/1.0.0/files/big.txt")

    body = response.json()
    assert body["truncated"] is True
    assert body["size"] == len(text)
    assert body["content"] == text[:100]


@pytest.mark.asyncio
async def test_a_file_exactly_at_the_inline_limit_is_not_truncated(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    text = "x" * 64
    await api.upload(
        make_skill_zip("exact-skill", "1.0.0", files={"exact.txt": text}), "owner", publish=True
    )
    monkeypatch.setattr(settings.registry.limits, "max_inline_read_bytes", 64)

    response = await api.get("/skills/exact-skill/versions/1.0.0/files/exact.txt")

    assert response.json()["truncated"] is False
    assert response.json()["content"] == text


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["files", "raw"])
@pytest.mark.parametrize("path", ["nope.txt", "docs", "Docs/guide.md", "docs/guide.md.bak"])
async def test_a_path_missing_from_the_manifest_is_404(
    api: Api, catalog: Catalog, route: str, path: str
):
    response = await api.get(f"{BASE}/{route}/{path}")

    assert_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["files", "raw"])
@pytest.mark.parametrize(
    "path",
    [
        "..%2F..%2Fetc%2Fpasswd",
        "docs%2F..%2F..%2Fsecret",
        "%2e%2e%2fsecret",
        "%2Fetc%2Fpasswd",
        "/etc/passwd",
        "docs//guide.md",
        "docs/",
        "docs/guide.md/",
        "docs%2F.%2Fguide.md",
        "docs%5Cguide.md",
        "docs%5C..%5Csecret",
        "docs%00guide.md",
        "%00",
        "docs%01guide.md",
        "C%3A%5Cwindows",
        "a" * 300,
        "docs/" + "b" * 300,
    ],
)
async def test_paths_that_could_escape_the_package_are_rejected_with_a_reason(
    api: Api, catalog: Catalog, route: str, path: str
):
    response = await api.get(f"{BASE}/{route}/{path}")

    assert_error(response, 422, "VALIDATION_ERROR", reason="INVALID_PATH")


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["files", "raw"])
async def test_a_newline_in_the_path_matches_no_route(api: Api, catalog: Catalog, route: str):
    response = await api.get(f"{BASE}/{route}/docs%0aguide.md")

    assert_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_the_listing_route_is_not_shadowed_by_the_file_route(api: Api, catalog: Catalog):
    listing = await api.get(f"{BASE}/files")
    slash = await api.get(f"{BASE}/files/")

    assert listing.status_code == 200
    assert "files" in listing.json()
    assert slash.status_code in (404, 422)
    assert "error" in slash.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", list(HOSTILE_FILES))
async def test_hostile_files_are_served_as_inert_downloads_or_plain_text(
    api: Api, catalog: Catalog, path: str
):
    response = await api.get(f"{BASE}/raw/{path}")

    assert response.status_code == 200
    assert response.content == as_bytes(HOSTILE_FILES[path])
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == CSP
    content_type = response.headers["content-type"].lower()
    assert content_type in ("text/plain; charset=utf-8", "application/octet-stream")
    assert not any(fragment in content_type for fragment in SCRIPTABLE_FRAGMENTS)
    if content_type == "application/octet-stream":
        assert response.headers["content-disposition"].startswith("attachment")
    else:
        assert response.headers["content-disposition"] == "inline"


@pytest.mark.asyncio
async def test_a_binary_file_pretending_to_be_html_is_an_attachment(api: Api, catalog: Catalog):
    response = await api.get(f"{BASE}/raw/binary.html")

    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == 'attachment; filename="binary.html"'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("index.html", "text/plain; charset=utf-8"),
        ("icon.svg", "text/plain; charset=utf-8"),
        ("app.js", "text/plain; charset=utf-8"),
        ("style.css", "text/plain; charset=utf-8"),
        ("SKILL.md", "text/plain; charset=utf-8"),
        ("docs/guide.md", "text/plain; charset=utf-8"),
        ("scripts/run.py", "text/plain; charset=utf-8"),
        ("empty.txt", "text/plain; charset=utf-8"),
        ("data/table.bin", "application/octet-stream"),
    ],
)
async def test_the_served_media_type_ignores_the_file_extension(
    api: Api, catalog: Catalog, path: str, expected: str
):
    response = await api.get(f"{BASE}/raw/{path}")

    assert response.headers["content-type"] == expected


@pytest.mark.asyncio
async def test_binary_files_download_byte_for_byte_with_a_safe_file_name(
    api: Api, catalog: Catalog
):
    response = await api.get(f"{BASE}/raw/data/table.bin")

    assert response.content == bytes(range(256))
    assert response.headers["content-length"] == "256"
    assert response.headers["content-disposition"] == 'attachment; filename="table.bin"'


@pytest.mark.asyncio
async def test_a_quote_in_a_file_name_cannot_break_out_of_the_header(api: Api, catalog: Catalog):
    response = await api.get(f"{BASE}/raw/{url_of(QUOTED_PATH)}")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition == "attachment; filename=\"we_ird.bin\"; filename*=UTF-8''we%22ird.bin"


@pytest.mark.asyncio
async def test_a_non_ascii_file_name_gets_an_rfc_5987_name_and_an_ascii_fallback(
    api: Api, catalog: Catalog
):
    response = await api.get(f"{BASE}/raw/{url_of('data/dönnées.bin')}")

    disposition = response.headers["content-disposition"]
    assert disposition == (
        "attachment; filename=\"d_nn_es.bin\"; filename*=UTF-8''d%C3%B6nn%C3%A9es.bin"
    )
    assert response.content == bytes([0, 7, 255])


@pytest.mark.asyncio
async def test_raw_text_keeps_its_exact_bytes(api: Api, catalog: Catalog):
    response = await api.get(f"{BASE}/raw/{url_of('notes/ünïcode.txt')}")

    assert response.content == "héllo wörld\n".encode()
    assert response.headers["content-length"] == str(len(response.content))


@pytest.mark.asyncio
async def test_raw_files_of_latest_come_from_the_resolved_version(api: Api, catalog: Catalog):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/latest/raw/SKILL.md")

    assert "pub-skill 1.1.0" in response.text


@pytest.mark.asyncio
async def test_a_big_raw_text_file_is_not_truncated_by_the_inline_limit(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    text = "0123456789\n" * 300
    await api.upload(
        make_grounding_zip("big-ground", "1.0.0", files={"big.txt": text}), "owner", publish=True
    )
    monkeypatch.setattr(settings.registry.limits, "max_inline_read_bytes", 50)

    response = await api.get("/skills/big-ground/versions/1.0.0/raw/big.txt")

    assert response.text == text


@pytest.mark.asyncio
async def test_repeated_reads_open_the_stored_package_once_per_file(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    opened: list[str] = []
    real_open = LocalBlobStore.open

    async def recording(self, key):
        opened.append(key)
        return await real_open(self, key)

    monkeypatch.setattr(LocalBlobStore, "open", recording)

    for _ in range(3):
        entrypoint = await api.get(f"{BASE}/entrypoint")
        guide = await api.get(f"{BASE}/files/docs/guide.md")
        listing = await api.get(f"{BASE}/files")

    assert [entrypoint.status_code, guide.status_code, listing.status_code] == [200, 200, 200]
    assert len(opened) == 2
    assert guide.json()["content"] == as_bytes(PACKAGE_FILES["docs/guide.md"]).decode()


@pytest.mark.asyncio
async def test_the_raw_endpoint_still_checks_the_package_every_time(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    opened: list[str] = []
    real_open = LocalBlobStore.open

    async def recording(self, key):
        opened.append(key)
        return await real_open(self, key)

    monkeypatch.setattr(LocalBlobStore, "open", recording)

    first = await api.get(f"{BASE}/raw/docs/guide.md")
    second = await api.get(f"{BASE}/raw/docs/guide.md")

    assert first.content == second.content == as_bytes(PACKAGE_FILES["docs/guide.md"])
    assert len(opened) == 4
