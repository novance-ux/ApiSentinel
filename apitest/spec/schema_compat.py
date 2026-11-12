"""Convert OpenAPI schema objects into JSON Schema 2020-12.

Handles the OpenAPI 3.0 dialect (``nullable``, boolean ``exclusiveMinimum``/``Maximum``,
OpenAPI-only keywords) and the request/response views of ``readOnly``/``writeOnly``.
OpenAPI 3.1 schemas pass through unchanged apart from dropping OpenAPI-only keywords.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from apitest.generation.regex import to_python_pattern

Direction = Literal["request", "response"]

MAP_KEYWORDS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
LIST_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")
SINGLE_KEYWORDS = (
    "items",
    "additionalProperties",
    "not",
    "contains",
    "propertyNames",
    "if",
    "then",
    "else",
    "additionalItems",
    "unevaluatedItems",
    "unevaluatedProperties",
)
_OPENAPI_ONLY = frozenset({"nullable", "discriminator", "xml", "externalDocs", "example"})
_COMPOSITION = ("allOf", "anyOf", "oneOf", "$ref")


def map_subschemas(schema: dict[str, Any], fn: Callable[[Any], Any]) -> dict[str, Any]:
    """Shallow copy of `schema` with `fn` applied to every direct subschema."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in MAP_KEYWORDS and isinstance(value, dict):
            out[key] = {name: fn(sub) for name, sub in value.items()}
        elif key in LIST_KEYWORDS and isinstance(value, list):
            out[key] = [fn(sub) for sub in value]
        elif key in SINGLE_KEYWORDS and isinstance(value, list):
            out[key] = [fn(sub) for sub in value]  # draft-4 tuple `items`
        elif key in SINGLE_KEYWORDS:
            out[key] = fn(value)
        else:
            out[key] = value
    return out


def to_json_schema(schema: Any) -> Any:
    """Recursively convert an OpenAPI schema object to JSON Schema 2020-12."""
    if not isinstance(schema, dict):
        return schema
    out = {
        k: v for k, v in map_subschemas(schema, to_json_schema).items() if k not in _OPENAPI_ONLY
    }
    for bound, exclusive in (("minimum", "exclusiveMinimum"), ("maximum", "exclusiveMaximum")):
        flag = out.get(exclusive)
        if isinstance(flag, bool):
            del out[exclusive]
            if flag and bound in out:
                out[exclusive] = out.pop(bound)
    if isinstance(out.get("pattern"), str):
        out["pattern"] = to_python_pattern(out["pattern"])
    if isinstance(out.get("items"), list):
        out["prefixItems"] = out.pop("items")
    if schema.get("nullable") is True:
        return _make_nullable(out)
    return out


def _make_nullable(schema: dict[str, Any]) -> dict[str, Any]:
    if any(key in schema for key in _COMPOSITION) or "type" not in schema:
        return {"anyOf": [schema, {"type": "null"}]}
    types = schema["type"]
    types = [types] if isinstance(types, str) else list(types)
    out = {**schema, "type": [*types, "null"] if "null" not in types else types}
    if "enum" in out and None not in out["enum"]:
        out["enum"] = [*out["enum"], None]
    return out


def for_direction(schema: Any, direction: Direction) -> Any:
    """Request view drops readOnly properties; response view stops requiring writeOnly ones."""
    if not isinstance(schema, dict):
        return schema
    out = map_subschemas(schema, lambda sub: for_direction(sub, direction))
    hidden_flag = "readOnly" if direction == "request" else "writeOnly"
    properties = out.get("properties")
    if not isinstance(properties, dict):
        return out
    hidden = {
        name for name, sub in properties.items() if isinstance(sub, dict) and sub.get(hidden_flag)
    }
    if not hidden:
        return out
    if direction == "request":
        out["properties"] = {k: v for k, v in properties.items() if k not in hidden}
    if isinstance(out.get("required"), list):
        out["required"] = [name for name in out["required"] if name not in hidden]
    return out
