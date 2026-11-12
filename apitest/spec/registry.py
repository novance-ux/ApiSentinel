"""JSON Schema validation against resolved schemas, including circular-ref resources."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from apitest.spec.errors import SpecError
from apitest.spec.resolver import URN_PREFIX, lookup_pointer

INT32 = (-(2**31), 2**31 - 1)
INT64 = (-(2**63), 2**63 - 1)


def _format_checker() -> FormatChecker:
    # A bare FormatChecker() mixes formats from every draft (e.g. draft-3 `time`, which
    # rejects offsets); start from the 2020-12 set instead.
    checker = FormatChecker()
    checker.checkers = dict(Draft202012Validator.FORMAT_CHECKER.checkers)

    def in_range(bounds: tuple[int, int]) -> Any:
        def check(value: object) -> bool:
            if not isinstance(value, int) or isinstance(value, bool):
                return True  # format only constrains integers
            return bounds[0] <= value <= bounds[1]

        return check

    checker.checks("int32")(in_range(INT32))
    checker.checks("int64")(in_range(INT64))
    return checker


FORMAT_CHECKER = _format_checker()


class SchemaRegistry:
    """Builds cached validators whose `urn:apitest:` refs resolve to the spec's resources."""

    def __init__(self, resources: dict[str, Any] | None = None) -> None:
        self.resources = resources or {}
        self._registry: Registry[Any] = Registry().with_resources(
            (uri, DRAFT202012.create_resource(contents)) for uri, contents in self.resources.items()
        )
        self._cache: dict[int, tuple[Any, Draft202012Validator]] = {}

    def validator(self, schema: Any) -> Draft202012Validator:
        cached = self._cache.get(id(schema))
        if cached is not None and cached[0] is schema:
            return cached[1]
        validator = Draft202012Validator(
            schema, registry=self._registry, format_checker=FORMAT_CHECKER
        )
        self._cache[id(schema)] = (schema, validator)
        return validator

    def is_valid(self, schema: Any, instance: Any) -> bool:
        return bool(self.validator(schema).is_valid(instance))

    def errors(self, schema: Any, instance: Any, limit: int = 5) -> list[str]:
        messages: list[str] = []
        for error in self.validator(schema).iter_errors(instance):
            location = "/".join(str(part) for part in error.absolute_path) or "<root>"
            messages.append(f"{location}: {error.message}")
            if len(messages) >= limit:
                break
        return messages

    def lookup(self, ref: str) -> Any:
        """Return the schema an absolute `urn:apitest:` ref points to."""
        if not ref.startswith(URN_PREFIX):
            raise SpecError(f"unexpected unresolved $ref {ref!r}")
        uri, _, fragment = ref.partition("#")
        if uri not in self.resources:
            raise SpecError(f"unknown schema resource {uri!r}")
        return lookup_pointer(self.resources[uri], unquote(fragment))
