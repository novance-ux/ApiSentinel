"""Load an OpenAPI 3.x spec from a file or URL into a normalized ApiSpec."""

from __future__ import annotations

from typing import Any

from openapi_spec_validator import validate as validate_openapi

from apitest.models import ApiSpec
from apitest.spec.documents import fetch_uri, source_to_uri
from apitest.spec.endpoints import build_endpoints, build_security_schemes
from apitest.spec.errors import SpecError
from apitest.spec.resolver import RefResolver
from apitest.spec.schema_compat import to_json_schema


def _check_version(document: Any, source: str) -> str:
    if not isinstance(document, dict):
        raise SpecError(f"{source} is not an OpenAPI document (top level is not a mapping)")
    if "swagger" in document:
        raise SpecError("Swagger 2.0 is not supported; convert the spec to OpenAPI 3.x first")
    version = str(document.get("openapi", ""))
    if not version.startswith("3."):
        raise SpecError(f"expected an OpenAPI 3.x document, got openapi={version!r}")
    return version


def _validation_warnings(document: dict[str, Any], base_uri: str) -> list[str]:
    try:
        validate_openapi(document, base_uri=base_uri)
    except Exception as exc:  # the validator raises several unrelated exception types
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
        return [f"spec failed OpenAPI validation: {first_line}"]
    return []


def load_spec(source: str, *, validate: bool = True, strict: bool = False) -> ApiSpec:
    """Load, validate, and normalize a spec.

    Validation problems are reported as warnings on the result unless `strict` is set, in
    which case they raise SpecError. Unresolvable refs always raise.
    """
    base_uri = source_to_uri(source)
    document = fetch_uri(base_uri)
    version = _check_version(document, source)
    warnings = _validation_warnings(document, base_uri) if validate else []
    if strict and warnings:
        raise SpecError(warnings[0])

    resolver = RefResolver(document, base_uri, fetch_uri)
    endpoints = build_endpoints(document, resolver)
    info = document.get("info") or {}
    servers = [
        s["url"] for s in document.get("servers") or [] if isinstance(s, dict) and "url" in s
    ]
    return ApiSpec(
        title=str(info.get("title", "")),
        version=str(info.get("version", "")),
        openapi_version=version,
        source=source,
        servers=servers,
        endpoints=endpoints,
        security_schemes=build_security_schemes(document, resolver),
        schema_resources=resolver.schema_resources(to_json_schema),
        warnings=warnings,
    )
