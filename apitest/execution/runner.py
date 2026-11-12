"""Async test runner: concurrency limit, timeouts, retries, rate limiting."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable

import httpx

from apitest.config import RunConfig
from apitest.execution.auth import AuthMaterial
from apitest.execution.requests import build_request, record_request
from apitest.models import ApiSpec, ResponseRecord, Result, TestCase
from apitest.validation.checks import ResponseValidator

MAX_RETRY_AFTER_S = 10.0
MAX_BODY_CHARS = 64_000


class RateLimiter:
    """Spaces request starts at least 1/rate seconds apart."""

    def __init__(self, rate: float | None) -> None:
        self._interval = 1.0 / rate if rate else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if not self._interval:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            await asyncio.sleep(delay)


def _retry_after(response: httpx.Response, fallback: float) -> float:
    try:
        return min(float(response.headers.get("retry-after", fallback)), MAX_RETRY_AFTER_S)
    except ValueError:
        return fallback


class Runner:
    def __init__(
        self,
        spec: ApiSpec,
        config: RunConfig,
        base_url: str,
        auth: AuthMaterial,
        validator: ResponseValidator,
        client: httpx.AsyncClient,
    ) -> None:
        self.spec = spec
        self.config = config
        self.base_url = base_url
        self.auth = auth
        self.validator = validator
        self.client = client
        self.request_count = 0
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self._limiter = RateLimiter(config.rate_limit)
        self._endpoints = {e.key: e for e in spec.endpoints}

    async def run(self, cases: Iterable[TestCase]) -> list[Result]:
        """Execute cases concurrently; results come back in input order."""
        return list(await asyncio.gather(*(self.execute(case) for case in cases)))

    async def execute(self, case: TestCase) -> Result:
        endpoint = self._endpoints[case.endpoint]
        async with self._semaphore:
            request = build_request(
                case,
                self.base_url,
                self.auth,
                self.config.headers,
                endpoint_secured=bool(endpoint.security),
            )
            record = record_request(request, self.auth.secret_names, self.config.redact_secrets)
            try:
                response, elapsed_ms = await self._send(request)
            except httpx.TimeoutException as exc:
                failure = self.validator.timeout_failure(case, record, repr(exc))
                return Result(test_case=case, request=record, failures=[failure])
            except httpx.TransportError as exc:
                return Result(test_case=case, request=record, error=f"{type(exc).__name__}: {exc}")
        response_record = _record_response(response, elapsed_ms)
        failures = self.validator.validate(endpoint, case, record, response_record)
        return Result(test_case=case, request=record, response=response_record, failures=failures)

    async def _send(self, request: httpx.Request) -> tuple[httpx.Response, float]:
        attempt = 0
        while True:
            await self._limiter.wait()
            self.request_count += 1
            started = time.perf_counter()
            try:
                response = await self.client.send(request)
                await response.aread()
            except httpx.TimeoutException:
                raise  # a timeout is a finding, not a flaky transport
            except httpx.TransportError:
                if attempt >= self.config.retries:
                    raise
                attempt += 1
                await asyncio.sleep(self.config.retry_backoff * 2 ** (attempt - 1))
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000
            if response.status_code == 429 and attempt < self.config.retries:
                attempt += 1
                await asyncio.sleep(_retry_after(response, self.config.retry_backoff))
                continue
            return response, elapsed_ms


def _record_response(response: httpx.Response, elapsed_ms: float) -> ResponseRecord:
    return ResponseRecord(
        status=response.status_code,
        headers={k.lower(): v for k, v in response.headers.items()},
        body=response.text[:MAX_BODY_CHARS],
        elapsed_ms=elapsed_ms,
    )


def make_client(config: RunConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(config.timeout),
        verify=config.verify_tls,
        follow_redirects=False,  # a redirect could point outside the target
        limits=httpx.Limits(max_connections=config.concurrency),
    )
