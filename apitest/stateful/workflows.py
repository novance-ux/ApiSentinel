"""Stateful CRUD workflows driven by the dependency graph.

For each root creator (a producer nothing else must run before) a chain runs:
create → read & compare → list (pagination / membership) → update → read & compare →
dependent creates (their own chains) → unauthenticated delete probe → delete →
read-after-delete. Every created resource is deleted again in `cleanup`, which the session
calls from a `finally` block so it runs even when steps fail or raise.
"""

from __future__ import annotations

import json
import math
import random
from typing import Any

from apitest.context import Created, SessionContext
from apitest.generation.cases import RequestParts
from apitest.generation.values import GenerationError, SchemaGenerator
from apitest.models import (
    Endpoint,
    Expectation,
    Failure,
    FailureCategory,
    Result,
    Severity,
    Strategy,
)
from apitest.stateful.graph import Binding, Edge
from apitest.stateful.pool import response_json
from apitest.validation.checks import make_failure

MAX_CHAIN_DEPTH = 2
PAGE_SIZE_NAMES = ("page_size", "pageSize", "per_page", "perPage", "limit", "size", "count")
PAGE_NAMES = ("page", "page_number", "pageNumber")
OFFSET_NAMES = ("offset", "skip", "start")
TOTAL_NAMES = ("total", "total_count", "totalCount", "total_items", "totalItems", "count")
MAX_LIST_PAGE = 500


def resolve_pointer(body: Any, pointer: str) -> Any:
    node = body
    for part in [p for p in pointer.split("/") if p]:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return None
    return node


def apply_bindings(parts: RequestParts, bindings: list[Binding], source: dict[str, Any]) -> bool:
    """Copy values from a producer response into a request. False if a value is missing."""
    for binding in bindings:
        value = resolve_pointer(source, binding.pointer)
        if value is None:
            return False
        if binding.target == "path":
            parts.path_params[binding.name] = value
        elif binding.target == "query":
            parts.query[binding.name] = value
        elif binding.target == "header":
            parts.headers[binding.name] = str(value)
        elif isinstance(parts.body, dict):
            parts.body[binding.name] = value
    return True


def _same(sent: Any, got: Any) -> bool:
    if isinstance(sent, str) and isinstance(got, str):
        return sent.strip() == got.strip()
    if isinstance(sent, int | float) and isinstance(got, int | float):
        return not isinstance(sent, bool) and math.isclose(sent, got, rel_tol=1e-9, abs_tol=1e-9)
    return bool(sent == got)


def _array_and_total(body: Any) -> tuple[list[Any] | None, int | None]:
    if isinstance(body, list):
        return body, None
    if not isinstance(body, dict):
        return None, None
    array = next((v for v in body.values() if isinstance(v, list)), None)
    total = next(
        (
            body[n]
            for n in TOTAL_NAMES
            if isinstance(body.get(n), int) and not isinstance(body.get(n), bool)
        ),
        None,
    )
    return array, total


class Workflow:
    def __init__(self, ctx: SessionContext) -> None:
        self.ctx = ctx
        self.results: list[Result] = []
        rng = random.Random(f"{ctx.config.seed}:stateful")
        self.generator: SchemaGenerator = ctx.factory.generator(rng)

    # ------------------------------------------------------------------ steps

    async def step(
        self,
        endpoint: Endpoint,
        technique: str,
        parts: RequestParts,
        expect: Expectation,
        *,
        authenticate: bool = True,
        description: str = "",
    ) -> Result:
        case = self.ctx.factory.make_case(
            endpoint,
            Strategy.STATEFUL,
            technique,
            parts,
            expect,
            description=description,
            authenticate=authenticate,
        )
        result = await self.ctx.runner.execute(case)
        self.results.append(result)
        return result

    def fail(
        self, result: Result, check: str, message: str, severity: Severity = Severity.MEDIUM
    ) -> None:
        failure: Failure = make_failure(
            result.test_case,
            result.request,
            result.response,
            FailureCategory.CONTRACT,
            check,
            message,
            severity,
        )
        result.failures.append(failure)

    def _endpoint(self, key: str) -> Endpoint:
        return self.ctx.spec.endpoint(key)

    def _valid_parts(self, endpoint: Endpoint) -> RequestParts | None:
        try:
            return self.ctx.factory.valid_parts(endpoint, self.generator)
        except GenerationError as exc:
            self.ctx.notes.append(f"stateful: cannot build a request for {endpoint.key}: {exc}")
            return None

    # ------------------------------------------------------------------ create

    async def create(
        self, producer: Endpoint, parent: tuple[Edge, Created] | None, depth: int
    ) -> Created | None:
        parts = self._valid_parts(producer)
        if parts is None:
            return None
        for edge in self.ctx.graph.producers_of(producer.key):
            if parent is not None and edge.producer == parent[0].producer:
                apply_bindings(parts, edge.bindings, parent[1].body)
                continue
            if depth >= MAX_CHAIN_DEPTH:
                continue
            prerequisite = await self.create(self._endpoint(edge.producer), None, depth + 1)
            if prerequisite is None or not apply_bindings(parts, edge.bindings, prerequisite.body):
                return None
        result = await self.step(producer, "create", parts, Expectation.SUCCESS)
        body = response_json(result.response)
        if result.response is None or not 200 <= result.response.status < 300:
            return None
        if not isinstance(body, dict):
            return None
        self.ctx.pool.harvest(producer, result.response)
        sent = parts.body if isinstance(parts.body, dict) else {}
        created = Created(producer.key, body, sent)
        self.ctx.created.append(created)
        return created

    # ------------------------------------------------------------------ read / update

    async def read(
        self,
        reader: Endpoint,
        edge: Edge,
        created: Created,
        technique: str,
        expect: Expectation = Expectation.SUCCESS,
    ) -> Result | None:
        parts = self._valid_parts(reader)
        if parts is None or not apply_bindings(parts, edge.bindings, created.body):
            return None
        return await self.step(reader, technique, parts, expect)

    def compare(self, result: Result | None, sent: dict[str, Any], what: str) -> None:
        body = response_json(result.response) if result else None
        if result is None or not isinstance(body, dict) or result.response is None:
            return
        if not 200 <= result.response.status < 300:
            return
        mismatched = [
            f"{key}: sent {value!r}, read {body[key]!r}"
            for key, value in sent.items()
            if key in body and not isinstance(value, dict | list) and not _same(value, body[key])
        ]
        if mismatched:
            self.fail(
                result, "read_mismatch", f"{what} not reflected: " + "; ".join(mismatched[:3])
            )

    async def update(
        self, updater: Endpoint, edge: Edge, created: Created
    ) -> dict[str, Any] | None:
        parts = self._valid_parts(updater)
        if parts is None or not apply_bindings(parts, edge.bindings, created.body):
            return None
        result = await self.step(updater, "update", parts, Expectation.SUCCESS)
        ok = result.response is not None and 200 <= result.response.status < 300
        return parts.body if ok and isinstance(parts.body, dict) else None

    # ------------------------------------------------------------------ listing

    async def listing(self, producer: Endpoint, created: Created) -> None:
        lister = next(
            (e for e in self.ctx.spec.endpoints if e.method == "GET" and e.path == producer.path),
            None,
        )
        if lister is None:
            return
        parts = self._valid_parts(lister)
        if parts is None:
            return
        smallest = parts.copy()
        if self._page_request(lister, smallest, largest=False) is not None:
            small = await self.step(lister, "list_smallest_page", smallest, Expectation.SUCCESS)
            self._check_page(small, created, check_membership=False)
        requested = self._page_request(lister, parts, largest=True)
        result = await self.step(lister, "list_after_create", parts, Expectation.SUCCESS)
        self._check_page(result, created, check_membership=requested is not None)

    def _check_page(self, result: Result, created: Created, *, check_membership: bool) -> None:
        """A first page must hold min(total, page size) items and, if not full, the new id."""
        if result.response is None or not 200 <= result.response.status < 300:
            return
        requested = self._requested_size(result)
        array, total = _array_and_total(response_json(result.response))
        if array is None or requested is None:
            return
        if total is not None and len(array) < min(total, requested):
            self.fail(
                result,
                "pagination_short_page",
                f"first page holds {len(array)} items but total={total} and page size={requested}",
            )
            return
        if not check_membership:
            return
        ids = [item.get("id") for item in array if isinstance(item, dict)]
        created_id = created.body.get("id")
        if len(array) < requested and created_id not in ids:
            self.fail(
                result,
                "created_resource_missing",
                f"created resource id={created_id!r} missing from a non-full first page",
            )

    @staticmethod
    def _requested_size(result: Result) -> int | None:
        for name in PAGE_SIZE_NAMES:
            value = result.test_case.query.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    def _page_request(self, lister: Endpoint, parts: RequestParts, *, largest: bool) -> int | None:
        """Point `parts` at the first page with the largest (or smallest) page size."""
        requested: int | None = None
        for param in lister.params_in("query"):
            schema = param.json_schema
            if param.name in PAGE_SIZE_NAMES:
                if largest:
                    requested = int(min(schema.get("maximum", 100), MAX_LIST_PAGE))
                else:
                    requested = int(max(schema.get("minimum", 1), 1))
                parts.query[param.name] = requested
            elif param.name in PAGE_NAMES:
                parts.query[param.name] = int(schema.get("minimum", 1))
            elif param.name in OFFSET_NAMES:
                parts.query[param.name] = int(schema.get("minimum", 0))
        return requested

    # ------------------------------------------------------------------ delete

    async def delete(self, deleter: Endpoint, edge: Edge, created: Created) -> None:
        parts = self._valid_parts(deleter)
        if parts is None or not apply_bindings(parts, edge.bindings, created.body):
            return
        marker = (deleter.key, json.dumps(parts.path_params, sort_keys=True, default=str))
        gone = False
        if deleter.requires_auth:
            probe = await self.step(
                deleter,
                "unauthenticated_delete",
                parts.copy(),
                Expectation.UNAUTHORIZED,
                authenticate=False,
                description="delete a live resource without credentials",
            )
            gone = probe.response is not None and 200 <= probe.response.status < 300
        if not gone:
            result = await self.step(deleter, "delete", parts, Expectation.SUCCESS)
            gone = result.response is not None and 200 <= result.response.status < 300
        if not gone:
            return
        self.ctx.deleted.add(marker)
        for reader_edge in self._consumers(created.producer, "GET"):
            reader = self._endpoint(reader_edge.consumer)
            after = await self.read(
                reader, reader_edge, created, "read_after_delete", Expectation.ANY
            )
            status = after.response.status if after and after.response else None
            if after is not None and status is not None and 200 <= status < 300:
                self.fail(
                    after,
                    "deleted_resource_retrievable",
                    f"resource still returned {status} after a successful delete",
                    Severity.HIGH,
                )

    # ------------------------------------------------------------------ chains

    def _consumers(self, producer_key: str, method: str) -> list[Edge]:
        return [
            e
            for e in self.ctx.graph.consumers_of(producer_key)
            if self._endpoint(e.consumer).method == method
            and all(b.target != "body" for b in e.bindings)
        ]

    async def chain(
        self, producer: Endpoint, parent: tuple[Edge, Created] | None = None, depth: int = 0
    ) -> None:
        created = await self.create(producer, parent, depth)
        if created is None:
            return
        readers = self._consumers(producer.key, "GET")
        for edge in readers:
            result = await self.read(
                self._endpoint(edge.consumer), edge, created, "read_after_create"
            )
            self.compare(result, created.sent, "created fields")
        await self.listing(producer, created)
        for method in ("PUT", "PATCH"):
            for edge in self._consumers(producer.key, method):
                updated = await self.update(self._endpoint(edge.consumer), edge, created)
                if updated is None:
                    continue
                for reader in readers:
                    result = await self.read(
                        self._endpoint(reader.consumer), reader, created, "read_after_update"
                    )
                    self.compare(result, updated, "updated fields")
        if depth < MAX_CHAIN_DEPTH:
            for edge in self.ctx.graph.consumers_of(producer.key):
                dependent = self._endpoint(edge.consumer)
                if dependent.method == "POST" and any(b.target == "body" for b in edge.bindings):
                    await self.chain(dependent, (edge, created), depth + 1)
        for edge in self._consumers(producer.key, "DELETE"):
            await self.delete(self._endpoint(edge.consumer), edge, created)


def root_creators(ctx: SessionContext) -> list[Endpoint]:
    graph = ctx.graph
    consumers_with_producers = {e.consumer for e in graph.edges if e.producer != e.consumer}
    return [
        ctx.spec.endpoint(key)
        for key in graph.creators()
        if key not in consumers_with_producers and ctx.spec.endpoint(key).method == "POST"
    ]


async def run_stateful(ctx: SessionContext) -> list[Result]:
    """Run one CRUD chain per root creator, sequentially so steps observe each other."""
    workflow = Workflow(ctx)
    for producer in root_creators(ctx):
        await workflow.chain(producer)
    if not workflow.results:
        ctx.notes.append("stateful: no create operations with usable responses were found")
    return workflow.results


async def cleanup(ctx: SessionContext) -> int:
    """Delete every resource created during the run that is still alive."""
    deleted = 0
    generator = ctx.factory.generator(random.Random(f"{ctx.config.seed}:cleanup"))
    for created in reversed(ctx.created):
        for edge in ctx.graph.consumers_of(created.producer):
            deleter = ctx.spec.endpoint(edge.consumer)
            if deleter.method != "DELETE":
                continue
            parts = RequestParts()
            for param in deleter.parameters:
                if param.required and param.location != "path":
                    parts.query[param.name] = generator.generate_valid(param.json_schema)
            if not apply_bindings(parts, edge.bindings, created.body):
                continue
            marker = (deleter.key, json.dumps(parts.path_params, sort_keys=True, default=str))
            if marker in ctx.deleted:
                continue
            case = ctx.factory.make_case(
                deleter, Strategy.STATEFUL, "cleanup", parts, Expectation.ANY
            )
            try:
                await ctx.runner.execute(case)
            except Exception as exc:  # cleanup must never mask the run's own outcome
                ctx.notes.append(f"cleanup of {marker} failed: {exc}")
                continue
            ctx.deleted.add(marker)
            deleted += 1
    return deleted
