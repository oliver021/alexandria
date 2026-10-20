from __future__ import annotations

import pytest

from app.services.package_service import PackageService
from app.services.skill_service import SkillService
from app.services.version_service import VersionService
from tests.helpers.packages import make_skill_zip
from tests.test_mcp.support import (
    PDF,
    open_client,
    payload_of,
    post,
    rpc,
    text_of,
    zip_to_base64,
)

VALID_PACKAGE = zip_to_base64(make_skill_zip("never-created", "1.0.0"))

INVALID_CALLS = [
    ("search_skills", {"limit": 51}, "limit"),
    ("search_skills", {"limit": 100}, "limit"),
    ("search_skills", {"limit": 10**9}, "limit"),
    ("search_skills", {"limit": 0}, "limit"),
    ("search_skills", {"limit": -1}, "limit"),
    ("search_skills", {"limit": "10"}, "limit"),
    ("search_skills", {"limit": "many"}, "limit"),
    ("search_skills", {"limit": 10.5}, "limit"),
    ("search_skills", {"limit": 10.0}, "limit"),
    ("search_skills", {"limit": True}, "limit"),
    ("search_skills", {"limit": None}, "limit"),
    ("search_skills", {"limit": [10]}, "limit"),
    ("search_skills", {"limit": {"n": 10}}, "limit"),
    ("search_skills", {"offset": -1}, "offset"),
    ("search_skills", {"offset": 2**31}, "offset"),
    ("search_skills", {"offset": "0"}, "offset"),
    ("search_skills", {"offset": 1.5}, "offset"),
    ("search_skills", {"offset": None}, "offset"),
    ("search_skills", {"offset": False}, "offset"),
    ("search_skills", {"kind": "other"}, "kind"),
    ("search_skills", {"kind": "SKILL"}, "kind"),
    ("search_skills", {"kind": "skill "}, "kind"),
    ("search_skills", {"kind": ""}, "kind"),
    ("search_skills", {"kind": 1}, "kind"),
    ("search_skills", {"kind": ["skill"]}, "kind"),
    ("search_skills", {"kind": {"skill": True}}, "kind"),
    ("search_skills", {"query": 123}, "query"),
    ("search_skills", {"query": None}, "query"),
    ("search_skills", {"query": ["a"]}, "query"),
    ("search_skills", {"query": "x" * 513}, "query"),
    ("search_skills", {"tags": "pdf"}, "tags"),
    ("search_skills", {"tags": [1]}, "tags"),
    ("search_skills", {"tags": [None]}, "tags"),
    ("search_skills", {"tags": {"a": 1}}, "tags"),
    ("search_skills", {"tags": ["x" * 257]}, "tags"),
    ("search_skills", {"tags": 5}, "tags"),
    ("search_skills", {"category": 5}, "category"),
    ("search_skills", {"category": ["a"]}, "category"),
    ("search_skills", {"category": "c" * 257}, "category"),
    ("get_skill", {}, "name"),
    ("get_skill", {"name": 5}, "name"),
    ("get_skill", {"name": None}, "name"),
    ("get_skill", {"name": [PDF]}, "name"),
    ("get_skill", {"name": {"name": PDF}}, "name"),
    ("get_skill", {"name": True}, "name"),
    ("get_entrypoint", {}, "name"),
    ("get_entrypoint", {"name": PDF, "version": 1}, "version"),
    ("get_entrypoint", {"name": PDF, "version": None}, "version"),
    ("get_entrypoint", {"name": PDF, "version": ["latest"]}, "version"),
    ("list_files", {}, "name"),
    ("list_files", {"name": PDF, "version": 1.0}, "version"),
    ("read_file", {"name": PDF}, "path"),
    ("read_file", {"path": "SKILL.md"}, "name"),
    ("read_file", {"name": PDF, "path": 1}, "path"),
    ("read_file", {"name": PDF, "path": None}, "path"),
    ("read_file", {"name": PDF, "path": ["SKILL.md"]}, "path"),
    ("read_file", {"name": PDF, "path": "SKILL.md", "version": 2}, "version"),
    ("get_download_info", {}, "name"),
    ("get_download_info", {"name": 1}, "name"),
    ("get_download_info", {"name": PDF, "version": False}, "version"),
    ("list_tags", {"limit": 0}, "limit"),
    ("list_tags", {"limit": 201}, "limit"),
    ("list_tags", {"limit": "5"}, "limit"),
    ("list_tags", {"limit": 5.5}, "limit"),
    ("list_tags", {"kind": "x"}, "kind"),
    ("list_categories", {"limit": -5}, "limit"),
    ("list_categories", {"limit": 1000}, "limit"),
    ("list_categories", {"limit": True}, "limit"),
    ("list_categories", {"kind": 7}, "kind"),
    ("upload_package", {}, "content_base64"),
    ("upload_package", {"content_base64": 123}, "content_base64"),
    ("upload_package", {"content_base64": None}, "content_base64"),
    ("upload_package", {"content_base64": [VALID_PACKAGE]}, "content_base64"),
    ("upload_package", {"content_base64": VALID_PACKAGE, "version": 1}, "version"),
    ("upload_package", {"content_base64": VALID_PACKAGE, "version": ["1.0.0"]}, "version"),
    ("upload_package", {"content_base64": VALID_PACKAGE, "publish": "true"}, "publish"),
    ("upload_package", {"content_base64": VALID_PACKAGE, "publish": "false"}, "publish"),
    ("upload_package", {"content_base64": VALID_PACKAGE, "publish": 1}, "publish"),
    ("upload_package", {"content_base64": VALID_PACKAGE, "publish": 0}, "publish"),
    ("upload_package", {"content_base64": VALID_PACKAGE, "publish": None}, "publish"),
    ("publish_version", {"name": PDF}, "version"),
    ("publish_version", {"version": "2.0.0"}, "name"),
    ("publish_version", {"name": PDF, "version": 2}, "version"),
    ("publish_version", {"name": PDF, "version": None}, "version"),
    ("deprecate_version", {"name": 5, "version": "1.0.0"}, "name"),
    ("deprecate_version", {"name": PDF}, "version"),
    ("deprecate_version", {"name": PDF, "version": ["1.0.0"]}, "version"),
]

VALID_EDGE_CALLS = [
    ("search_skills", {"limit": 1}),
    ("search_skills", {"limit": 50}),
    ("search_skills", {"offset": 0}),
    ("search_skills", {"offset": 2**31 - 1}),
    ("search_skills", {"query": ""}),
    ("search_skills", {"query": "x" * 512}),
    ("search_skills", {"kind": None, "category": None, "tags": []}),
    ("search_skills", {"kind": "skill"}),
    ("search_skills", {"kind": "grounding"}),
    ("search_skills", {"tags": ["x" * 256]}),
    ("search_skills", {"category": "c" * 256}),
    ("get_entrypoint", {"name": PDF, "version": "latest"}),
    ("list_tags", {"limit": 1}),
    ("list_tags", {"limit": 200}),
    ("list_categories", {"limit": 200, "kind": "grounding"}),
]


class TestInvalidArguments:
    @pytest.mark.parametrize(("tool", "arguments", "field"), INVALID_CALLS)
    async def test_the_input_schema_rejects_wrong_types_and_out_of_range_values(
        self, gateway, tool, arguments, field
    ):
        async with open_client(gateway) as client:
            result = await client.call_tool(tool, arguments)

        assert result.is_error is True
        assert result.structured_content is None
        text = text_of(result)
        assert text.startswith(f"Error executing tool {tool}: ")
        assert field in text
        assert "Traceback" not in text

    @pytest.mark.parametrize("mode", ["auto", "2026-07-28"])
    @pytest.mark.parametrize(
        ("tool", "arguments", "field"),
        [
            ("search_skills", {"limit": 51}, "limit"),
            ("search_skills", {"kind": "other"}, "kind"),
            ("get_skill", {"name": 5}, "name"),
            ("upload_package", {"content_base64": "AAAA", "publish": "yes"}, "publish"),
        ],
    )
    async def test_the_modern_protocol_era_rejects_them_the_same_way(
        self, gateway, mode, tool, arguments, field
    ):
        async with open_client(gateway, mode=mode) as client:
            result = await client.call_tool(tool, arguments)

        assert result.is_error is True
        assert field in text_of(result)

    async def test_invalid_arguments_never_reach_the_services(
        self, gateway, credentials, monkeypatch
    ):
        calls: list[str] = []

        def spy(name: str):
            async def record(self, *args, **kwargs):
                calls.append(name)
                raise AssertionError(name)

            return record

        monkeypatch.setattr(SkillService, "search", spy("search"))
        monkeypatch.setattr(SkillService, "get", spy("get"))
        monkeypatch.setattr(SkillService, "tags", spy("tags"))
        monkeypatch.setattr(PackageService, "upload", spy("upload"))
        monkeypatch.setattr(VersionService, "publish", spy("publish"))
        monkeypatch.setattr(VersionService, "deprecate", spy("deprecate"))

        async with open_client(gateway, credentials.owner_write) as client:
            for tool, arguments, _ in INVALID_CALLS:
                result = await client.call_tool(tool, arguments)
                assert result.is_error is True

        assert calls == []

    async def test_a_bad_flag_does_not_upload_the_package_it_came_with(self, gateway, credentials):
        async with open_client(gateway, credentials.owner_write) as client:
            rejected = await client.call_tool(
                "upload_package", {"content_base64": VALID_PACKAGE, "publish": "true"}
            )
            missing = await client.call_tool("get_skill", {"name": "never-created"})

        assert rejected.is_error is True
        assert text_of(missing).startswith("NOT_FOUND: ")

    @pytest.mark.usefixtures("registry")
    @pytest.mark.parametrize(("tool", "arguments"), VALID_EDGE_CALLS)
    async def test_the_inclusive_limits_of_the_schema_are_accepted(self, gateway, tool, arguments):
        async with open_client(gateway) as client:
            result = await client.call_tool(tool, arguments)

        assert result.is_error is False, text_of(result)

    @pytest.mark.usefixtures("registry")
    async def test_the_documented_defaults_apply_when_arguments_are_omitted(self, gateway):
        async with open_client(gateway) as client:
            page = payload_of(await client.call_tool("search_skills", {}))
            tags = payload_of(await client.call_tool("list_tags", {}))
            entry = payload_of(await client.call_tool("get_entrypoint", {"name": PDF}))

        assert (page["limit"], page["offset"]) == (10, 0)
        assert len(tags) <= 50
        assert entry["version"] == "1.1.0"

    async def test_missing_arguments_are_reported_for_every_required_field(self, gateway):
        async with open_client(gateway) as client:
            result = await client.call_tool("read_file", {})

        text = text_of(result)
        assert "2 validation errors" in text
        assert "name" in text and "path" in text


class TestMalformedCalls:
    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"name": 5},
            {"name": "get_skill", "arguments": []},
            {"name": "get_skill", "arguments": "name=pdf-forms"},
            {"name": "get_skill", "arguments": 7},
            {"arguments": {"name": PDF}},
        ],
    )
    async def test_a_tools_call_that_is_not_well_formed_is_a_protocol_error_not_a_crash(
        self, gateway, params
    ):
        response = await post(gateway, rpc("tools/call", params))

        assert response.status_code in {200, 400}
        assert "error" in response.json() or response.json()["result"]["isError"] is True
        followup = await post(gateway, rpc("tools/list"))
        assert followup.status_code == 200

    @pytest.mark.usefixtures("registry")
    async def test_extra_unknown_arguments_do_not_change_the_result(self, gateway):
        async with open_client(gateway) as client:
            plain = await client.call_tool("get_skill", {"name": PDF})
            extra = await client.call_tool("get_skill", {"name": PDF, "unexpected": 1})

        assert extra.structured_content == plain.structured_content
