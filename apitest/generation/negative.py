"""Negative and boundary test cases.

Each case starts from one schema-valid request for the endpoint and changes exactly one
thing. Whether the API should accept the result is decided by validating the mutated
request against the spec (not by the mutation's name), so e.g. `null` on a nullable field
or `quantity=10000` at an inclusive maximum are expected to succeed.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from apitest.generation.cases import CaseFactory, RequestParts
from apitest.generation.merge import merge_all_of
from apitest.generation.values import GenerationError, SchemaGenerator, infer_types
from apitest.models import Endpoint, Expectation, Parameter, Strategy, TestCase

WRONG_TYPE_CANDIDATES: tuple[Any, ...] = ("not-a-number", 12345, 3.14, True, ["x"], {"x": 1})
MASS_ASSIGNMENT_FIELD = ("is_admin", True)
MAX_NESTING = 1


@dataclass(frozen=True)
class Field:
    path: tuple[str, ...]
    schema: dict[str, Any]
    required: bool

    @property
    def label(self) -> str:
        return ".".join(self.path)


@dataclass(frozen=True)
class Mutation:
    technique: str
    description: str
    parts: RequestParts


# --------------------------------------------------------------------------- helpers


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def set_path(body: Any, path: tuple[str, ...], value: Any) -> Any:
    out = copy.deepcopy(body)
    node = out
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    return out


def delete_path(body: Any, path: tuple[str, ...]) -> Any:
    out = copy.deepcopy(body)
    node = out
    for key in path[:-1]:
        node = node[key]
    node.pop(path[-1], None)
    return out


def _flatten(schema: Any, generator: SchemaGenerator) -> dict[str, Any]:
    schema = generator.resolve(schema)
    if not isinstance(schema, dict):
        return {}
    if "allOf" in schema:
        return merge_all_of(schema, generator.resolve)
    return dict(schema)


def body_fields(
    schema: Any, body: Any, generator: SchemaGenerator, prefix: tuple[str, ...] = ()
) -> list[Field]:
    """Properties of an object body (and of nested objects present in `body`)."""
    flat = _flatten(schema, generator)
    required = set(flat.get("required", []))
    fields: list[Field] = []
    for name, sub in (flat.get("properties") or {}).items():
        sub_flat = _flatten(sub, generator)
        fields.append(Field((*prefix, name), sub_flat, name in required))
        nested = body.get(name) if isinstance(body, dict) else None
        if isinstance(nested, dict) and len(prefix) < MAX_NESTING:
            fields += body_fields(sub_flat, nested, generator, (*prefix, name))
    return fields


def wrong_type_value(schema: dict[str, Any], generator: SchemaGenerator) -> Any:
    allowed = set(infer_types(schema))
    if "number" in allowed:
        allowed.add("integer")
    for candidate in WRONG_TYPE_CANDIDATES:
        if json_type(candidate) in allowed:
            continue
        if not generator.registry.is_valid(schema, candidate):
            return candidate
    return None


def _string_of(length: int, schema: dict[str, Any], generator: SchemaGenerator) -> str:
    """A string of exactly `length`, satisfying the rest of the schema when possible."""
    try:
        exact = {**schema, "minLength": length, "maxLength": length}
        return str(generator.generate_valid(exact))
    except GenerationError:
        return "a" * length


# --------------------------------------------------------------------------- expectations


def request_is_valid(endpoint: Endpoint, parts: RequestParts, generator: SchemaGenerator) -> bool:
    registry = generator.registry
    body_spec = endpoint.request_body
    if parts.raw_body is not None and body_spec is not None and body_spec.json_schema is not None:
        return False
    if (
        body_spec is not None
        and body_spec.required
        and not parts.has_body
        and parts.raw_body is None
    ):
        return False
    body_schema = body_spec.json_schema if body_spec is not None else None
    if (
        parts.has_body
        and body_schema is not None
        and not registry.is_valid(body_schema, parts.body)
    ):
        return False
    for param in endpoint.parameters:
        value = _param_value(param, parts)
        if value is _MISSING:
            if param.required:
                return False
        elif not registry.is_valid(param.json_schema, value):
            return False
    return True


_MISSING = object()


def _param_value(param: Parameter, parts: RequestParts) -> Any:
    source: dict[str, Any] = {
        "path": parts.path_params,
        "query": parts.query,
        "header": parts.headers,
        "cookie": parts.cookies,
    }[param.location]
    return source.get(param.name, _MISSING)


def _set_param(parts: RequestParts, param: Parameter, value: Any) -> RequestParts:
    out = parts.copy()
    target: dict[str, Any] = {
        "path": out.path_params,
        "query": out.query,
        "header": out.headers,
        "cookie": out.cookies,
    }[param.location]
    if value is _MISSING:
        target.pop(param.name, None)
    else:
        target[param.name] = str(value) if param.location in ("header", "cookie") else value
    return out


def _with_body(parts: RequestParts, body: Any) -> RequestParts:
    out = parts.copy()
    out.body = body
    out.has_body = True
    out.raw_body = None
    return out


# --------------------------------------------------------------------------- negative


def _negative_body(
    endpoint: Endpoint, base: RequestParts, generator: SchemaGenerator
) -> Iterator[Mutation]:
    spec = endpoint.request_body
    if spec is None or spec.json_schema is None:
        return
    if spec.required:
        missing = base.copy()
        missing.has_body, missing.body = False, None
        yield Mutation("missing_body", "required request body omitted", missing)
    if not isinstance(base.body, dict):
        return
    malformed = base.copy()
    malformed.raw_body, malformed.has_body = '{"truncated": ', False
    yield Mutation("malformed_json", "syntactically invalid JSON body", malformed)
    yield Mutation("body_not_object", "array sent instead of an object", _with_body(base, []))
    schema = _flatten(spec.json_schema, generator)
    if schema.get("additionalProperties") is False:
        name, value = MASS_ASSIGNMENT_FIELD
        body = {**base.body, name: value}
        yield Mutation(
            f"unknown_field:{name}", "undeclared field (mass assignment)", _with_body(base, body)
        )
    for field in body_fields(spec.json_schema, base.body, generator):
        yield from _negative_field(field, base, generator)


def _negative_field(
    field: Field, base: RequestParts, generator: SchemaGenerator
) -> Iterator[Mutation]:
    label, schema = field.label, field.schema
    if field.required:
        yield Mutation(
            f"missing_required:{label}",
            f"required field {label} removed",
            _with_body(base, delete_path(base.body, field.path)),
        )
    wrong = wrong_type_value(schema, generator)
    if wrong is not None:
        yield Mutation(
            f"wrong_type:{label}",
            f"{label} set to {json_type(wrong)} {wrong!r}",
            _with_body(base, set_path(base.body, field.path, wrong)),
        )
    yield Mutation(
        f"null:{label}",
        f"{label} set to null",
        _with_body(base, set_path(base.body, field.path, None)),
    )
    if "enum" in schema:
        invalid = "__not_in_enum__" if "string" in infer_types(schema) else 987654321
        yield Mutation(
            f"invalid_enum:{label}",
            f"{label} outside its enum",
            _with_body(base, set_path(base.body, field.path, invalid)),
        )
    if "pattern" in schema:
        yield Mutation(
            f"pattern_violation:{label}",
            f"{label} does not match {schema['pattern']}",
            _with_body(base, set_path(base.body, field.path, "!! no match !!")),
        )
    if isinstance(schema.get("format"), str):
        yield Mutation(
            f"format_violation:{label}",
            f"{label} is not a valid {schema['format']}",
            _with_body(base, set_path(base.body, field.path, f"not-a-{schema['format']}")),
        )


def _negative_params(endpoint: Endpoint, base: RequestParts) -> Iterator[Mutation]:
    for param in endpoint.parameters:
        label = f"{param.location}.{param.name}"
        if param.required and param.location != "path":
            yield Mutation(
                f"missing_required_param:{label}",
                f"required parameter {label} omitted",
                _set_param(base, param, _MISSING),
            )
        types = set(infer_types(param.json_schema))
        if types & {"integer", "number", "boolean"} and "string" not in types:
            yield Mutation(
                f"wrong_type_param:{label}",
                f"{label} set to a non-numeric string",
                _set_param(base, param, "abc"),
            )
        if "enum" in param.json_schema and "string" in types:
            yield Mutation(
                f"invalid_enum_param:{label}",
                f"{label} outside its enum",
                _set_param(base, param, "__not_in_enum__"),
            )


# --------------------------------------------------------------------------- boundary


def numeric_boundaries(schema: dict[str, Any]) -> list[tuple[str, float | int]]:
    is_int = "integer" in infer_types(schema) and "number" not in infer_types(schema)
    step: float | int = 1 if is_int else 0.01
    points: list[tuple[str, float | int]] = []
    if "minimum" in schema:
        points += [("min", schema["minimum"]), ("min-1", schema["minimum"] - step)]
    if "exclusiveMinimum" in schema:
        low = schema["exclusiveMinimum"]
        points += [("exclusive_min", low), ("exclusive_min+1", low + step)]
    if "maximum" in schema:
        points += [("max", schema["maximum"]), ("max+1", schema["maximum"] + step)]
    if "exclusiveMaximum" in schema:
        high = schema["exclusiveMaximum"]
        points += [("exclusive_max", high), ("exclusive_max-1", high - step)]
    if not points:
        points = [("zero", 0), ("negative", -1)]
    if not is_int:
        points = [(name, round(value, 6)) for name, value in points if math.isfinite(value)]
    return points


def string_boundaries(schema: dict[str, Any], generator: SchemaGenerator) -> list[tuple[str, str]]:
    points: list[tuple[str, str]] = []
    min_len = int(schema.get("minLength", 0))
    if min_len > 0:
        points += [("min_length", _string_of(min_len, schema, generator))]
        points += [("min_length-1", "a" * (min_len - 1))]
    else:
        points += [("empty", "")]
    if "maxLength" in schema:
        max_len = int(schema["maxLength"])
        points += [("max_length", _string_of(max_len, schema, generator))]
        points += [("max_length+1", _string_of(max_len, schema, generator) + "a")]
    return points


def array_boundaries(
    schema: dict[str, Any], generator: SchemaGenerator
) -> list[tuple[str, list[Any]]]:
    try:
        item = generator.generate_valid(schema.get("items", {}))
    except GenerationError:
        return []
    points: list[tuple[str, list[Any]]] = [("empty_array", [])]
    if "minItems" in schema:
        size = int(schema["minItems"])
        points += [("min_items", [item] * size)]
        if size > 0:
            points += [("min_items-1", [item] * (size - 1))]
    if "maxItems" in schema:
        size = int(schema["maxItems"])
        points += [("max_items", [item] * size), ("max_items+1", [item] * (size + 1))]
    return points


def boundary_values(schema: dict[str, Any], generator: SchemaGenerator) -> list[tuple[str, Any]]:
    types = set(infer_types(schema))
    if "enum" in schema or "const" in schema:
        return []
    if types & {"integer", "number"}:
        return list(numeric_boundaries(schema))
    if "string" in types and "format" not in schema:
        return list(string_boundaries(schema, generator))
    if "array" in types:
        return list(array_boundaries(schema, generator))
    return []


def _boundary_mutations(
    endpoint: Endpoint, base: RequestParts, generator: SchemaGenerator
) -> Iterator[Mutation]:
    for param in endpoint.parameters:
        for name, value in boundary_values(param.json_schema, generator):
            label = f"{param.location}.{param.name}"
            yield Mutation(
                f"boundary:{label}={name}",
                f"{label} = {_short(value)} ({name})",
                _set_param(base, param, value),
            )
    spec = endpoint.request_body
    if spec is None or spec.json_schema is None or not isinstance(base.body, dict):
        return
    for field in body_fields(spec.json_schema, base.body, generator):
        for name, value in boundary_values(field.schema, generator):
            yield Mutation(
                f"boundary:{field.label}={name}",
                f"{field.label} = {_short(value)} ({name})",
                _with_body(base, set_path(base.body, field.path, value)),
            )


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 40 else f"{text[:30]}… (len {len(value)})"


# --------------------------------------------------------------------------- entry points


MutationSource = Callable[[Endpoint, RequestParts, SchemaGenerator], Iterable[Mutation]]


def _build(
    factory: CaseFactory, endpoint: Endpoint, strategy: Strategy, mutations_of: MutationSource
) -> list[TestCase]:
    generator = factory.generator(factory.rng_for(strategy, endpoint))
    try:
        base = factory.valid_parts(endpoint, generator, pool_probability=1.0)
    except GenerationError as exc:
        factory.skipped.append(f"{endpoint.key} ({strategy.value}): {exc}")
        return []
    cases = []
    seen: set[str] = set()
    for mutation in mutations_of(endpoint, base, generator):
        if mutation.technique in seen:
            continue
        seen.add(mutation.technique)
        valid = request_is_valid(endpoint, mutation.parts, generator)
        cases.append(
            factory.make_case(
                endpoint,
                strategy,
                mutation.technique,
                mutation.parts,
                Expectation.SUCCESS if valid else Expectation.CLIENT_ERROR,
                description=mutation.description,
            )
        )
    return cases


def negative_cases(factory: CaseFactory, endpoint: Endpoint) -> list[TestCase]:
    def mutations(
        endpoint: Endpoint, base: RequestParts, generator: SchemaGenerator
    ) -> Iterator[Mutation]:
        yield from _negative_params(endpoint, base)
        yield from _negative_body(endpoint, base, generator)

    return _build(factory, endpoint, Strategy.NEGATIVE, mutations)


def boundary_cases(factory: CaseFactory, endpoint: Endpoint) -> list[TestCase]:
    return _build(factory, endpoint, Strategy.BOUNDARY, _boundary_mutations)
