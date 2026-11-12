"""Build normalized Endpoint models from a resolved OpenAPI document."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from apitest.models import (
    Endpoint,
    HttpMethod,
    Link,
    Parameter,
    ParameterLocation,
    RequestBody,
    ResponseSpec,
    SecurityScheme,
)
from apitest.spec.resolver import RefResolver
from apitest.spec.schema_compat import Direction, for_direction, to_json_schema

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
LOCATIONS = ("path", "query", "header", "cookie")
# Header parameters with these names are ignored by the OpenAPI spec itself.
IGNORED_HEADERS = frozenset({"accept", "content-type", "authorization"})


def is_json_media_type(media_type: str) -> bool:
    base = media_type.split(";")[0].strip().lower()
    return base == "application/json" or base.endswith("+json") or base == "*/*"


def pick_media_type(content: dict[str, Any]) -> str:
    """Prefer JSON, then form encodings, then whatever the spec lists first."""
    types = list(content)
    predicates: tuple[Callable[[str], bool], ...] = (
        lambda t: t.split(";")[0].strip().lower() == "application/json",
        lambda t: is_json_media_type(t) and t != "*/*",
        lambda t: t.startswith("application/x-www-form-urlencoded"),
        lambda t: t.startswith("multipart/form-data"),
    )
    for predicate in predicates:
        match = next((t for t in types if predicate(t)), None)
        if match is not None:
            return match
    return types[0]


def _schema(raw: Any, direction: Direction) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return cast(dict[str, Any], for_direction(to_json_schema(raw), direction))


def _media_schema(content: Any, media_type: str, direction: Direction) -> dict[str, Any] | None:
    media = content.get(media_type) if isinstance(content, dict) else None
    if not isinstance(media, dict) or "schema" not in media:
        return None
    return _schema(media["schema"], direction)


def _build_parameter(raw: dict[str, Any]) -> Parameter | None:
    location = raw.get("in")
    name = raw.get("name")
    if location not in LOCATIONS or not isinstance(name, str):
        return None
    if location == "header" and name.lower() in IGNORED_HEADERS:
        return None
    schema = raw.get("schema")
    if schema is None and isinstance(raw.get("content"), dict) and raw["content"]:
        media = next(iter(raw["content"].values()))
        schema = media.get("schema") if isinstance(media, dict) else None
    return Parameter(
        name=name,
        location=cast(ParameterLocation, location),
        required=bool(raw.get("required")) or location == "path",
        json_schema=_schema(schema, "request"),
        description=raw.get("description"),
    )


def _merge_parameters(shared: list[Any], own: list[Any]) -> list[Parameter]:
    merged: dict[tuple[str, str], Parameter] = {}
    for raw in [*shared, *own]:
        if isinstance(raw, dict) and (param := _build_parameter(raw)) is not None:
            merged[(param.name, param.location)] = param
    return list(merged.values())


def _build_request_body(raw: Any) -> RequestBody | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("content"), dict) or not raw["content"]:
        return None
    content = raw["content"]
    media_type = pick_media_type(content)
    return RequestBody(
        required=bool(raw.get("required")),
        content_type=media_type,
        content_types=list(content),
        json_schema=_media_schema(content, media_type, "request"),
    )


def _build_links(raw: Any) -> list[Link]:
    if not isinstance(raw, dict):
        return []
    return [
        Link(
            name=name,
            operation_id=link.get("operationId"),
            operation_ref=link.get("operationRef"),
            parameters=link.get("parameters") or {},
        )
        for name, link in raw.items()
        if isinstance(link, dict)
    ]


def _build_response(status: str, raw: Any) -> ResponseSpec:
    raw = raw if isinstance(raw, dict) else {}
    content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    json_type = next((t for t in content if is_json_media_type(t)), None)
    return ResponseSpec(
        status=status,
        description=raw.get("description") or "",
        content_types=list(content),
        json_schema=_media_schema(content, json_type, "response") if json_type else None,
        links=_build_links(raw.get("links")),
    )


def _build_endpoint(
    path: str,
    method: str,
    operation: dict[str, Any],
    shared_params: list[Any],
    global_security: list[dict[str, list[str]]],
) -> Endpoint:
    security = operation.get("security", global_security)
    responses = operation.get("responses") or {}
    return Endpoint(
        method=cast(HttpMethod, method.upper()),
        path=path,
        operation_id=operation.get("operationId"),
        summary=operation.get("summary"),
        description=operation.get("description"),
        tags=list(operation.get("tags") or []),
        parameters=_merge_parameters(shared_params, operation.get("parameters") or []),
        request_body=_build_request_body(operation.get("requestBody")),
        responses={str(code): _build_response(str(code), raw) for code, raw in responses.items()},
        security=list(security or []),
        deprecated=bool(operation.get("deprecated")),
    )


def build_endpoints(document: dict[str, Any], resolver: RefResolver) -> list[Endpoint]:
    global_security = document.get("security") or []
    endpoints: list[Endpoint] = []
    for path, raw_item in (document.get("paths") or {}).items():
        item = resolver.resolve(raw_item)
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in METHODS:
            operation = item.get(method)
            if isinstance(operation, dict):
                endpoints.append(_build_endpoint(path, method, operation, shared, global_security))
    return endpoints


def build_security_schemes(document: dict[str, Any], resolver: RefResolver) -> dict[str, Any]:
    components = document.get("components") or {}
    raw_schemes = resolver.resolve(components.get("securitySchemes") or {})
    return {
        name: SecurityScheme(
            name=name,
            type=str(raw.get("type", "")),
            location=raw.get("in"),
            parameter_name=raw.get("name"),
            scheme=str(raw["scheme"]).lower() if raw.get("scheme") else None,
        )
        for name, raw in raw_schemes.items()
        if isinstance(raw, dict)
    }
