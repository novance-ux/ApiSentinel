from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from apitest.models import ApiSpec
from apitest.spec.errors import SpecError
from apitest.spec.loader import load_spec
from apitest.spec.registry import SchemaRegistry
from apitest.spec.resolver import URN_PREFIX
from apitest.spec.schema_compat import for_direction, to_json_schema
from tests.conftest import SPECS


def _contains_ref(node: Any, prefix: str = "#") -> bool:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith(URN_PREFIX):
            return True
        return any(_contains_ref(v, prefix) for v in node.values())
    if isinstance(node, list):
        return any(_contains_ref(v, prefix) for v in node)
    return False


# --------------------------------------------------------------------------- buggy api (allOf)


def test_buggy_api_endpoints(buggy_spec: ApiSpec) -> None:
    assert buggy_spec.title == "Buggy Inventory API"
    assert buggy_spec.warnings == []
    assert {e.key for e in buggy_spec.endpoints} == {
        "GET /health",
        "GET /items",
        "POST /items",
        "GET /items/{item_id}",
        "PUT /items/{item_id}",
        "DELETE /items/{item_id}",
        "POST /users",
        "GET /users/{user_id}",
        "GET /search",
        "POST /orders",
        "GET /orders/{order_id}",
    }


def test_buggy_api_everything_is_resolved(buggy_spec: ApiSpec) -> None:
    dumped = [e.model_dump() for e in buggy_spec.endpoints]
    assert not _contains_ref(dumped)
    assert buggy_spec.schema_resources == {}


def test_parameters_and_security(buggy_spec: ApiSpec) -> None:
    list_items = buggy_spec.endpoint("GET /items")
    assert [(p.name, p.location, p.required) for p in list_items.parameters] == [
        ("page", "query", False),
        ("page_size", "query", False),
    ]
    assert list_items.parameters[1].json_schema["maximum"] == 100
    assert list_items.security == [{"ApiKeyAuth": []}]
    assert list_items.requires_auth
    assert not buggy_spec.endpoint("GET /health").requires_auth
    path_param = buggy_spec.endpoint("DELETE /items/{item_id}").parameters[0]
    assert (path_param.name, path_param.location, path_param.required) == ("item_id", "path", True)
    scheme = buggy_spec.security_schemes["ApiKeyAuth"]
    assert (scheme.type, scheme.location, scheme.parameter_name) == (
        "apiKey",
        "header",
        "X-API-Key",
    )


def test_request_body_allof_and_read_only(buggy_spec: ApiSpec) -> None:
    body = buggy_spec.endpoint("POST /items").request_body
    assert body is not None and body.required and body.content_type == "application/json"
    schema = body.json_schema
    assert schema is not None and "allOf" in schema
    base = schema["allOf"][0]
    assert base["properties"]["description"]["type"] == ["string", "null"]
    assert base["properties"]["category"]["enum"] == ["books", "electronics", "toys"]


def test_response_view_keeps_read_only_required(buggy_spec: ApiSpec) -> None:
    created = buggy_spec.endpoint("POST /items").responses["201"]
    assert created.json_schema is not None
    assert "id" in created.json_schema["allOf"][1]["required"]
    assert [link.operation_id for link in created.links] == ["getItem", "deleteItem"]
    assert created.links[0].parameters == {"item_id": "$response.body#/id"}


def test_response_lookup(buggy_spec: ApiSpec) -> None:
    endpoint = buggy_spec.endpoint("GET /items/{item_id}")
    assert endpoint.response_for(404) is not None
    assert endpoint.response_for(418) is None
    assert endpoint.success_statuses() == [200]


# --------------------------------------------------------------------------- petstore (oneOf)


def test_petstore_yaml_quirks(petstore_spec: ApiSpec) -> None:
    list_pets = petstore_spec.endpoint("GET /pets")
    assert set(list_pets.responses) == {"200", "default"}  # unquoted 200 → "200"
    species = next(p for p in list_pets.parameters if p.name == "species")
    assert species.json_schema["enum"] == ["dog", "cat", "yes", "no"]  # not booleans
    assert [p.name for p in list_pets.parameters] == ["limit", "species", "X-Request-ID"]
    limit = list_pets.parameters[0].json_schema
    assert limit["exclusiveMinimum"] == 0 and "minimum" not in limit


def test_petstore_oneof_discriminator_and_nullable(petstore_spec: ApiSpec) -> None:
    body = petstore_spec.endpoint("POST /pets").request_body
    assert body is not None and body.content_type == "application/json"
    assert body.content_types == ["application/x-www-form-urlencoded", "application/json"]
    assert body.json_schema is not None
    props = body.json_schema["properties"]
    assert props["birthday"]["format"] == "date"
    assert "example" not in props["birthday"]
    assert props["nickname"]["type"] == ["string", "null"]
    kind = props["kind"]
    assert "discriminator" not in kind
    assert [branch["properties"]["type"]["enum"] for branch in kind["oneOf"]] == [["dog"], ["cat"]]


def test_petstore_path_level_parameter_override(petstore_spec: ApiSpec) -> None:
    get_pet = petstore_spec.endpoint("GET /pets/{petId}")
    pet_id = next(p for p in get_pet.parameters if p.name == "petId")
    assert pet_id.json_schema["type"] == "integer"
    assert {p.name for p in get_pet.parameters} == {"petId", "session"}
    assert get_pet.response_for(404) is get_pet.responses["4XX"]
    assert petstore_spec.endpoint("DELETE /pets/{petId}").security == []
    assert petstore_spec.security_schemes["bearerAuth"].scheme == "bearer"


def test_petstore_response_json_variant(petstore_spec: ApiSpec) -> None:
    problem = petstore_spec.endpoint("GET /pets").responses["default"]
    assert problem.content_types == ["application/problem+json"]
    assert problem.json_schema is not None and problem.json_schema["type"] == "object"


# --------------------------------------------------------------------------- library (3.1, cycles)


def test_library_circular_refs_become_urns(library_spec: ApiSpec) -> None:
    categories = library_spec.endpoint("GET /categories").responses["200"].json_schema
    assert categories is not None
    child_items = categories["items"]["properties"]["children"]["items"]
    assert child_items["$ref"].startswith(URN_PREFIX)
    assert library_spec.schema_resources


def test_library_recursive_validation(library_spec: ApiSpec) -> None:
    schema = library_spec.endpoint("GET /categories").responses["200"].json_schema
    registry = SchemaRegistry(library_spec.schema_resources)
    tree = [{"name": "root", "children": [{"name": "a", "children": [{"name": "b"}]}]}]
    assert registry.is_valid(schema, tree)
    broken = [{"name": "root", "children": [{"children": [{"name": 3}]}]}]
    assert not registry.is_valid(schema, broken)
    target = registry.lookup(child_items_ref(schema))
    assert target["required"] == ["name"]


def child_items_ref(schema: Any) -> str:
    ref: str = schema["items"]["properties"]["children"]["items"]["$ref"]
    return ref


def test_library_mutual_recursion_book_author(library_spec: ApiSpec) -> None:
    book = library_spec.endpoint("POST /books").request_body
    assert book is not None and book.json_schema is not None
    registry = SchemaRegistry(library_spec.schema_resources)
    value = {
        "isbn": "978-0123456789",
        "title": "T",
        "author": {"name": "A", "books": [{"isbn": "979-0123456789", "title": "U"}]},
    }
    assert registry.is_valid(book.json_schema, value)
    value["author"]["books"][0]["isbn"] = "bad"  # type: ignore[index]
    assert not registry.is_valid(book.json_schema, value)


def test_library_external_file_ref(library_spec: ApiSpec) -> None:
    error = library_spec.endpoint("POST /books").responses["400"].json_schema
    assert error == {
        "type": "object",
        "required": ["code", "message"],
        "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
    }


def test_library_security_alternatives_and_write_only(library_spec: ApiSpec) -> None:
    create = library_spec.endpoint("POST /books")
    assert create.security == [{"basicAuth": []}, {"bearerAuth": []}]
    register = library_spec.endpoint("POST /members")
    assert not register.requires_auth
    assert register.request_body is not None and register.request_body.json_schema is not None
    assert register.request_body.json_schema["properties"]["plan"] == {"const": "free"}
    links = create.responses["201"].links
    assert links[0].parameters == {"isbn": "$response.body#/isbn"}


# --------------------------------------------------------------------------- sources & errors


@respx.mock
def test_load_from_url_with_relative_external_ref() -> None:
    base = "https://specs.example.test/v1/"
    respx.get(base + "library.json").mock(
        return_value=httpx.Response(200, text=(SPECS / "library.json").read_text("utf-8"))
    )
    respx.get(base + "common.yaml").mock(
        return_value=httpx.Response(200, text=(SPECS / "common.yaml").read_text("utf-8"))
    )
    spec = load_spec(base + "library.json", validate=False)
    assert len(spec.endpoints) == 4
    assert spec.endpoint("GET /books/{isbn}").responses["404"].json_schema is not None


@respx.mock
def test_load_from_url_http_error() -> None:
    respx.get("https://specs.example.test/missing.yaml").mock(return_value=httpx.Response(404))
    with pytest.raises(SpecError, match="cannot fetch"):
        load_spec("https://specs.example.test/missing.yaml")


def test_rejects_swagger_2(tmp_path: Path) -> None:
    path = tmp_path / "swagger.json"
    path.write_text(json.dumps({"swagger": "2.0", "info": {}, "paths": {}}))
    with pytest.raises(SpecError, match=r"Swagger 2\.0"):
        load_spec(str(path))


def test_missing_file() -> None:
    with pytest.raises(SpecError, match="not found"):
        load_spec("does-not-exist.yaml")


def test_unresolvable_ref(tmp_path: Path) -> None:
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "x", "version": "1"},
        "paths": {"/a": {"get": {"responses": {"200": {"$ref": "#/components/responses/Nope"}}}}},
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(SpecError, match="unresolvable"):
        load_spec(str(path), validate=False)


def test_invalid_spec_warns_or_fails_in_strict_mode(tmp_path: Path) -> None:
    doc = {"openapi": "3.0.3", "info": {"title": "x"}, "paths": {}}  # info.version missing
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(doc))
    assert load_spec(str(path)).warnings
    with pytest.raises(SpecError):
        load_spec(str(path), strict=True)


# --------------------------------------------------------------------------- schema conversion


def test_nullable_on_composed_schema_wraps_in_anyof() -> None:
    converted = to_json_schema({"allOf": [{"type": "object"}], "nullable": True})
    assert converted == {"anyOf": [{"allOf": [{"type": "object"}]}, {"type": "null"}]}


def test_nullable_enum_gets_null_member() -> None:
    converted = to_json_schema({"type": "string", "enum": ["a"], "nullable": True})
    assert converted == {"type": ["string", "null"], "enum": ["a", None]}


def test_for_direction_request_and_response() -> None:
    schema = {
        "type": "object",
        "required": ["id", "secret", "name"],
        "properties": {
            "id": {"type": "integer", "readOnly": True},
            "secret": {"type": "string", "writeOnly": True},
            "name": {"type": "string"},
        },
    }
    request = for_direction(schema, "request")
    assert request["required"] == ["secret", "name"] and "id" not in request["properties"]
    response = for_direction(schema, "response")
    assert response["required"] == ["id", "name"] and "secret" in response["properties"]
