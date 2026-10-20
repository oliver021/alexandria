from __future__ import annotations

import json

import pytest
from httpx import Response

from tests.test_integration.support import (
    differences,
    mcp_outcome,
    parse_tool_outcome,
    rest_outcome,
)


def rpc_response(result: dict) -> Response:
    return Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def tool_text(text: str, *, error: bool = False, structured: object = None) -> Response:
    result: dict = {"content": [{"type": "text", "text": text}], "isError": error}
    if structured is not None:
        result["structuredContent"] = structured
    return rpc_response(result)


class TestDifferences:
    def test_equal_documents_have_no_differences(self):
        document = {"a": [1, {"b": None, "c": "x"}], "d": 1.5, "e": True}

        assert differences(document, json.loads(json.dumps(document))) == []

    def test_a_missing_key_is_reported_on_the_side_that_lacks_it(self):
        found = differences({"a": 1, "b": 2}, {"a": 1})
        other = differences({"a": 1}, {"a": 1, "b": 2})

        assert found == ["$.b: only REST has it (2)"]
        assert other == ["$.b: only MCP has it (2)"]

    def test_a_changed_value_is_reported_with_its_path(self):
        found = differences({"items": [{"tags": ["a", "b"]}]}, {"items": [{"tags": ["a", "c"]}]})

        assert found == ["$.items[0].tags[1]: REST 'b' != MCP 'c'"]

    def test_lists_of_different_lengths_are_reported_once(self):
        assert differences([1, 2, 3], [1, 2]) == ["$: REST has 3 items, MCP has 2"]

    @pytest.mark.parametrize(
        ("rest", "mcp"),
        [(1, True), (0, False), (1, 1.0), ("1", 1), (None, "null"), ([], {}), ({}, []), (None, 0)],
    )
    def test_values_of_different_types_never_compare_equal(self, rest, mcp):
        assert differences(rest, mcp) != []

    def test_null_and_a_missing_key_are_not_the_same_thing(self):
        assert differences({"a": None}, {}) == ["$.a: only REST has it (None)"]

    def test_the_order_of_a_list_matters_but_the_order_of_keys_does_not(self):
        assert differences({"a": 1, "b": 2}, {"b": 2, "a": 1}) == []
        assert differences(["a", "b"], ["b", "a"]) != []


class TestToolOutcomes:
    def test_a_success_carries_the_parsed_json_and_the_structured_content(self):
        outcome = parse_tool_outcome(tool_text('{"a":1}', structured={"a": 1}))

        assert outcome.ok and outcome.code is None
        assert outcome.payload == outcome.structured == {"a": 1}
        assert mcp_outcome(outcome).code is None

    def test_a_tool_error_is_split_into_code_message_and_reason(self):
        outcome = parse_tool_outcome(
            tool_text("VALIDATION_ERROR: The path 'a [b]' is invalid [INVALID_PATH]", error=True)
        )

        assert not outcome.ok
        assert (outcome.code, outcome.message, outcome.reason) == (
            "VALIDATION_ERROR",
            "The path 'a [b]' is invalid",
            "INVALID_PATH",
        )

    def test_a_tool_error_without_a_reason_has_none(self):
        outcome = parse_tool_outcome(tool_text("NOT_FOUND: Skill not found: 'x'", error=True))

        assert (outcome.code, outcome.message, outcome.reason) == (
            "NOT_FOUND",
            "Skill not found: 'x'",
            None,
        )

    def test_an_http_level_rejection_is_an_error_with_the_http_status(self):
        response = Response(401, json={"error": {"code": "UNAUTHORIZED", "message": "No."}})

        outcome = parse_tool_outcome(response)

        assert (outcome.status, outcome.code, outcome.message) == (401, "UNAUTHORIZED", "No.")
        assert outcome.is_error

    def test_a_text_that_is_not_in_the_error_format_has_no_code(self):
        outcome = parse_tool_outcome(tool_text("Error executing tool x: boom", error=True))

        assert outcome.code is None
        assert outcome.is_error


class TestRestOutcomes:
    def test_a_2xx_response_is_a_success(self):
        outcome = rest_outcome(Response(201, json={}))

        assert (outcome.code, outcome.status) == (None, 201)

    def test_an_error_response_is_its_error_code(self):
        response = Response(409, json={"error": {"code": "CONFLICT", "message": "x"}})

        outcome = rest_outcome(response)

        assert (outcome.code, outcome.status) == ("CONFLICT", 409)
