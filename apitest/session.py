"""Orchestrates one apitest run: build cases per strategy, execute, collect a RunReport.

Order matters: positive/security run first, their successful creates seed the value pool,
stateful chains run next, and negative/boundary/LLM cases are generated afterwards so they
can target resources that really exist. Everything created is deleted in a `finally`.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Collection
from datetime import UTC, datetime

import httpx

from apitest import __version__
from apitest.config import RunConfig
from apitest.context import SessionContext
from apitest.execution.auth import build_auth
from apitest.execution.runner import Runner, make_client
from apitest.generation.cases import CaseFactory
from apitest.generation.negative import boundary_cases, negative_cases
from apitest.llm.cases import run_llm_cases
from apitest.llm.client import JsonLlm
from apitest.models import ApiSpec, Result, RunReport, Strategy, TestCase
from apitest.spec.registry import SchemaRegistry
from apitest.stateful.graph import build_graph
from apitest.stateful.pool import ValuePool
from apitest.stateful.workflows import cleanup, root_creators, run_stateful
from apitest.validation.checks import ResponseValidator

SETUP_CREATES_PER_ENDPOINT = 2


def build_static_cases(ctx: SessionContext, strategies: Collection[Strategy]) -> list[TestCase]:
    """Cases that need no multi-step state: positive, security, negative, boundary."""
    cases: list[TestCase] = []
    for endpoint in ctx.spec.endpoints:
        if Strategy.POSITIVE in strategies:
            cases += ctx.factory.positive_cases(endpoint, ctx.config.cases_per_endpoint)
        if Strategy.SECURITY in strategies:
            cases += ctx.factory.security_cases(endpoint)
        if Strategy.NEGATIVE in strategies:
            cases += negative_cases(ctx.factory, endpoint)
        if Strategy.BOUNDARY in strategies:
            cases += boundary_cases(ctx.factory, endpoint)
    return cases


async def seed_pool(ctx: SessionContext) -> None:
    """Create a few resources so later strategies can use real ids. Not reported."""
    cases: list[TestCase] = []
    for endpoint in root_creators(ctx):
        for case in ctx.factory.positive_cases(endpoint, SETUP_CREATES_PER_ENDPOINT):
            cases.append(case.model_copy(update={"technique": "setup"}))
    setup = await ctx.runner.run(cases)
    ctx.harvest(setup)
    created = sum(1 for r in setup if r.response is not None and 200 <= r.response.status < 300)
    if cases:
        ctx.notes.append(f"setup: sent {len(cases)} create requests to seed ids ({created} ok)")


async def _run_strategies(ctx: SessionContext, strategies: list[Strategy]) -> list[Result]:
    results: list[Result] = []
    first = [s for s in (Strategy.POSITIVE, Strategy.SECURITY) if s in strategies]
    if first:
        batch = await ctx.runner.run(build_static_cases(ctx, first))
        ctx.harvest(batch)
        results += batch
    if Strategy.STATEFUL in strategies:
        results += await run_stateful(ctx)
    later = [s for s in (Strategy.NEGATIVE, Strategy.BOUNDARY) if s in strategies]
    needs_ids = bool(later) or Strategy.LLM in strategies
    if needs_ids and not ctx.created:
        await seed_pool(ctx)
    if later:
        batch = await ctx.runner.run(build_static_cases(ctx, later))
        ctx.harvest(batch)
        results += batch
    if Strategy.LLM in strategies:
        results += await run_llm_cases(ctx)
    return results


async def run_session(
    spec: ApiSpec,
    config: RunConfig,
    base_url: str,
    *,
    client: httpx.AsyncClient | None = None,
    llm: JsonLlm | None = None,
) -> RunReport:
    registry = SchemaRegistry(spec.schema_resources)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    started = time.perf_counter()
    async with client or make_client(config) as http:
        runner = Runner(
            spec,
            config,
            base_url,
            build_auth(config.auth, spec),
            ResponseValidator(registry, config.latency_threshold_ms),
            http,
        )
        pool = ValuePool()
        factory = CaseFactory(spec, registry, config.seed, pool=pool)
        ctx = SessionContext(spec, config, registry, factory, runner, build_graph(spec), pool)
        ctx.llm = llm
        try:
            results = await _run_strategies(ctx, list(config.strategies))
        finally:
            if config.cleanup:
                removed = await cleanup(ctx)
                if removed:
                    ctx.notes.append(f"cleanup: deleted {removed} resource(s) created by the run")
    executed = Counter(result.test_case.strategy.value for result in results)
    return RunReport(
        tool_version=__version__,
        spec_title=spec.title,
        spec_source=spec.source,
        base_url=base_url,
        seed=config.seed,
        strategies=list(config.strategies),
        started_at=started_at,
        duration_s=round(time.perf_counter() - started, 3),
        request_count=runner.request_count,
        results=results,
        executed_cases=dict(executed),
        skipped=factory.skipped,
        notes=ctx.notes,
    )
