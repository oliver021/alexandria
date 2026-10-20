from __future__ import annotations

import uuid
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import update

from app.api.deps import OptionalPrincipal, RequiredPrincipal, WriterPrincipal
from app.api.documentation import DocumentedRoute
from app.config.settings import settings
from app.main import app
from app.models import SkillVersion
from app.models.schemas import ErrorResponse
from app.storage import BlobStoreError, LocalBlobStore
from tests.helpers.packages import make_skill_zip
from tests.test_api.support import PUBLISHED_ENTRY, Api, Catalog, assert_error

PREFIX = "/api/v1"
VERSION_BASE = "/skills/{ref}/versions/{version}"

CONTRACT_ROUTES = {
    ("GET", "/health"),
    ("GET", "/info"),
    ("POST", "/auth/login"),
    ("GET", "/auth/me"),
    ("GET", "/skills"),
    ("GET", "/skills/{ref}"),
    ("DELETE", "/skills/{ref}"),
    ("POST", "/skills/{ref}/restore"),
    ("POST", "/packages"),
    ("GET", "/skills/{ref}/versions"),
    ("GET", VERSION_BASE),
    ("POST", f"{VERSION_BASE}/publish"),
    ("POST", f"{VERSION_BASE}/deprecate"),
    ("GET", f"{VERSION_BASE}/entrypoint"),
    ("GET", f"{VERSION_BASE}/files"),
    ("GET", f"{VERSION_BASE}/files/{{path}}"),
    ("GET", f"{VERSION_BASE}/raw/{{path}}"),
    ("GET", f"{VERSION_BASE}/download"),
    ("GET", "/tags"),
    ("GET", "/categories"),
    ("POST", "/tokens"),
    ("GET", "/tokens"),
    ("DELETE", "/tokens/{id}"),
    ("GET", "/users"),
    ("POST", "/users"),
}

REMOVED_ROUTES = {
    ("POST", "/skills"),
    ("PUT", "/skills/{id}"),
    ("PUT", "/skills/{ref}"),
    ("PATCH", "/skills/{ref}"),
    ("POST", "/skills/{id}/versions"),
    ("POST", "/skills/{ref}/versions"),
    ("POST", "/skills/{id}/versions/{version}/unpublish"),
    ("POST", f"{VERSION_BASE}/unpublish"),
}

PUBLIC_ROUTES = {("GET", "/health"), ("GET", "/info"), ("POST", "/auth/login")}
READ_ROUTES = {
    ("GET", "/skills"),
    ("GET", "/skills/{ref}"),
    ("GET", "/skills/{ref}/versions"),
    ("GET", VERSION_BASE),
    ("GET", f"{VERSION_BASE}/entrypoint"),
    ("GET", f"{VERSION_BASE}/files"),
    ("GET", f"{VERSION_BASE}/files/{{path}}"),
    ("GET", f"{VERSION_BASE}/raw/{{path}}"),
    ("GET", f"{VERSION_BASE}/download"),
    ("GET", "/tags"),
    ("GET", "/categories"),
}
ANONYMOUS_SECURITY = [{}, {"BearerAuth": []}]
REQUIRED_SECURITY = [{"BearerAuth": []}]
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return app.openapi()


def operations(spec: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for path, item in spec["paths"].items():
        if not path.startswith(PREFIX):
            continue
        for method, operation in item.items():
            if method in METHODS:
                found[(method.upper(), path.removeprefix(PREFIX))] = operation
    return found


def test_the_documented_api_has_exactly_the_routes_of_the_contract(spec):
    assert set(operations(spec)) == CONTRACT_ROUTES


def test_the_old_routes_are_gone(spec):
    assert not REMOVED_ROUTES & set(operations(spec))


def test_every_operation_has_a_summary_a_description_and_one_tag(spec):
    for key, operation in operations(spec).items():
        assert operation.get("summary"), key
        assert len(operation.get("description", "")) > 40, key
        assert len(operation.get("tags", [])) == 1, key
        assert operation["tags"][0] in {
            "system",
            "auth",
            "skills",
            "versions",
            "files",
            "packages",
            "taxonomy",
            "tokens",
            "users",
        }, key


def test_operation_ids_are_unique(spec):
    ids = [operation["operationId"] for operation in operations(spec).values()]

    assert len(ids) == len(set(ids))


def test_the_title_and_description_describe_the_registry(spec):
    assert spec["info"]["title"] == "Alexandria"
    assert spec["info"]["version"]


def test_the_bearer_scheme_is_the_only_security_scheme_and_explains_anonymous_reading(spec):
    schemes = spec["components"]["securitySchemes"]

    assert schemes == {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT or API token",
            "description": schemes["BearerAuth"]["description"],
        }
    }
    assert "public_read" in schemes["BearerAuth"]["description"]


def test_the_public_operations_declare_no_security_at_all(spec):
    found = operations(spec)

    for key in PUBLIC_ROUTES:
        assert "security" not in found[key], key


def test_read_operations_accept_anonymous_callers_or_a_bearer_credential(spec):
    found = operations(spec)

    for key in READ_ROUTES:
        assert found[key]["security"] == ANONYMOUS_SECURITY, key


def test_every_other_operation_requires_the_bearer_credential(spec):
    found = operations(spec)

    for key in set(found) - PUBLIC_ROUTES - READ_ROUTES:
        assert found[key]["security"] == REQUIRED_SECURITY, key


def test_the_three_kinds_of_operation_cover_the_whole_api_without_overlap(spec):
    assert not PUBLIC_ROUTES & READ_ROUTES
    assert PUBLIC_ROUTES | READ_ROUTES <= set(operations(spec))
    assert len(READ_ROUTES) == 11


def concrete(template: str) -> str:
    return (
        template.replace("{ref}", PUBLISHED_ENTRY)
        .replace("{version}", "1.0.0")
        .replace("{path}", "docs/guide.md")
        .replace("{id}", str(uuid.uuid4()))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "template"), sorted(CONTRACT_ROUTES))
async def test_the_declared_security_matches_how_anonymous_callers_are_really_treated(
    api: Api, mini_catalog: Catalog, spec, method: str, template: str
):
    declared = operations(spec)[(method, template)].get("security")
    extra = {"json": {}} if method == "POST" else {}

    response = await api.request(method, concrete(template), "anonymous", **extra)

    if declared is None or {} in declared:
        assert response.status_code != 401
    else:
        assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize("template", sorted(template for method, template in READ_ROUTES))
async def test_the_anonymous_alternative_is_conditional_on_public_reading(
    api: Api, mini_catalog: Catalog, monkeypatch: pytest.MonkeyPatch, template: str
):
    monkeypatch.setattr(settings.registry, "public_read", False)

    response = await api.get(concrete(template), "anonymous")

    assert_error(response, 401, "UNAUTHORIZED")


async def nested_optional(principal: OptionalPrincipal) -> str:
    return "optional"


async def nested_writer(principal: WriterPrincipal) -> str:
    return "writer"


async def open_route() -> dict[str, str]:
    return {}


async def optional_route(principal: OptionalPrincipal) -> dict[str, str]:
    return {}


async def required_route(principal: RequiredPrincipal) -> dict[str, str]:
    return {}


async def writer_route(principal: WriterPrincipal) -> dict[str, str]:
    return {}


async def through_optional(value: Annotated[str, Depends(nested_optional)]) -> dict[str, str]:
    return {}


async def through_writer(value: Annotated[str, Depends(nested_writer)]) -> dict[str, str]:
    return {}


async def mixed_route(optional: OptionalPrincipal, required: RequiredPrincipal) -> dict[str, str]:
    return {}


def build_documented_app() -> FastAPI:
    router = APIRouter(route_class=DocumentedRoute)
    for path, endpoint in (
        ("/open", open_route),
        ("/optional", optional_route),
        ("/required", required_route),
        ("/writer", writer_route),
        ("/through-optional", through_optional),
        ("/through-writer", through_writer),
        ("/mixed", mixed_route),
    ):
        router.add_api_route(path, endpoint, methods=["GET"])
    application = FastAPI()
    application.include_router(router)
    return application


def test_the_route_class_derives_the_security_from_the_dependencies_of_any_route():
    paths = build_documented_app().openapi()["paths"]

    assert {path: item["get"].get("security") for path, item in paths.items()} == {
        "/open": None,
        "/optional": ANONYMOUS_SECURITY,
        "/required": REQUIRED_SECURITY,
        "/writer": REQUIRED_SECURITY,
        "/through-optional": ANONYMOUS_SECURITY,
        "/through-writer": REQUIRED_SECURITY,
        "/mixed": REQUIRED_SECURITY,
    }


async def extra_route(principal: OptionalPrincipal) -> dict[str, str]:
    return {}


def test_routes_that_keep_their_own_openapi_extra_still_get_the_security():
    router = APIRouter(route_class=DocumentedRoute)
    router.add_api_route("/extra", extra_route, methods=["GET"], openapi_extra={"x-note": "kept"})
    application = FastAPI()
    application.include_router(router)

    operation = application.openapi()["paths"]["/extra"]["get"]

    assert operation["x-note"] == "kept"
    assert operation["security"] == ANONYMOUS_SECURITY


def test_the_real_application_builds_its_specification_once():
    assert app.openapi() is app.openapi()
    assert app.openapi()["paths"]["/api/v1/skills"]["get"]["security"] == ANONYMOUS_SECURITY


def test_the_anonymous_security_is_generated_once_and_cached_by_the_application():
    application = build_documented_app()

    first = application.openapi()
    second = application.openapi()

    assert first is second


STORAGE_SCENARIOS = ("lost_blob", "unreadable_manifest", "unreadable_tags", "unreadable_metadata")
OPERATION_ACTORS = {("GET", "/users"): "admin", ("POST", "/users"): "admin"}
OPERATION_BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/tokens"): {"name": "probe", "scope": "read"},
    ("POST", "/users"): {
        "username": "zed",
        "email": "zed@example.com",
        "password": "correct horse battery",
    },
    ("POST", "/auth/login"): {"username": "nobody", "password": "wrong"},
}
STORAGE_ERROR_REACHED_BY = {
    ("GET", "/skills/{ref}"): "unreadable_tags",
    ("POST", "/skills/{ref}/restore"): "unreadable_tags",
    ("GET", "/skills/{ref}/versions"): "unreadable_tags",
    ("GET", VERSION_BASE): "unreadable_metadata",
    ("POST", f"{VERSION_BASE}/publish"): "unreadable_tags",
    ("POST", f"{VERSION_BASE}/deprecate"): "unreadable_tags",
    ("GET", f"{VERSION_BASE}/entrypoint"): "lost_blob",
    ("GET", f"{VERSION_BASE}/files"): "unreadable_manifest",
    ("GET", f"{VERSION_BASE}/files/{{path}}"): "lost_blob",
    ("GET", f"{VERSION_BASE}/raw/{{path}}"): "lost_blob",
    ("GET", f"{VERSION_BASE}/download"): "lost_blob",
    ("POST", "/packages"): "store_refuses_writes",
}


async def damage_storage(scenario: str, db_session, blob_store, monkeypatch) -> None:
    if scenario == "lost_blob":
        for path in blob_store.root.rglob("*.zip"):
            path.unlink()
    elif scenario == "store_refuses_writes":

        async def refuse(self, key, data):
            raise BlobStoreError("the disk is full")

        monkeypatch.setattr(LocalBlobStore, "put", refuse)
    else:
        column = scenario.removeprefix("unreadable_")
        column = "metadata_" if column == "metadata" else column
        await db_session.execute(update(SkillVersion).values({column: "{not json"}))
        await db_session.commit()


async def call_operation(api: Api, method: str, template: str):
    key = (method, template)
    actor = OPERATION_ACTORS.get(key, "owner")
    if key == ("POST", "/packages"):
        return await api.upload(make_skill_zip("probe-skill", "1.0.0"), actor)
    kwargs: dict[str, Any] = {}
    if key in OPERATION_BODIES:
        kwargs["json"] = OPERATION_BODIES[key]
    elif method == "POST":
        kwargs["json"] = {}
    return await api.request(method, concrete(template), actor, **kwargs)


def is_storage_error(response) -> bool:
    if response.status_code != 500:
        return False
    assert response.json()["error"]["code"] == "STORAGE_ERROR", response.text
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [*STORAGE_SCENARIOS, "store_refuses_writes"])
@pytest.mark.parametrize(("method", "template"), sorted(CONTRACT_ROUTES))
async def test_no_operation_answers_storage_error_without_documenting_it(
    api: Api,
    mini_catalog: Catalog,
    db_session,
    blob_store,
    monkeypatch: pytest.MonkeyPatch,
    spec,
    method: str,
    template: str,
    scenario: str,
):
    await damage_storage(scenario, db_session, blob_store, monkeypatch)

    response = await call_operation(api, method, template)

    if is_storage_error(response):
        assert "500" in operations(spec)[(method, template)]["responses"], (method, template)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "scenario"), sorted(STORAGE_ERROR_REACHED_BY.items()), ids=lambda value: str(value)
)
async def test_every_documented_storage_error_really_happens(
    api: Api,
    mini_catalog: Catalog,
    db_session,
    blob_store,
    monkeypatch: pytest.MonkeyPatch,
    key: tuple[str, str],
    scenario: str,
):
    await damage_storage(scenario, db_session, blob_store, monkeypatch)

    response = await call_operation(api, *key)

    assert is_storage_error(response), (key, scenario, response.status_code)


def test_the_storage_error_is_documented_exactly_where_it_can_happen(spec):
    documented = {
        key for key, operation in operations(spec).items() if "500" in operation["responses"]
    }

    assert documented == set(STORAGE_ERROR_REACHED_BY)


def test_the_storage_error_response_names_its_code_with_a_valid_example(spec):
    for key in STORAGE_ERROR_REACHED_BY:
        response = operations(spec)[key]["responses"]["500"]
        content = response["content"]["application/json"]

        assert content["schema"]["$ref"].endswith("/ErrorResponse"), key
        example = ErrorResponse.model_validate(content["example"])
        assert example.error.code == "STORAGE_ERROR"
        assert "STORAGE_ERROR" in response["description"]


def test_no_head_operation_is_documented(spec):
    assert all("head" not in item for item in spec["paths"].values())


def test_the_error_format_is_documented_instead_of_the_default_validation_schema(spec):
    schemas = spec["components"]["schemas"]

    assert {"ErrorResponse", "ErrorDetail"} <= set(schemas)
    assert set(schemas["ErrorDetail"]["required"]) == {"code", "message"}
    assert "reason" in schemas["ErrorDetail"]["properties"]
    for key, operation in operations(spec).items():
        for status, response in operation["responses"].items():
            if status.startswith(("4", "5")):
                reference = response["content"]["application/json"]["schema"]["$ref"]
                assert reference.endswith("/ErrorResponse"), (key, status)


@pytest.mark.parametrize(
    ("key", "status", "schema"),
    [
        (("GET", "/skills"), "200", "PaginatedResult_SkillSummary_"),
        (("GET", "/skills/{ref}"), "200", "SkillDetail"),
        (("POST", "/skills/{ref}/restore"), "200", "SkillDetail"),
        (("POST", "/packages"), "201", "UploadResult"),
        (("GET", VERSION_BASE), "200", "VersionDetail"),
        (("POST", f"{VERSION_BASE}/publish"), "200", "VersionSummary"),
        (("POST", f"{VERSION_BASE}/deprecate"), "200", "VersionSummary"),
        (("GET", f"{VERSION_BASE}/entrypoint"), "200", "EntrypointContent"),
        (("GET", f"{VERSION_BASE}/files"), "200", "FileListing"),
        (("GET", f"{VERSION_BASE}/files/{{path}}"), "200", "FileContent"),
        (("POST", "/tokens"), "201", "TokenCreated"),
        (("POST", "/users"), "201", "UserResponse"),
        (("POST", "/auth/login"), "200", "LoginResponse"),
        (("GET", "/auth/me"), "200", "UserResponse"),
        (("GET", "/info"), "200", "InfoResponse"),
        (("GET", "/health"), "200", "HealthResponse"),
    ],
)
def test_responses_use_the_shared_schemas(spec, key, status, schema):
    content = operations(spec)[key]["responses"][status]["content"]["application/json"]

    assert content["schema"]["$ref"].endswith(f"/{schema}")


@pytest.mark.parametrize(
    ("key", "schema"),
    [
        (("GET", "/skills/{ref}/versions"), "VersionSummary"),
        (("GET", "/tokens"), "TokenResponse"),
        (("GET", "/users"), "UserResponse"),
        (("GET", "/tags"), "TaxonomyItem"),
        (("GET", "/categories"), "TaxonomyItem"),
    ],
)
def test_list_responses_are_arrays_of_the_shared_schemas(spec, key, schema):
    body = operations(spec)[key]["responses"]["200"]["content"]["application/json"]["schema"]

    assert body["type"] == "array"
    assert body["items"]["$ref"].endswith(f"/{schema}")


def test_bodyless_responses_are_declared_as_such(spec):
    for key in (("DELETE", "/skills/{ref}"), ("DELETE", "/tokens/{id}")):
        responses = operations(spec)[key]["responses"]
        assert "204" in responses and "200" not in responses, key
        assert "content" not in responses["204"], key


def test_the_search_parameters_are_documented(spec):
    parameters = {item["name"]: item for item in operations(spec)[("GET", "/skills")]["parameters"]}

    assert set(parameters) == {
        "q",
        "kind",
        "tag",
        "category",
        "status",
        "mine",
        "sort",
        "limit",
        "offset",
    }
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["maximum"] == 100
    assert parameters["offset"]["schema"]["minimum"] == 0
    assert parameters["tag"]["schema"]["anyOf"][0]["type"] == "array"
    assert all(item["description"] for item in parameters.values())


def test_the_upload_documents_its_multipart_body(spec):
    operation = operations(spec)[("POST", "/packages")]

    body = operation["requestBody"]
    assert body["required"] is True
    schema = body["content"]["multipart/form-data"]["schema"]
    assert schema["required"] == ["file"]
    assert schema["properties"]["file"]["format"] == "binary"
    assert schema["properties"]["version"]["type"] == "string"
    assert schema["properties"]["publish"]["type"] == "boolean"
    assert {"400", "401", "403", "409", "413", "422"} <= set(operation["responses"])


def test_the_download_documents_its_binary_response_and_its_headers(spec):
    operation = operations(spec)[("GET", f"{VERSION_BASE}/download")]

    ok = operation["responses"]["200"]
    assert ok["content"]["application/zip"]["schema"] == {"type": "string", "format": "binary"}
    assert {"ETag", "X-Content-SHA256", "Content-Disposition", "Cache-Control"} <= set(
        ok["headers"]
    )
    assert "304" in operation["responses"]


def test_the_download_documents_its_three_cache_policies(spec):
    operation = operations(spec)[("GET", f"{VERSION_BASE}/download")]

    header = operation["responses"]["200"]["headers"]["Cache-Control"]["description"]

    for policy in ("public, immutable", "public, no-cache", "private, no-store"):
        assert policy in header, policy
        assert policy in operation["description"], policy
    assert "latest" in header and "latest" in operation["description"]


def test_the_raw_file_documents_its_hardening_headers(spec):
    operation = operations(spec)[("GET", f"{VERSION_BASE}/raw/{{path}}")]

    ok = operation["responses"]["200"]
    assert {"X-Content-Type-Options", "Content-Security-Policy"} <= set(ok["headers"])
    assert ok["headers"]["Content-Length"]["schema"] == {"type": "integer"}
    assert set(ok["content"]) == {"text/plain", "application/octet-stream"}


def test_path_parameters_are_described(spec):
    for key, operation in operations(spec).items():
        for parameter in operation.get("parameters", []):
            if parameter["in"] == "path":
                assert parameter["description"], (key, parameter["name"])
                assert parameter["required"] is True


def test_the_public_site_is_not_part_of_the_api_prefix(spec):
    assert all(path.startswith(PREFIX) or not path.startswith("/api") for path in spec["paths"])


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
async def test_the_interactive_documentation_answers(api: Api, path: str):
    response = await api.get(path)

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_the_served_specification_is_the_generated_one(api: Api, spec):
    response = await api.get("/openapi.json")

    assert response.json() == spec
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_the_documentation_is_public_even_when_reading_is_private(
    api: Api, monkeypatch: pytest.MonkeyPatch
):
    from app.config.settings import settings

    monkeypatch.setattr(settings.registry, "public_read", False)

    docs = await api.get("/docs")
    schema = await api.get("/openapi.json")

    assert (docs.status_code, schema.status_code) == (200, 200)
