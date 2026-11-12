"""Generate JSON values that satisfy a JSON Schema, using an explicit seeded random.Random.

Nothing in this module touches the global `random` state: every choice goes through the
`rng` passed to :class:`SchemaGenerator`, so a seed fully determines the output.
"""

from __future__ import annotations

import copy
import json
import math
import random
import string
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from apitest.generation.formats import FORMATS
from apitest.generation.merge import merge_all_of
from apitest.generation.regex import RegexGenerationError, RegexGenerator
from apitest.spec.registry import INT32, INT64, SchemaRegistry

ALL_TYPES = ("string", "integer", "number", "boolean", "object", "array", "null")
DEFAULT_ALPHABET = string.ascii_letters + string.digits + " -_.'"
HARD_DEPTH_LIMIT = 40


class GenerationError(ValueError):
    """No value satisfying the schema could be produced."""


@dataclass(frozen=True)
class GeneratorConfig:
    max_depth: int = 4
    max_string_extra: int = 24
    max_array_extra: int = 3
    default_int_span: int = 1000
    optional_probability: float = 0.5
    null_probability: float = 0.1
    alphabet: str = DEFAULT_ALPHABET
    attempts: int = 25


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def infer_types(schema: dict[str, Any]) -> list[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list) and declared:
        return list(declared)
    if any(k in schema for k in ("properties", "required", "additionalProperties")):
        return ["object"]
    if any(k in schema for k in ("items", "prefixItems", "minItems", "maxItems")):
        return ["array"]
    if any(k in schema for k in ("minLength", "maxLength", "pattern", "format")):
        return ["string"]
    if any(k in schema for k in ("minimum", "maximum", "multipleOf", "exclusiveMinimum")):
        return ["number"]
    return ["string", "integer", "number", "boolean"]


class SchemaGenerator:
    def __init__(
        self,
        rng: random.Random,
        registry: SchemaRegistry | None = None,
        config: GeneratorConfig | None = None,
    ) -> None:
        self.rng = rng
        self.registry = registry or SchemaRegistry()
        self.config = config or GeneratorConfig()
        self.regex = RegexGenerator(rng)

    # ------------------------------------------------------------------ public API

    def generate_valid(self, schema: Any) -> Any:
        """Generate a value and confirm it validates, retrying on the rare miss."""
        last_error = "no attempts made"
        for _ in range(self.config.attempts):
            try:
                value = self.generate(schema)
            except GenerationError as exc:
                last_error = str(exc)
                continue
            if self.registry.is_valid(schema, value):
                return value
            last_error = "; ".join(self.registry.errors(schema, value, limit=2))
        raise GenerationError(f"could not generate a valid value: {last_error}")

    def generate(self, schema: Any, depth: int = 0) -> Any:
        if depth > HARD_DEPTH_LIMIT:
            raise GenerationError("schema recursion too deep (required cycle?)")
        if schema is True or schema == {}:
            return self._scalar(depth)
        if schema is False:
            raise GenerationError("the `false` schema accepts no values")
        if not isinstance(schema, dict):
            raise GenerationError(f"not a schema: {schema!r}")
        if "$ref" in schema:
            return self._from_ref(schema, depth)
        if "allOf" in schema:
            merged = merge_all_of(schema, self.resolve)
            merged.pop("allOf", None)  # residual constraints are enforced by generate_valid
            return self.generate(merged, depth)
        if "oneOf" in schema:
            return self._from_branches(schema, "oneOf", depth)
        if "anyOf" in schema:
            return self._from_branches(schema, "anyOf", depth)
        if "not" in schema:
            return self._from_not(schema, depth)
        if "const" in schema:
            return copy.deepcopy(schema["const"])
        if "enum" in schema:
            return self._from_enum(schema)
        return self._from_type(schema, self._pick_type(schema), depth)

    def resolve(self, schema: Any) -> Any:
        """Follow a `urn:apitest:` ref (circular schemas) if `schema` is one."""
        if isinstance(schema, dict) and "$ref" in schema:
            target = self.registry.lookup(schema["$ref"])
            rest = {k: v for k, v in schema.items() if k != "$ref"}
            return {"allOf": [target, rest]} if rest else target
        return schema

    # ------------------------------------------------------------------ composition

    def _from_ref(self, schema: dict[str, Any], depth: int) -> Any:
        return self.generate(self.resolve(schema), depth + 1)

    def _from_branches(self, schema: dict[str, Any], keyword: str, depth: int) -> Any:
        rest = {k: v for k, v in schema.items() if k != keyword}
        branches = list(schema[keyword])
        for _ in range(max(1, self.config.attempts // max(1, len(branches)))):
            self.rng.shuffle(branches)
            for branch in branches:
                candidate = {"allOf": [rest, branch]} if rest else branch
                try:
                    value = self.generate(candidate, depth)
                except GenerationError:
                    continue
                if keyword == "anyOf" or self._matches_exactly_one(schema[keyword], value):
                    return value
        raise GenerationError(f"no {keyword} branch produced a value")

    def _matches_exactly_one(self, branches: list[Any], value: Any) -> bool:
        return sum(1 for branch in branches if self.registry.is_valid(branch, value)) == 1

    def _from_not(self, schema: dict[str, Any], depth: int) -> Any:
        rest = {k: v for k, v in schema.items() if k != "not"}
        for _ in range(self.config.attempts):
            value = self.generate(rest, depth)
            if not self.registry.is_valid(schema["not"], value):
                return value
        raise GenerationError("could not satisfy `not`")

    def _from_enum(self, schema: dict[str, Any]) -> Any:
        options = schema["enum"]
        if not options:
            raise GenerationError("empty enum")
        return copy.deepcopy(self.rng.choice(options))

    # ------------------------------------------------------------------ types

    def _pick_type(self, schema: dict[str, Any]) -> str:
        types = infer_types(schema)
        non_null = [t for t in types if t != "null"]
        if not non_null or ("null" in types and self.rng.random() < self.config.null_probability):
            return "null"
        return self.rng.choice(non_null)

    def _from_type(self, schema: dict[str, Any], kind: str, depth: int) -> Any:
        if kind == "string":
            return self._string(schema)
        if kind == "integer":
            return self._integer(schema)
        if kind == "number":
            return self._number(schema)
        if kind == "boolean":
            return self.rng.random() < 0.5
        if kind == "null":
            return None
        if kind == "array":
            return self._array(schema, depth)
        if kind == "object":
            return self._object(schema, depth)
        raise GenerationError(f"unknown type {kind!r}")

    def _scalar(self, depth: int) -> Any:
        return self._from_type({}, self.rng.choice(("string", "integer", "boolean")), depth)

    def _string(self, schema: dict[str, Any]) -> str:
        min_len = int(schema.get("minLength", 0))
        max_len = schema.get("maxLength")
        if max_len is not None and max_len < min_len:
            raise GenerationError("maxLength < minLength")
        if "pattern" in schema:
            try:
                return self.regex.generate(schema["pattern"], min_len, max_len)
            except RegexGenerationError as exc:
                raise GenerationError(str(exc)) from exc
        formatter = FORMATS.get(str(schema.get("format", "")))
        if formatter is not None:
            value = formatter(self.rng)
            if len(value) >= min_len and (max_len is None or len(value) <= max_len):
                return value
        upper = min_len + self.config.max_string_extra
        if max_len is not None:
            upper = min(upper, max_len)
        length = self.rng.randint(min_len, upper)
        return "".join(self.rng.choice(self.config.alphabet) for _ in range(length))

    def int_bounds(self, schema: dict[str, Any]) -> tuple[int, int]:
        lows: list[int] = []
        highs: list[int] = []
        if "minimum" in schema:
            lows.append(math.ceil(schema["minimum"]))
        if "exclusiveMinimum" in schema:
            lows.append(math.floor(schema["exclusiveMinimum"]) + 1)
        if "maximum" in schema:
            highs.append(math.floor(schema["maximum"]))
        if "exclusiveMaximum" in schema:
            highs.append(math.ceil(schema["exclusiveMaximum"]) - 1)
        span = self.config.default_int_span
        low = max(lows) if lows else None
        high = min(highs) if highs else None
        if low is None and high is None:
            low, high = 0, span
        elif low is None:
            assert high is not None
            low = high - span
        elif high is None:
            high = low + span
        fmt_bounds = {"int32": INT32, "int64": INT64}.get(str(schema.get("format")))
        if fmt_bounds is not None:
            low, high = max(low, fmt_bounds[0]), min(high, fmt_bounds[1])
        if low > high:
            raise GenerationError(f"empty integer range [{low}, {high}]")
        return low, high

    def _integer(self, schema: dict[str, Any]) -> int:
        low, high = self.int_bounds(schema)
        multiple = schema.get("multipleOf")
        if not multiple:
            return self.rng.randint(low, high)
        step = Decimal(str(multiple))
        first, last = math.ceil(Decimal(low) / step), math.floor(Decimal(high) / step)
        candidates = [v for v in (first, last) if (Decimal(v) * step) % 1 == 0]
        for _ in range(self.config.attempts):
            k = self.rng.randint(first, last) if first <= last else None
            if k is not None and (Decimal(k) * step) % 1 == 0:
                return int(Decimal(k) * step)
        if candidates:
            return int(Decimal(candidates[0]) * step)
        raise GenerationError(f"no integer multiple of {multiple} in [{low}, {high}]")

    def _number(self, schema: dict[str, Any]) -> float:
        low = float(schema.get("minimum", -math.inf))
        high = float(schema.get("maximum", math.inf))
        if "exclusiveMinimum" in schema:
            low = max(low, _nudge_up(float(schema["exclusiveMinimum"])))
        if "exclusiveMaximum" in schema:
            high = min(high, _nudge_down(float(schema["exclusiveMaximum"])))
        span = float(self.config.default_int_span)
        if math.isinf(low) and math.isinf(high):
            low, high = 0.0, span
        elif math.isinf(low):
            low = high - span
        elif math.isinf(high):
            high = low + span
        if low > high:
            raise GenerationError(f"empty number range [{low}, {high}]")
        multiple = schema.get("multipleOf")
        if multiple:
            return self._number_multiple(float(multiple), low, high)
        value = round(self.rng.uniform(low, high), 2)
        return value if low <= value <= high else self.rng.uniform(low, high)

    def _number_multiple(self, multiple: float, low: float, high: float) -> float:
        first, last = math.ceil(low / multiple), math.floor(high / multiple)
        for _ in range(self.config.attempts):
            if first > last:
                break
            candidate = float(Decimal(self.rng.randint(first, last)) * Decimal(str(multiple)))
            if low <= candidate <= high and (candidate / multiple).is_integer():
                return candidate
        if low <= 0 <= high:
            return 0.0
        raise GenerationError(f"no multiple of {multiple} in [{low}, {high}]")

    def _array(self, schema: dict[str, Any], depth: int) -> list[Any]:
        min_items = int(schema.get("minItems", 0))
        upper = min_items + (self.config.max_array_extra if depth < self.config.max_depth else 0)
        if "maxItems" in schema:
            upper = min(upper, int(schema["maxItems"]))
        if upper < min_items:
            raise GenerationError("maxItems < minItems")
        prefix = list(schema.get("prefixItems", []))
        items = schema.get("items", {})
        if items is False:
            upper = min(upper, len(prefix))
        count = self.rng.randint(min_items, upper)
        values: list[Any] = []
        seen: set[str] = set()
        for index in range(count):
            item_schema = prefix[index] if index < len(prefix) else items
            unique = seen if schema.get("uniqueItems") else None
            try:
                values.append(self._array_item(item_schema, depth, unique))
            except GenerationError:
                if index < min_items:
                    raise
                break  # optional items: stop once the item domain is exhausted
        if "contains" in schema and not any(
            self.registry.is_valid(schema["contains"], v) for v in values
        ):
            element = self.generate(schema["contains"], depth + 1)
            if values:
                values[self.rng.randrange(len(values))] = element
            else:
                values.append(element)
        return values

    def _array_item(self, item_schema: Any, depth: int, seen: set[str] | None) -> Any:
        for _ in range(self.config.attempts):
            value = self.generate(item_schema, depth + 1)
            if seen is None:
                return value
            key = canonical(value)
            if key not in seen:
                seen.add(key)
                return value
        raise GenerationError("could not generate enough unique array items")

    def _object(self, schema: dict[str, Any], depth: int) -> dict[str, Any]:
        props: dict[str, Any] = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        out: dict[str, Any] = {}
        for name in schema.get("required", []):
            out[name] = self._property(name, props, extra, depth)
        optional = [name for name in props if name not in out]
        if depth < self.config.max_depth:
            for name in optional:
                if self.rng.random() < self.config.optional_probability:
                    out[name] = self.generate(props[name], depth + 1)
        for name, needed in schema.get("dependentRequired", {}).items():
            if name in out:
                for dep in needed:
                    out.setdefault(dep, self._property(dep, props, extra, depth))
        self._fill_min_properties(schema, out, props, extra, depth)
        return out

    def _property(self, name: str, props: dict[str, Any], extra: Any, depth: int) -> Any:
        if name in props:
            return self.generate(props[name], depth + 1)
        return self.generate(extra if isinstance(extra, dict) else {}, depth + 1)

    def _fill_min_properties(
        self,
        schema: dict[str, Any],
        out: dict[str, Any],
        props: dict[str, Any],
        extra: Any,
        depth: int,
    ) -> None:
        minimum = int(schema.get("minProperties", 0))
        candidates = [name for name in props if name not in out]
        while len(out) < minimum:
            if candidates:
                name = candidates.pop(0)
                out[name] = self.generate(props[name], depth + 1)
            elif extra is not False:
                out[f"extra_{len(out)}"] = self._property("", {}, extra, depth)
            else:
                raise GenerationError("cannot satisfy minProperties")


def _nudge_up(value: float) -> float:
    return value + max(abs(value) * 1e-9, 1e-6)


def _nudge_down(value: float) -> float:
    return value - max(abs(value) * 1e-9, 1e-6)
