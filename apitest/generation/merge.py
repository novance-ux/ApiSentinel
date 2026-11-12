"""Merge `allOf` subschemas into a single schema for generation.

The merge is an approximation of the intersection: it is exact for the keywords that
matter for generation (types, properties, required, bounds, enums) and keeps anything it
cannot combine in a residual `allOf`, which the generator enforces by validate-and-retry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_MAX_KEYS = ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties")
_MIN_KEYS = ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties")
_RESIDUAL_KEYS = ("oneOf", "anyOf", "not", "pattern", "contains", "if")


def _types(value: Any) -> set[str] | None:
    if value is None:
        return None
    return {value} if isinstance(value, str) else set(value)


def _merge_types(left: Any, right: Any) -> Any:
    a, b = _types(left), _types(right)
    if a is None or b is None:
        return left if b is None else right
    merged = a & b
    if ("number" in a and "integer" in b) or ("integer" in a and "number" in b):
        merged.add("integer")
    ordered = sorted(merged)
    return ordered[0] if len(ordered) == 1 else ordered


def _merge_into(out: dict[str, Any], sub: dict[str, Any], residual: list[Any]) -> None:
    for key, value in sub.items():
        if key not in out:
            out[key] = value
        elif key == "type":
            out[key] = _merge_types(out[key], value)
        elif key == "properties":
            props = dict(out[key])
            for name, schema in value.items():
                props[name] = {"allOf": [props[name], schema]} if name in props else schema
            out[key] = props
        elif key == "required":
            out[key] = list(dict.fromkeys([*out[key], *value]))
        elif key in _MAX_KEYS:
            out[key] = max(out[key], value)
        elif key in _MIN_KEYS:
            out[key] = min(out[key], value)
        elif key == "enum":
            out[key] = [item for item in out[key] if item in value]
        elif key in ("items", "additionalProperties") and isinstance(value, dict):
            out[key] = {"allOf": [out[key], value]} if isinstance(out[key], dict) else out[key]
        elif key in _RESIDUAL_KEYS and out[key] != value:
            residual.append({key: value})


def merge_all_of(schema: dict[str, Any], resolve: Callable[[Any], Any]) -> dict[str, Any]:
    """Flatten nested `allOf` (following refs via `resolve`) into one schema."""
    out = {key: value for key, value in schema.items() if key != "allOf"}
    residual: list[Any] = []
    for sub in schema.get("allOf", []):
        sub = resolve(sub)
        if not isinstance(sub, dict):
            continue
        if "allOf" in sub:
            sub = merge_all_of(sub, resolve)
        residual.extend(sub.pop("allOf", []) if "allOf" in sub else [])
        _merge_into(out, sub, residual)
    if residual:
        out["allOf"] = residual
    return out
