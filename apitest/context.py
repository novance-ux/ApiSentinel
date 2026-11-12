"""State shared by every strategy during one run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apitest.config import RunConfig
from apitest.execution.runner import Runner
from apitest.generation.cases import CaseFactory
from apitest.llm.client import JsonLlm
from apitest.models import ApiSpec, Result
from apitest.spec.registry import SchemaRegistry
from apitest.stateful.graph import DependencyGraph
from apitest.stateful.pool import ValuePool


@dataclass
class Created:
    """A resource created during the run, remembered so it can be cleaned up."""

    producer: str
    body: dict[str, Any]
    sent: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionContext:
    spec: ApiSpec
    config: RunConfig
    registry: SchemaRegistry
    factory: CaseFactory
    runner: Runner
    graph: DependencyGraph
    pool: ValuePool
    created: list[Created] = field(default_factory=list)
    deleted: set[tuple[str, str]] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    llm: JsonLlm | None = None  # injected in tests; built from config otherwise

    def harvest(self, results: list[Result]) -> None:
        for result in results:
            endpoint = self.spec.endpoint(result.test_case.endpoint)
            body = self.pool.harvest(endpoint, result.response)
            if body is not None:
                self.created.append(Created(endpoint.key, body))
