"""Shared helpers: run apitest in-process against the buggy example app."""

from __future__ import annotations

from typing import Any

import httpx

from apitest.config import ApiKeyAuth, RunConfig
from apitest.models import ApiSpec, Failure, RunReport, Strategy
from apitest.session import run_session
from examples.buggy_api.app import API_KEY, create_app

BASE_URL = "http://buggy.test"


def asgi_client(app: Any | None = None) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app or create_app(), raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, timeout=10)


def buggy_config(strategies: list[Strategy], **overrides: Any) -> RunConfig:
    values: dict[str, Any] = {
        "base_url": BASE_URL,
        "auth": ApiKeyAuth(type="api_key", value=API_KEY),
        "strategies": strategies,
        "seed": 0,
        "concurrency": 4,
        **overrides,
    }
    return RunConfig(**values)


async def run_buggy(spec: ApiSpec, strategies: list[Strategy], **overrides: Any) -> RunReport:
    config = buggy_config(strategies, **overrides)
    return await run_session(spec, config, BASE_URL, client=asgi_client())


def found(report: RunReport, endpoint: str, check: str) -> list[Failure]:
    return [f for f in report.failures if f.endpoint == endpoint and f.check == check]
