"""Build TestCases from endpoints: valid (positive) requests and missing-credential probes.

Every endpoint/strategy pair gets its own RNG seeded from ``"{seed}:{strategy}:{key}"``, so
adding an endpoint to the spec does not change the data generated for any other endpoint.
"""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from typing import Any

from apitest.execution.requests import serialize_scalar
from apitest.generation.values import GenerationError, GeneratorConfig, SchemaGenerator
from apitest.models import ApiSpec, Endpoint, Expectation, Strategy, TestCase
from apitest.spec.registry import SchemaRegistry
from apitest.stateful.naming import resource_for_param
from apitest.stateful.pool import MISSING, ValuePool


@dataclass
class RequestParts:
    path_params: dict[str, Any] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    body: Any = None
    has_body: bool = False
    content_type: str | None = None
    raw_body: str | None = None

    def copy(self) -> RequestParts:
        return copy.deepcopy(self)


class CaseFactory:
    def __init__(
        self,
        spec: ApiSpec,
        registry: SchemaRegistry,
        seed: int,
        generator_config: GeneratorConfig | None = None,
        pool: ValuePool | None = None,
    ) -> None:
        self.spec = spec
        self.registry = registry
        self.seed = seed
        self.generator_config = generator_config or GeneratorConfig()
        self.pool = pool or ValuePool()
        self.skipped: list[str] = []
        self._counter = 0

    def rng_for(self, strategy: Strategy, endpoint: Endpoint) -> random.Random:
        return random.Random(f"{self.seed}:{strategy.value}:{endpoint.key}")

    def generator(self, rng: random.Random) -> SchemaGenerator:
        return SchemaGenerator(rng, self.registry, self.generator_config)

    # ------------------------------------------------------------------ requests

    def valid_parts(
        self, endpoint: Endpoint, generator: SchemaGenerator, pool_probability: float = 0.8
    ) -> RequestParts:
        """A schema-valid request: all required params, some optional ones, a valid body.

        Id-like fields take a known-live id from the pool with `pool_probability`; positive
        cases keep some random ids so unknown-id paths (404) get exercised too.
        """
        parts = RequestParts()
        for param in endpoint.parameters:
            if not param.required and generator.rng.random() < 0.5:
                continue
            value = generator.generate_valid(param.json_schema)
            if param.location == "path":
                parts.path_params[param.name] = value
            elif param.location == "query":
                parts.query[param.name] = value
            elif param.location == "header":
                parts.headers[param.name] = serialize_scalar(value)
            else:
                parts.cookies[param.name] = serialize_scalar(value)
        body = endpoint.request_body
        if body is not None and (body.required or generator.rng.random() < 0.8):
            parts.content_type = body.content_type
            if body.json_schema is None:
                parts.raw_body = "apitest"
            else:
                parts.body = generator.generate_valid(body.json_schema)
                parts.has_body = True
        self.apply_pool(endpoint, parts, generator, pool_probability)
        return parts

    def apply_pool(
        self, endpoint: Endpoint, parts: RequestParts, gen: SchemaGenerator, probability: float
    ) -> None:
        """Swap generated ids for ids of resources that really exist, when we know some."""
        for name in list(parts.path_params):
            value = self.pool.pick(resource_for_param(name, endpoint.path), gen.rng, probability)
            if value is not MISSING:
                parts.path_params[name] = value
        if parts.has_body and isinstance(parts.body, dict):
            for name in list(parts.body):
                value = self.pool.pick(resource_for_param(name), gen.rng, probability)
                if value is not MISSING:
                    parts.body[name] = value

    def make_case(
        self,
        endpoint: Endpoint,
        strategy: Strategy,
        technique: str,
        parts: RequestParts,
        expect: Expectation,
        *,
        description: str = "",
        authenticate: bool = True,
    ) -> TestCase:
        self._counter += 1
        return TestCase(
            id=f"{strategy.value}-{self._counter:05d}",
            endpoint=endpoint.key,
            strategy=strategy,
            technique=technique,
            description=description,
            method=endpoint.method,
            path=endpoint.path,
            path_params=parts.path_params,
            query=parts.query,
            headers=parts.headers,
            cookies=parts.cookies,
            body=parts.body,
            has_body=parts.has_body,
            content_type=parts.content_type,
            raw_body=parts.raw_body,
            expect=expect,
            authenticate=authenticate,
        )

    # ------------------------------------------------------------------ strategies

    def positive_cases(self, endpoint: Endpoint, count: int) -> list[TestCase]:
        generator = self.generator(self.rng_for(Strategy.POSITIVE, endpoint))
        cases = []
        for index in range(count):
            try:
                parts = self.valid_parts(endpoint, generator)
            except GenerationError as exc:
                self.skipped.append(f"{endpoint.key}: {exc}")
                break
            cases.append(
                self.make_case(
                    endpoint,
                    Strategy.POSITIVE,
                    "schema_valid",
                    parts,
                    Expectation.SUCCESS,
                    description=f"schema-valid request #{index + 1}",
                )
            )
        return cases

    def security_cases(self, endpoint: Endpoint) -> list[TestCase]:
        if not endpoint.requires_auth:
            return []
        generator = self.generator(self.rng_for(Strategy.SECURITY, endpoint))
        try:
            parts = self.valid_parts(endpoint, generator)
        except GenerationError as exc:
            self.skipped.append(f"{endpoint.key}: {exc}")
            return []
        case = self.make_case(
            endpoint,
            Strategy.SECURITY,
            "missing_credentials",
            parts,
            Expectation.UNAUTHORIZED,
            description="valid request sent without credentials",
            authenticate=False,
        )
        return [case]
