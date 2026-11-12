"""LLM-assisted test cases (strategy `llm`).

(a) Realistic values: schema-valid request bodies with domain-plausible data instead of
    random strings. Anything that fails the spec's own schema is discarded.
(b) Business rules: tests proposed from endpoint summaries/descriptions, targeting rules
    the schema alone does not encode. The model's expectation is overridden whenever the
    request is invalid per the schema, so it cannot invent contract violations.
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any

from apitest.context import SessionContext
from apitest.generation.cases import RequestParts
from apitest.generation.negative import request_is_valid
from apitest.generation.values import GenerationError, SchemaGenerator
from apitest.llm.client import AnthropicJsonLlm, CachedJsonLlm, JsonLlm, LlmError
from apitest.models import Endpoint, Expectation, Result, Strategy, TestCase

SYSTEM = (
    "You design test inputs for an HTTP API described by an OpenAPI specification. "
    "Reply only with JSON matching the requested schema."
)
EXAMPLES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "examples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"body_json": {"type": "string"}},
                "required": ["body_json"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["examples"],
    "additionalProperties": False,
}
RULES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "rule": {"type": "string"},
                    "expect": {"type": "string", "enum": ["success", "client_error"]},
                    "path_params_json": {"type": "string"},
                    "query_json": {"type": "string"},
                    "body_json": {"type": "string"},
                },
                "required": [
                    "name",
                    "rule",
                    "expect",
                    "path_params_json",
                    "query_json",
                    "body_json",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tests"],
    "additionalProperties": False,
}
MAX_CONCURRENT_PROMPTS = 4


def _endpoint_brief(endpoint: Endpoint) -> str:
    params = [
        {"name": p.name, "in": p.location, "required": p.required, "schema": p.json_schema}
        for p in endpoint.parameters
    ]
    body = endpoint.request_body.json_schema if endpoint.request_body else None
    return json.dumps(
        {
            "operation": endpoint.key,
            "summary": endpoint.summary,
            "description": endpoint.description,
            "parameters": params,
            "request_body_schema": body,
            "documented_statuses": list(endpoint.responses),
        },
        indent=2,
    )


def examples_prompt(endpoint: Endpoint, count: int) -> str:
    return (
        f"{_endpoint_brief(endpoint)}\n\n"
        f"Propose {count} realistic request bodies a real client of this operation would send. "
        "Every body must be valid against request_body_schema. Use plausible domain values "
        "(real-looking names, prices, emails, descriptions), not placeholders like 'string'. "
        "Vary them. Encode each body as a JSON string in body_json."
    )


def rules_prompt(endpoint: Endpoint, count: int) -> str:
    return (
        f"{_endpoint_brief(endpoint)}\n\n"
        "From the summary and description, infer business rules this operation should enforce "
        "that the schema alone may not capture (documented limits, relationships between "
        f"fields, calculations, state rules). Propose up to {count} test requests, each "
        "targeting one rule. For each give a snake_case name, the rule in one sentence, the "
        "expected outcome (success or client_error), and path_params_json, query_json and "
        'body_json as JSON strings (use "{}" when there are none, "null" for no body). '
        "Propose nothing if the description states no rules."
    )


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


class LlmCaseBuilder:
    def __init__(self, ctx: SessionContext, llm: JsonLlm) -> None:
        self.ctx = ctx
        self.llm = llm
        self.generator: SchemaGenerator = ctx.factory.generator(
            random.Random(f"{ctx.config.seed}:llm")
        )
        self.rejected = 0

    def _base(self, endpoint: Endpoint) -> RequestParts | None:
        try:
            return self.ctx.factory.valid_parts(endpoint, self.generator, pool_probability=1.0)
        except GenerationError:
            return None

    def _case(
        self, endpoint: Endpoint, technique: str, parts: RequestParts, expect: Expectation, why: str
    ) -> TestCase:
        self.ctx.factory.apply_pool(endpoint, parts, self.generator, 1.0)
        if not request_is_valid(endpoint, parts, self.generator):
            expect = Expectation.CLIENT_ERROR
        return self.ctx.factory.make_case(
            endpoint, Strategy.LLM, technique, parts, expect, description=why
        )

    async def realistic(self, endpoint: Endpoint) -> list[TestCase]:
        body = endpoint.request_body
        if body is None or body.json_schema is None:
            return []
        count = self.ctx.config.llm_examples
        reply = await self.llm.complete_json(
            SYSTEM, examples_prompt(endpoint, count), EXAMPLES_SCHEMA
        )
        cases = []
        for example in (reply or {}).get("examples", [])[:count]:
            value = _loads(example.get("body_json", ""))
            base = self._base(endpoint)
            if base is None or not self.ctx.registry.is_valid(body.json_schema, value):
                self.rejected += 1
                continue
            base.body, base.has_body, base.content_type = value, True, body.content_type
            cases.append(
                self._case(
                    endpoint,
                    "llm_realistic",
                    base,
                    Expectation.SUCCESS,
                    "LLM-proposed realistic body",
                )
            )
        return cases

    async def rules(self, endpoint: Endpoint) -> list[TestCase]:
        if not (endpoint.summary or endpoint.description):
            return []
        count = self.ctx.config.llm_rules
        reply = await self.llm.complete_json(SYSTEM, rules_prompt(endpoint, count), RULES_SCHEMA)
        cases = []
        for test in (reply or {}).get("tests", [])[:count]:
            base = self._base(endpoint)
            if base is None:
                continue
            path_params, query = _loads(test["path_params_json"]), _loads(test["query_json"])
            if isinstance(path_params, dict):
                base.path_params.update(
                    {k: v for k, v in path_params.items() if k in base.path_params}
                )
            if isinstance(query, dict):
                base.query.update(query)
            body = _loads(test["body_json"])
            if endpoint.request_body is not None and body is not None:
                base.body, base.has_body = body, True
                base.content_type = endpoint.request_body.content_type
            expect = (
                Expectation.SUCCESS if test["expect"] == "success" else Expectation.CLIENT_ERROR
            )
            name = "".join(ch for ch in str(test["name"]) if ch.isalnum() or ch == "_")[:60]
            cases.append(self._case(endpoint, f"llm_rule:{name}", base, expect, str(test["rule"])))
        return cases

    async def build(self) -> list[TestCase]:
        limit = asyncio.Semaphore(MAX_CONCURRENT_PROMPTS)

        async def for_endpoint(endpoint: Endpoint) -> list[TestCase]:
            async with limit:
                return await self.realistic(endpoint) + await self.rules(endpoint)

        batches = await asyncio.gather(*(for_endpoint(e) for e in self.ctx.spec.endpoints))
        return [case for batch in batches for case in batch]


def default_llm(ctx: SessionContext) -> JsonLlm:
    config = ctx.config
    inner = AnthropicJsonLlm(config.llm_model, config.llm_effort)
    return CachedJsonLlm(inner, Path(config.llm_cache_dir), config.llm_model)


async def run_llm_cases(ctx: SessionContext) -> list[Result]:
    try:
        builder = LlmCaseBuilder(ctx, ctx.llm or default_llm(ctx))
        cases = await builder.build()
    except LlmError as exc:
        ctx.notes.append(f"llm: skipped ({exc})")
        return []
    if builder.rejected:
        ctx.notes.append(
            f"llm: discarded {builder.rejected} proposed bodies that failed the schema"
        )
    results = await ctx.runner.run(cases)
    ctx.harvest(results)
    return results
