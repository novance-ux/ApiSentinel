"""Property tests: every generated value validates against the schema it came from."""

from __future__ import annotations

import random
import re
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apitest.generation.regex import RegexGenerator
from apitest.generation.values import GenerationError, SchemaGenerator
from apitest.models import ApiSpec
from apitest.spec.registry import SchemaRegistry
from apitest.spec.schema_compat import to_json_schema

FEATURE_SCHEMAS: dict[str, Any] = {
    "string": {"type": "string"},
    "string_bounds": {"type": "string", "minLength": 5, "maxLength": 7},
    "string_exact": {"type": "string", "minLength": 3, "maxLength": 3},
    "email": {"type": "string", "format": "email"},
    "uuid": {"type": "string", "format": "uuid"},
    "date": {"type": "string", "format": "date"},
    "date_time": {"type": "string", "format": "date-time"},
    "time": {"type": "string", "format": "time"},
    "uri": {"type": "string", "format": "uri"},
    "hostname": {"type": "string", "format": "hostname"},
    "ipv4": {"type": "string", "format": "ipv4"},
    "ipv6": {"type": "string", "format": "ipv6"},
    "byte": {"type": "string", "format": "byte"},
    "pattern_username": {"type": "string", "pattern": "^[a-z0-9_]{3,20}$"},
    "pattern_isbn": {"type": "string", "pattern": r"^97[89]-\d{10}$"},
    "pattern_phone": {"type": "string", "pattern": r"^\+?[1-9]\d{1,14}$"},
    "pattern_branch": {"type": "string", "pattern": "^(foo|bar)-[A-F0-9]{4}$"},
    "pattern_unanchored_min": {"type": "string", "pattern": "ab+c", "minLength": 10},
    "pattern_lookahead": {"type": "string", "pattern": "^(?!admin)[a-z]{3,8}$"},
    "pattern_negated_class": {"type": "string", "pattern": "^[^0-9 ]{4}$"},
    "pattern_backref": {"type": "string", "pattern": r"^(\w)x\1$"},
    "pattern_ecma_named_group": {"type": "string", "pattern": "^(?<year>[0-9]{4})-Q[1-4]$"},
    "integer": {"type": "integer"},
    "integer_bounds": {"type": "integer", "minimum": -5, "maximum": 5},
    "integer_exclusive": {"type": "integer", "exclusiveMinimum": 0, "exclusiveMaximum": 3},
    "integer_multiple": {"type": "integer", "multipleOf": 7, "minimum": 10, "maximum": 100},
    "integer_int32": {"type": "integer", "format": "int32"},
    "integer_max_only": {"type": "integer", "maximum": -10},
    "number": {"type": "number"},
    "number_bounds": {"type": "number", "minimum": 0.5, "maximum": 0.75},
    "number_exclusive": {"type": "number", "exclusiveMinimum": 1, "exclusiveMaximum": 1.001},
    "number_multiple": {"type": "number", "multipleOf": 0.25, "minimum": 1, "maximum": 3},
    "number_multiple_tenth": {"type": "number", "multipleOf": 0.1, "maximum": 2},
    "boolean": {"type": "boolean"},
    "null": {"type": "null"},
    "nullable": {"type": ["string", "null"], "minLength": 2},
    "enum": {"enum": ["red", "green", 3, None]},
    "const": {"const": {"a": [1, 2]}},
    "empty": {},
    "true_schema": True,
    "array": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 4},
    "array_unique": {
        "type": "array",
        "items": {"type": "integer", "minimum": 0, "maximum": 5},
        "minItems": 4,
        "uniqueItems": True,
    },
    "array_contains": {
        "type": "array",
        "items": {"type": "integer"},
        "contains": {"const": 42},
    },
    "array_prefix": {
        "type": "array",
        "prefixItems": [{"type": "string"}, {"type": "boolean"}],
        "items": False,
        "minItems": 2,
    },
    "nested_object": {
        "type": "object",
        "required": ["user", "tags"],
        "properties": {
            "user": {
                "type": "object",
                "required": ["name", "address"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "address": {
                        "type": "object",
                        "required": ["zip"],
                        "properties": {"zip": {"type": "string", "pattern": r"^\d{5}$"}},
                    },
                },
            },
            "tags": {"type": "array", "items": {"type": "string", "maxLength": 5}},
        },
        "additionalProperties": False,
    },
    "object_min_properties": {"type": "object", "minProperties": 3},
    "object_dependent_required": {
        "type": "object",
        "required": ["card"],
        "properties": {"card": {"type": "string"}, "cvv": {"type": "string"}},
        "dependentRequired": {"card": ["cvv"]},
    },
    "all_of": {
        "allOf": [
            {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}},
            {"required": ["b"], "properties": {"b": {"type": "string"}, "a": {"minimum": 10}}},
        ]
    },
    "all_of_number_integer": {"allOf": [{"type": "number"}, {"type": "integer", "maximum": 3}]},
    "one_of_overlapping": {
        "oneOf": [
            {"type": "integer", "multipleOf": 3},
            {"type": "integer", "multipleOf": 5},
        ]
    },
    "one_of_discriminated": {
        "type": "object",
        "required": ["pet"],
        "properties": {
            "pet": {
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["kind", "bark"],
                        "properties": {"kind": {"const": "dog"}, "bark": {"type": "boolean"}},
                    },
                    {
                        "type": "object",
                        "required": ["kind", "lives"],
                        "properties": {"kind": {"const": "cat"}, "lives": {"type": "integer"}},
                    },
                ]
            }
        },
    },
    "any_of": {"anyOf": [{"type": "string", "format": "email"}, {"type": "integer", "minimum": 5}]},
    "not": {"type": "integer", "minimum": 0, "maximum": 10, "not": {"enum": [1, 2, 3]}},
}


def _spec_schemas(*specs: ApiSpec) -> list[tuple[str, Any, SchemaRegistry]]:
    out: list[tuple[str, Any, SchemaRegistry]] = []
    for spec in specs:
        registry = SchemaRegistry(spec.schema_resources)
        for endpoint in spec.endpoints:
            for param in endpoint.parameters:
                out.append((f"{endpoint.key} param {param.name}", param.json_schema, registry))
            if endpoint.request_body and endpoint.request_body.json_schema is not None:
                out.append((f"{endpoint.key} body", endpoint.request_body.json_schema, registry))
            for status, response in endpoint.responses.items():
                if response.json_schema is not None:
                    out.append((f"{endpoint.key} {status}", response.json_schema, registry))
    return out


seeds = st.integers(min_value=0, max_value=2**32 - 1)


@pytest.mark.parametrize("name", sorted(FEATURE_SCHEMAS))
@settings(max_examples=40, deadline=None)
@given(seed=seeds)
def test_feature_schema_values_validate(name: str, seed: int) -> None:
    schema = to_json_schema(FEATURE_SCHEMAS[name])
    registry = SchemaRegistry()
    value = SchemaGenerator(random.Random(seed), registry).generate(schema)
    assert registry.is_valid(schema, value), (value, registry.errors(schema, value))


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(seed=seeds)
def test_example_spec_values_validate(
    buggy_spec: ApiSpec, petstore_spec: ApiSpec, library_spec: ApiSpec, seed: int
) -> None:
    for label, schema, registry in _spec_schemas(buggy_spec, petstore_spec, library_spec):
        value = SchemaGenerator(random.Random(seed), registry).generate_valid(schema)
        assert registry.is_valid(schema, value), label


def test_recursive_schema_terminates(library_spec: ApiSpec) -> None:
    schema = library_spec.endpoint("GET /categories").responses["200"].json_schema
    registry = SchemaRegistry(library_spec.schema_resources)
    for seed in range(50):
        value = SchemaGenerator(random.Random(seed), registry).generate(schema)
        assert registry.is_valid(schema, value)


@pytest.mark.parametrize("name", sorted(FEATURE_SCHEMAS))
def test_same_seed_same_value(name: str) -> None:
    schema = FEATURE_SCHEMAS[name]
    first = SchemaGenerator(random.Random(1234)).generate(schema)
    second = SchemaGenerator(random.Random(1234)).generate(schema)
    assert first == second


def test_different_seeds_differ() -> None:
    schema = FEATURE_SCHEMAS["nested_object"]
    values = {repr(SchemaGenerator(random.Random(seed)).generate(schema)) for seed in range(20)}
    assert len(values) > 15


def test_generator_never_uses_global_random(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("global random used")

    for name in ("random", "randint", "choice", "uniform", "shuffle", "getrandbits", "randrange"):
        monkeypatch.setattr(random, name, forbidden)
    for schema in FEATURE_SCHEMAS.values():
        SchemaGenerator(random.Random(7)).generate(schema)


def test_impossible_schemas_raise() -> None:
    generator = SchemaGenerator(random.Random(0))
    with pytest.raises(GenerationError):
        generator.generate_valid({"type": "integer", "minimum": 5, "maximum": 4})
    with pytest.raises(GenerationError):
        generator.generate_valid({"type": "string", "minLength": 3, "maxLength": 2})
    with pytest.raises(GenerationError):
        generator.generate_valid(False)


@settings(max_examples=100, deadline=None)
@given(seed=seeds)
def test_regex_generator_matches(seed: int) -> None:
    generator = RegexGenerator(random.Random(seed))
    for pattern in (r"^[A-Z]{2}\d{3}$", r"^(a|bc)+$", r"^\w+@\w+\.(com|org)$", r"x{2,}y?"):
        assert re.search(pattern, generator.generate(pattern))
