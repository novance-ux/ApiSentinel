"""LLM-assisted cases, with a scripted fake model (no network, no credentials)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from apitest.llm.client import AnthropicJsonLlm, CachedJsonLlm, LlmError
from apitest.models import ApiSpec, Expectation, Strategy
from apitest.session import run_session
from tests.helpers import BASE_URL, asgi_client, buggy_config, found


class FakeLlm:
    """Answers by operation name; records every prompt it sees."""

    def __init__(self, replies: dict[str, dict[str, Any]], fail: bool = False) -> None:
        self.replies = replies
        self.fail = fail
        self.prompts: list[str] = []

    async def complete_json(
        self, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any] | None:
        if self.fail:
            raise LlmError("no credentials")
        self.prompts.append(prompt)
        operation = json.loads(prompt.split("\n\n")[0])["operation"]
        kind = "examples" if "examples" in schema["properties"] else "tests"
        return self.replies.get(f"{kind} {operation}", {kind: []})


def _rule(name: str, expect: str, body: Any = None, query: Any = None) -> dict[str, str]:
    return {
        "name": name,
        "rule": f"rule {name}",
        "expect": expect,
        "path_params_json": "{}",
        "query_json": json.dumps(query or {}),
        "body_json": json.dumps(body),
    }


REPLIES: dict[str, dict[str, Any]] = {
    "examples POST /items": {
        "examples": [
            {
                "body_json": json.dumps(
                    {"name": "Dune (paperback)", "price": 9.99, "quantity": 12, "category": "books"}
                )
            },
            {
                "body_json": json.dumps(
                    {
                        "name": "USB-C cable",
                        "price": 7.5,
                        "quantity": 40,
                        "category": "electronics",
                        "tags": ["cable"],
                    }
                )
            },
            {
                "body_json": json.dumps(
                    {"name": "Bad", "price": "free", "quantity": 1, "category": "toys"}
                )
            },
            {"body_json": "not json"},
        ]
    },
    "tests GET /search": {
        "tests": [_rule("accented_query_is_searchable", "success", query={"q": "café"})]
    },
    "tests POST /orders": {
        "tests": [_rule("zero_quantity_rejected", "success", body={"item_id": 1, "quantity": 0})]
    },
}


async def _run(spec: ApiSpec, llm: FakeLlm) -> Any:
    config = buggy_config([Strategy.LLM])
    return await run_session(spec, config, BASE_URL, client=asgi_client(), llm=llm)


@pytest.mark.slow
async def test_llm_cases_are_validated_and_labelled(buggy_spec: ApiSpec) -> None:
    llm = FakeLlm(REPLIES)
    report = await _run(buggy_spec, llm)
    llm_results = [r for r in report.results if r.test_case.strategy == Strategy.LLM]
    realistic = [r for r in llm_results if r.test_case.technique == "llm_realistic"]
    assert [r.test_case.body["name"] for r in realistic] == ["Dune (paperback)", "USB-C cable"]
    assert all(r.test_case.expect == Expectation.SUCCESS for r in realistic)
    # llm_examples=3 caps the list, so the 4th ("not json") is never considered.
    assert any("discarded 1 proposed" in note for note in report.notes)
    # The model said "success" for quantity 0, but the schema says minimum 1: overridden.
    zero = next(
        r for r in llm_results if r.test_case.technique == "llm_rule:zero_quantity_rejected"
    )
    assert zero.test_case.expect == Expectation.CLIENT_ERROR
    assert found(report, "GET /search", "server_error")  # BUG-13, via the accented query
    assert found(report, "POST /orders", "server_error")  # BUG-18


@pytest.mark.slow
async def test_llm_prompts_contain_spec_not_responses(buggy_spec: ApiSpec) -> None:
    llm = FakeLlm(REPLIES)
    await _run(buggy_spec, llm)
    joined = "\n".join(llm.prompts)
    assert "secret-key" not in joined
    assert "Internal Server Error" not in joined
    assert '"operation": "POST /items"' in joined


@pytest.mark.slow
async def test_llm_errors_become_a_note(buggy_spec: ApiSpec) -> None:
    report = await _run(buggy_spec, FakeLlm({}, fail=True))
    assert any("llm: skipped (no credentials)" in note for note in report.notes)
    assert not [r for r in report.results if r.test_case.strategy == Strategy.LLM]


async def test_cache_replays_without_calling_the_model(tmp_path: Path) -> None:
    inner = FakeLlm(REPLIES)
    cached = CachedJsonLlm(inner, tmp_path, "m")
    prompt = json.dumps({"operation": "POST /items"}) + "\n\nask"
    schema: dict[str, Any] = {"properties": {"examples": {}}}
    first = await cached.complete_json("s", prompt, schema)
    second = await cached.complete_json("s", prompt, schema)
    assert first == second
    assert (cached.hits, cached.misses, len(inner.prompts)) == (1, 1, 1)


async def test_anthropic_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    llm = AnthropicJsonLlm()
    calls: list[dict[str, Any]] = []

    async def fake_create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        text = SimpleNamespace(type="text", text='{"examples": []}')
        return SimpleNamespace(stop_reason="end_turn", content=[text])

    monkeypatch.setattr(llm._client.beta.messages, "create", fake_create)
    assert await llm.complete_json("sys", "prompt", {"type": "object"}) == {"examples": []}
    request = calls[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["fallbacks"] == "default"
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["output_config"]["format"] == {
        "type": "json_schema",
        "schema": {"type": "object"},
    }

    async def refuse(**kwargs: Any) -> Any:
        return SimpleNamespace(stop_reason="refusal", content=[])

    monkeypatch.setattr(llm._client.beta.messages, "create", refuse)
    assert await llm.complete_json("sys", "prompt", {"type": "object"}) is None
