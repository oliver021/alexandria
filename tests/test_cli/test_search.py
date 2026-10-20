from __future__ import annotations

import httpx
import pytest

from app import cli
from tests.helpers.packages import make_grounding_zip, make_skill_zip
from tests.test_cli.support import FakeRegistry, remote_env, run

BIDI_OVERRIDE = chr(0x202E)


@pytest.fixture
def catalog(registry: FakeRegistry) -> FakeRegistry:
    registry.add_package(
        make_skill_zip(
            "git-changelog",
            "1.2.0",
            description="Write a changelog from git history.",
            tags=["git", "changelog"],
            category="release",
        )
    )
    registry.add_package(
        make_skill_zip(
            "git-bisect-helper",
            "0.3.1",
            description="Find the commit that broke a test.",
            tags=["git", "debugging"],
        )
    )
    registry.add_package(
        make_grounding_zip(
            "http-status",
            "2.0.0",
            description="HTTP status codes reference.",
            tags=["http", "reference"],
        )
    )
    registry.add_package(make_skill_zip("hidden-draft", "0.1.0"), status="draft")
    return registry


def search(registry: FakeRegistry, *argv: object, env: dict[str, str] | None = None):
    return run("search", *argv, transport=registry.transport, env=env if env is not None else {})


def names(output: str) -> list[str]:
    rows = output.split("\n\n", 1)[0].splitlines()[1:]
    return [row.split()[0] for row in rows]


class TestResults:
    def test_listing_everything_shows_the_visible_entries_as_a_table(self, catalog: FakeRegistry):
        result = search(catalog)

        assert result.code == 0
        assert result.err == ""
        lines = result.out.splitlines()
        assert lines[0].split() == ["NAME", "KIND", "LATEST", "DESCRIPTION"]
        assert names(result.out) == ["git-bisect-helper", "git-changelog", "http-status"]
        assert "hidden-draft" not in result.out
        assert lines[-1] == "3 of 3 result(s) shown"
        row = next(line for line in lines if line.startswith("git-changelog"))
        assert row.split()[:3] == ["git-changelog", "skill", "1.2.0"]
        assert row.endswith("Write a changelog from git history.")
        status_row = next(line for line in lines if line.startswith("http-status"))
        assert status_row.split()[:3] == ["http-status", "grounding", "2.0.0"]

    def test_columns_are_aligned(self, catalog: FakeRegistry):
        header, first = search(catalog).out.splitlines()[:2]

        assert header.index("KIND") == first.index("skill")
        assert header.index("LATEST") == first.index("0.3.1")
        assert header.index("DESCRIPTION") == first.index("Find the commit")

    def test_the_query_filters_by_text(self, catalog: FakeRegistry):
        result = search(catalog, "changelog")

        assert names(result.out) == ["git-changelog"]
        assert catalog.last_request().url.params["q"] == "changelog"

    def test_several_words_must_all_match(self, catalog: FakeRegistry):
        assert names(search(catalog, "git test").out) == ["git-bisect-helper"]

    def test_words_may_be_given_as_separate_arguments(self, catalog: FakeRegistry):
        result = search(catalog, "git", "test")

        assert names(result.out) == ["git-bisect-helper"]
        assert catalog.last_request().url.params["q"] == "git test"

    def test_options_may_come_before_or_after_the_words(self, catalog: FakeRegistry):
        before = search(catalog, "--kind", "skill", "git", "changelog")
        after = search(catalog, "git", "changelog", "--kind", "skill")

        assert names(before.out) == names(after.out) == ["git-changelog"]

    def test_a_double_dash_allows_words_that_look_like_options(self, catalog: FakeRegistry):
        result = search(catalog, "--", "-nothing")

        assert result.out == "No results.\n"
        assert catalog.last_request().url.params["q"] == "-nothing"

    def test_blank_words_mean_no_q_parameter(self, catalog: FakeRegistry):
        result = search(catalog, "", "  ")

        assert len(names(result.out)) == 3
        assert "q" not in catalog.last_request().url.params

    def test_no_query_means_no_q_parameter(self, catalog: FakeRegistry):
        search(catalog)

        assert "q" not in catalog.last_request().url.params

    def test_the_kind_filter_is_sent_and_applied(self, catalog: FakeRegistry):
        result = search(catalog, "--kind", "grounding")

        assert names(result.out) == ["http-status"]
        assert catalog.last_request().url.params["kind"] == "grounding"

    def test_tags_are_repeatable_and_combined(self, catalog: FakeRegistry):
        result = search(catalog, "--tag", "git", "--tag", "debugging")

        assert names(result.out) == ["git-bisect-helper"]
        assert catalog.last_request().url.params.get_list("tag") == ["git", "debugging"]

    def test_tags_are_normalised_like_package_tags(self, catalog: FakeRegistry):
        search(catalog, "--tag", "  GIT ")

        assert catalog.last_request().url.params.get_list("tag") == ["git"]

    def test_everything_can_be_combined(self, catalog: FakeRegistry):
        result = search(
            catalog, "git", "--kind", "skill", "--tag", "git", "--limit", "5", "--offset", "0"
        )

        assert names(result.out) == ["git-bisect-helper", "git-changelog"]
        params = catalog.last_request().url.params
        assert (params["limit"], params["offset"]) == ("5", "0")

    def test_no_results_say_so(self, catalog: FakeRegistry):
        result = search(catalog, "zzz-nothing")

        assert result.code == 0
        assert result.out == "No results.\n"

    def test_the_default_limit_is_twenty(self, catalog: FakeRegistry):
        search(catalog)

        assert catalog.last_request().url.params["limit"] == "20"
        assert catalog.last_request().url.params["offset"] == "0"


class TestPagination:
    def test_more_results_are_announced_with_the_next_offset(self, catalog: FakeRegistry):
        result = search(catalog, "--limit", "2")

        assert names(result.out) == ["git-bisect-helper", "git-changelog"]
        assert result.out.splitlines()[-2:] == [
            "2 of 3 result(s) shown",
            "More results are available: use --offset 2",
        ]

    def test_the_next_page_continues_and_finishes(self, catalog: FakeRegistry):
        result = search(catalog, "--limit", "2", "--offset", "2")

        assert names(result.out) == ["http-status"]
        assert result.out.splitlines()[-1] == "1 of 3 result(s) shown"
        assert "More results" not in result.out

    def test_an_offset_beyond_the_end_is_an_empty_page(self, catalog: FakeRegistry):
        result = search(catalog, "--offset", "50")

        assert result.out == "No results.\n"


class TestJson:
    def test_json_is_the_server_page_unchanged(self, catalog: FakeRegistry):
        result = search(catalog, "git", "--json")

        assert result.code == 0
        page = result.json()
        assert page["total"] == 2
        assert (page["limit"], page["offset"]) == (20, 0)
        assert [item["name"] for item in page["items"]] == ["git-bisect-helper", "git-changelog"]
        assert set(page["items"][0]) >= {"id", "name", "kind", "tags", "status", "latest_version"}

    def test_json_with_no_results_is_still_a_page(self, catalog: FakeRegistry):
        page = search(catalog, "zzz", "--json").json()

        assert page["items"] == [] and page["total"] == 0


class TestAuthentication:
    def test_a_token_is_sent_when_available(self, catalog: FakeRegistry):
        search(catalog, env=remote_env("alx_read"))

        assert catalog.last_request().headers["Authorization"] == "Bearer alx_read"

    def test_no_authorization_header_without_a_token(self, catalog: FakeRegistry):
        search(catalog)

        assert "Authorization" not in catalog.last_request().headers

    def test_a_private_registry_asks_for_a_token(self, catalog: FakeRegistry):
        catalog.public_read = False

        result = search(catalog)

        assert result.code == 1
        assert result.err.startswith("error: UNAUTHORIZED: Authentication required")
        assert "pass --token or set ALX_TOKEN" in result.err

    def test_a_private_registry_is_searchable_with_a_token(self, catalog: FakeRegistry):
        catalog.public_read = False

        result = search(catalog, "--token", "alx_read")

        assert result.code == 0
        assert len(names(result.out)) == 3

    def test_flags_override_the_environment(self, catalog: FakeRegistry):
        search(catalog, "--url", "http://localhost:7000", env=remote_env("alx_read"))

        assert catalog.last_request().url.port == 7000


class TestPresentation:
    def test_long_descriptions_are_truncated_in_the_table_but_not_in_json(
        self, registry: FakeRegistry
    ):
        long_text = "word " * 100
        registry.add_package(make_skill_zip("verbose", "1.0.0", description=long_text.strip()))

        table = search(registry).out
        full = search(registry, "--json").json()["items"][0]["description"]

        assert "..." in table
        assert max(len(line) for line in table.splitlines()) <= 120
        assert full == long_text.strip()

    def test_untrusted_text_cannot_inject_terminal_control_sequences(self, registry: FakeRegistry):
        page = {
            "items": [
                {
                    "name": "evil\x1b[2Jname",
                    "kind": "skill",
                    "latest_version": "1.0.0\x07",
                    "description": "line one\nline two\x1b]0;title\x07 end " + BIDI_OVERRIDE,
                    "status": "published",
                }
            ],
            "total": 1,
            "limit": 20,
            "offset": 0,
        }
        registry.raw_response = httpx.Response(200, json=page)

        result = search(registry)

        assert result.code == 0
        assert "\x1b" not in result.out
        assert "\x07" not in result.out
        assert BIDI_OVERRIDE not in result.out
        assert "line one line two" in result.out

    def test_the_status_column_appears_only_when_something_is_not_published(
        self, registry: FakeRegistry
    ):
        page = {
            "items": [
                {
                    "name": "a-skill",
                    "kind": "skill",
                    "latest_version": "1.0.0",
                    "status": "published",
                },
                {"name": "b-skill", "kind": "skill", "latest_version": None, "status": "draft"},
            ],
            "total": 2,
            "limit": 20,
            "offset": 0,
        }
        registry.raw_response = httpx.Response(200, json=page)

        lines = search(registry).out.splitlines()

        assert lines[0].split() == ["NAME", "KIND", "LATEST", "STATUS", "DESCRIPTION"]
        assert lines[1].split() == ["a-skill", "skill", "1.0.0", "published"]
        assert lines[2].split() == ["b-skill", "skill", "-", "draft"]

    def test_incomplete_items_are_shown_with_placeholders(self, registry: FakeRegistry):
        registry.raw_response = httpx.Response(
            200, json={"items": [{"name": "bare"}, "junk", 7], "limit": 20, "offset": 0}
        )

        result = search(registry)

        assert result.code == 0
        assert result.out.splitlines()[1].split() == ["bare", "?", "-"]
        assert result.out.splitlines()[-1] == "1 of 1 result(s) shown"


class TestUsage:
    @pytest.mark.parametrize(
        "argv",
        [
            ["--limit", "0"],
            ["--limit", "101"],
            ["--limit", "-3"],
            ["--limit", "ten"],
            ["--offset", "-1"],
            ["--offset", "x"],
            ["--kind", "widget"],
            ["--tag"],
        ],
    )
    def test_bad_arguments_are_usage_errors(self, catalog: FakeRegistry, argv: list[str]):
        result = search(catalog, *argv)

        assert result.code == 2
        assert result.out == ""
        assert catalog.requests == []

    def test_the_limit_bounds_are_accepted(self, catalog: FakeRegistry):
        assert search(catalog, "--limit", "1").code == 0
        assert search(catalog, "--limit", "100").code == 0

    def test_server_errors_are_reported(self, catalog: FakeRegistry):
        catalog.raw_response = httpx.Response(
            422,
            json={"error": {"code": "VALIDATION_ERROR", "message": "limit must be small"}},
        )

        result = search(catalog)

        assert result.code == 1
        assert result.err == "error: VALIDATION_ERROR: limit must be small\n"

    @pytest.mark.parametrize("body", [[], {"total": 1}, {"items": "no"}, "text"])
    def test_a_malformed_page_is_an_error(self, catalog: FakeRegistry, body: object):
        catalog.raw_response = httpx.Response(200, json=body)

        result = search(catalog)

        assert result.code == 1
        assert "BAD_RESPONSE" in result.err

    def test_network_errors_are_reported(self, catalog: FakeRegistry):
        catalog.failure = httpx.ConnectError("refused")

        result = search(catalog)

        assert result.code == 1
        assert result.err.startswith("error: NETWORK_ERROR: cannot connect to")


class TestHostileAnswers:
    def test_an_endless_answer_is_refused(self, catalog: FakeRegistry, monkeypatch):
        monkeypatch.setattr(cli, "MAX_RESPONSE_BYTES", 200)
        catalog.raw_response = httpx.Response(
            200, content=b'{"items": [], "padding": "' + b"x" * 500 + b'"}'
        )

        result = search(catalog)

        assert result.code == 1
        assert result.err.startswith("error: TOO_LARGE: the server sent more than 200 bytes")

    def test_a_deeply_nested_answer_is_a_bad_answer(self, catalog: FakeRegistry):
        catalog.raw_response = httpx.Response(200, content=b"[" * 200_000 + b"]" * 200_000)

        result = search(catalog)

        assert result.code == 1
        assert "BAD_RESPONSE" in result.err
