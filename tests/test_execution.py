"""Config, auth, request building, runner behaviour (retries, timeouts, concurrency)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from apitest.config import ApiKeyAuth, BasicAuth, BearerAuth, ConfigError, RunConfig, load_config
from apitest.execution.auth import build_auth
from apitest.execution.requests import (
    REDACTED,
    UnsafeTargetError,
    build_request,
    ensure_same_origin,
    record_request,
)
from apitest.execution.runner import Runner
from apitest.models import ApiSpec, Expectation, Strategy, TestCase
from apitest.reporting.curl import shell_quote, to_curl
from apitest.spec.registry import SchemaRegistry
from apitest.validation.checks import ResponseValidator

BASE = "http://api.test"


def _case(**overrides: object) -> TestCase:
    values: dict[str, object] = {
        "id": "positive-00001",
        "endpoint": "GET /items/{item_id}",
        "strategy": Strategy.POSITIVE,
        "technique": "schema_valid",
        "method": "GET",
        "path": "/items/{item_id}",
        "path_params": {"item_id": 7},
        "expect": Expectation.SUCCESS,
    }
    values.update(overrides)
    return TestCase.model_validate(values)


# --------------------------------------------------------------------------- config & auth


def test_config_env_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "abc")
    path = tmp_path / "apitest.yaml"
    path.write_text("base_url: http://x\nauth: {type: bearer, token: '${TOKEN}'}\n")
    config = load_config(path)
    assert isinstance(config.auth, BearerAuth)
    assert config.auth.token == "abc"


def test_config_missing_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE_NOT_SET", raising=False)
    path = tmp_path / "apitest.yaml"
    path.write_text("auth: {type: bearer, token: '${NOPE_NOT_SET}'}\n")
    with pytest.raises(ConfigError, match="NOPE_NOT_SET"):
        load_config(path)


def test_config_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "apitest.yaml"
    path.write_text("concurency: 3\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_api_key_defaults_from_spec(buggy_spec: ApiSpec) -> None:
    material = build_auth(ApiKeyAuth(type="api_key", value="k"), buggy_spec)
    assert material.headers == {"X-API-Key": "k"}


def test_api_key_in_query_and_cookie(buggy_spec: ApiSpec) -> None:
    query = build_auth(
        ApiKeyAuth.model_validate({"type": "api_key", "value": "k", "name": "key", "in": "query"}),
        buggy_spec,
    )
    assert query.query == {"key": "k"}
    cookie = build_auth(
        ApiKeyAuth.model_validate({"type": "api_key", "value": "k", "name": "sid", "in": "cookie"}),
        buggy_spec,
    )
    assert cookie.cookies == {"sid": "k"}


def test_bearer_and_basic(buggy_spec: ApiSpec) -> None:
    assert build_auth(BearerAuth(type="bearer", token="t"), buggy_spec).headers == {
        "Authorization": "Bearer t"
    }
    basic = build_auth(BasicAuth(type="basic", username="u", password="p"), buggy_spec)
    assert basic.headers == {"Authorization": "Basic dTpw"}


# --------------------------------------------------------------------------- requests & curl


def test_path_params_are_escaped_and_origin_is_kept(buggy_spec: ApiSpec) -> None:
    auth = build_auth(ApiKeyAuth(type="api_key", value="k"), buggy_spec)
    case = _case(path_params={"item_id": "../../admin?x=1#frag"})
    request = build_request(case, BASE, auth, {})
    assert str(request.url) == BASE + "/items/..%2F..%2Fadmin%3Fx%3D1%23frag"
    assert request.headers["X-API-Key"] == "k"


def test_unsafe_target_is_refused() -> None:
    with pytest.raises(UnsafeTargetError):
        ensure_same_origin("http://evil.test/x", BASE)
    with pytest.raises(UnsafeTargetError):
        build_request(
            _case(path="@evil.test/x", path_params={}), BASE, build_auth(None, _spec_stub()), {}
        )


def _spec_stub() -> ApiSpec:
    return ApiSpec(title="t", version="1", openapi_version="3.0.3", source="-")


def test_unauthenticated_case_omits_credentials(buggy_spec: ApiSpec) -> None:
    auth = build_auth(ApiKeyAuth(type="api_key", value="k"), buggy_spec)
    request = build_request(_case(authenticate=False), BASE, auth, {})
    assert "X-API-Key" not in request.headers


def test_record_redacts_secrets(buggy_spec: ApiSpec) -> None:
    auth = build_auth(ApiKeyAuth(type="api_key", value="k"), buggy_spec)
    request = build_request(_case(), BASE, auth, {})
    assert record_request(request, auth.secret_names, redact=True).headers["x-api-key"] == REDACTED
    assert record_request(request, auth.secret_names, redact=False).headers["x-api-key"] == "k"


def test_json_body_and_curl() -> None:
    case = _case(
        method="POST",
        path="/items",
        path_params={},
        body={"name": "it's", "n": 1},
        has_body=True,
        content_type="application/json",
    )
    request = build_request(case, BASE, build_auth(None, _spec_stub()), {})
    record = record_request(request, set(), redact=True)
    curl = to_curl(record)
    assert curl.startswith("curl -sS -i -X POST http://api.test/items")
    assert "-H 'content-type: application/json'" in curl
    assert '--data-raw \'{"name": "it\'"\'"\'s", "n": 1}\'' in curl


def test_shell_quote_control_characters() -> None:
    assert shell_quote("a\x00b") == "$'a\\x00b'"
    assert shell_quote("plain") == "plain"


# --------------------------------------------------------------------------- runner


def _runner(spec: ApiSpec, client: httpx.AsyncClient, **config: object) -> Runner:
    run_config = RunConfig.model_validate({"retry_backoff": 0, **config})
    validator = ResponseValidator(SchemaRegistry(spec.schema_resources), 1000)
    return Runner(spec, run_config, BASE, build_auth(None, spec), validator, client)


@respx.mock
async def test_runner_retries_transport_errors(buggy_spec: ApiSpec) -> None:
    route = respx.get(f"{BASE}/items/7").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(404, json={"detail": "x"})]
    )
    async with httpx.AsyncClient() as client:
        result = await _runner(buggy_spec, client, retries=2).execute(_case())
    assert route.call_count == 2
    assert result.response is not None and result.response.status == 404
    assert result.passed


@respx.mock
async def test_runner_gives_up_after_retries(buggy_spec: ApiSpec) -> None:
    respx.get(f"{BASE}/items/7").mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as client:
        result = await _runner(buggy_spec, client, retries=1).execute(_case())
    assert result.error is not None and "ConnectError" in result.error


@respx.mock
async def test_runner_honours_429_retry_after(buggy_spec: ApiSpec) -> None:
    route = respx.get(f"{BASE}/items/7").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0"}),
            httpx.Response(404, json={"detail": "x"}),
        ]
    )
    async with httpx.AsyncClient() as client:
        result = await _runner(buggy_spec, client).execute(_case())
    assert route.call_count == 2
    assert result.response is not None and result.response.status == 404


@respx.mock
async def test_timeout_is_a_performance_failure(buggy_spec: ApiSpec) -> None:
    respx.get(f"{BASE}/items/7").mock(side_effect=httpx.ReadTimeout("slow"))
    async with httpx.AsyncClient() as client:
        result = await _runner(buggy_spec, client).execute(_case())
    assert [f.check for f in result.failures] == ["timeout"]


async def test_concurrency_limit(buggy_spec: ApiSpec) -> None:
    in_flight = 0
    peak = 0

    async def app(
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport) as client:
        runner = _runner(buggy_spec, client, concurrency=3)
        await runner.run([_case(id=f"c{i}") for i in range(12)])
    assert peak == 3
    assert runner.request_count == 12
