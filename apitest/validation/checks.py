"""Response validators. Each check inspects one response and emits classified Failures.

Categories (FailureCategory): contract violation, server error, undocumented response,
performance, security smell. Every check has a stable name (`Failure.check`) so reports
and the evaluation script can tell which rule fired.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from apitest.models import (
    Endpoint,
    Expectation,
    Failure,
    FailureCategory,
    RequestRecord,
    ResponseRecord,
    ResponseSpec,
    Severity,
    TestCase,
)
from apitest.reporting.curl import to_curl
from apitest.spec.endpoints import is_json_media_type
from apitest.spec.registry import SchemaRegistry

STACK_TRACE_PATTERNS = [
    re.compile(p)
    for p in (
        r"Traceback \(most recent call last\)",
        r'File "[^"]+\.py", line \d+',
        r"\bat [\w$.]+\([\w$]+\.java:\d+\)",
        r"Exception in thread \"",
        r"\bat [\w.<>]+\(.*\) in .+:line \d+",  # .NET
        r"System\.[A-Za-z]+Exception",
        r"\bpanic: .+\n\s*goroutine \d+",  # Go
        r"(?:Fatal error|Warning): .+ in /.+\.php on line \d+",
        r"\bat .+ \((?:/|[A-Z]:\\).+\.js:\d+:\d+\)",  # Node
    )
]
# Statuses that a schema-valid request may legitimately get: unknown ids, conflicts, auth.
_TOLERATED_FOR_VALID = frozenset({401, 403, 404, 409, 410, 429})


@dataclass(frozen=True)
class CheckContext:
    endpoint: Endpoint
    case: TestCase
    request: RequestRecord
    response: ResponseRecord
    registry: SchemaRegistry
    latency_threshold_ms: float

    def failure(
        self, category: FailureCategory, check: str, message: str, severity: Severity
    ) -> Failure:
        return make_failure(
            self.case, self.request, self.response, category, check, message, severity
        )


def make_failure(
    case: TestCase,
    request: RequestRecord,
    response: ResponseRecord | None,
    category: FailureCategory,
    check: str,
    message: str,
    severity: Severity,
) -> Failure:
    return Failure(
        category=category,
        check=check,
        message=message,
        severity=severity,
        endpoint=case.endpoint,
        test_case_id=case.id,
        strategy=case.strategy,
        technique=case.technique,
        status=response.status if response else None,
        request=request,
        response=response,
        curl=to_curl(request),
    )


Check = Callable[[CheckContext], list[Failure]]


def check_server_error(ctx: CheckContext) -> list[Failure]:
    if ctx.response.status < 500:
        return []
    return [
        ctx.failure(
            FailureCategory.SERVER_ERROR,
            "server_error",
            f"server error {ctx.response.status} ({ctx.case.technique})",
            Severity.HIGH,
        )
    ]


def check_documented_status(ctx: CheckContext) -> list[Failure]:
    status = ctx.response.status
    if status >= 500 or ctx.endpoint.response_for(status) is not None:
        return []
    documented = ", ".join(ctx.endpoint.responses) or "none"
    return [
        ctx.failure(
            FailureCategory.UNDOCUMENTED,
            "status_undocumented",
            f"status {status} is not documented (documented: {documented})",
            Severity.MEDIUM,
        )
    ]


def _content_type_matches(actual: str, documented: list[str]) -> bool:
    for media_type in documented:
        expected = media_type.split(";")[0].strip().lower()
        if expected in ("*/*", actual):
            return True
        if expected.endswith("/*") and actual.startswith(expected[:-1]):
            return True
    return False


def check_content(ctx: CheckContext) -> list[Failure]:
    """Content type and JSON schema conformance for documented responses."""
    spec = ctx.endpoint.response_for(ctx.response.status)
    if spec is None or ctx.response.status >= 500:
        return []
    if not spec.content_types:
        return _check_unexpected_body(ctx)
    if not ctx.response.body:
        return []
    actual = ctx.response.content_type
    if not _content_type_matches(actual, spec.content_types):
        message = f"content type {actual or '<none>'} not in documented {spec.content_types}"
        return [
            ctx.failure(FailureCategory.CONTRACT, "content_type_mismatch", message, Severity.MEDIUM)
        ]
    if spec.json_schema is None or not is_json_media_type(actual):
        return []
    return _check_json_body(ctx, spec)


def _check_unexpected_body(ctx: CheckContext) -> list[Failure]:
    if not ctx.response.body.strip():
        return []
    return [
        ctx.failure(
            FailureCategory.CONTRACT,
            "unexpected_body",
            f"status {ctx.response.status} documents no content but a body was returned",
            Severity.LOW,
        )
    ]


def _check_json_body(ctx: CheckContext, spec: ResponseSpec) -> list[Failure]:
    try:
        body: Any = json.loads(ctx.response.body)
    except json.JSONDecodeError as exc:
        return [
            ctx.failure(
                FailureCategory.CONTRACT, "invalid_json", f"body is not JSON: {exc}", Severity.HIGH
            )
        ]
    errors = ctx.registry.errors(spec.json_schema, body, limit=3)
    if not errors:
        return []
    return [
        ctx.failure(
            FailureCategory.CONTRACT,
            "schema_mismatch",
            "response does not match schema: " + "; ".join(errors),
            Severity.MEDIUM,
        )
    ]


def check_expectation(ctx: CheckContext) -> list[Failure]:
    status = ctx.response.status
    expect = ctx.case.expect
    if expect == Expectation.SUCCESS and 400 <= status < 500 and status not in _TOLERATED_FOR_VALID:
        return [
            ctx.failure(
                FailureCategory.CONTRACT,
                "valid_input_rejected",
                f"schema-valid request rejected with {status} ({ctx.case.technique})",
                Severity.MEDIUM,
            )
        ]
    if expect == Expectation.CLIENT_ERROR and 200 <= status < 300:
        return [
            ctx.failure(
                FailureCategory.CONTRACT,
                "invalid_input_accepted",
                f"invalid request accepted with {status} ({ctx.case.technique})",
                Severity.MEDIUM,
            )
        ]
    if expect == Expectation.UNAUTHORIZED and status not in (401, 403) and status < 500:
        processed = 200 <= status < 300
        message = (
            f"request without credentials succeeded with {status}"
            if processed
            else f"request without credentials was processed before auth (got {status})"
        )
        severity = Severity.HIGH if processed else Severity.LOW
        return [ctx.failure(FailureCategory.SECURITY, "auth_not_enforced", message, severity)]
    return []


def check_latency(ctx: CheckContext) -> list[Failure]:
    elapsed = ctx.response.elapsed_ms
    if elapsed <= ctx.latency_threshold_ms:
        return []
    message = f"took {elapsed:.0f} ms (threshold {ctx.latency_threshold_ms:.0f} ms)"
    return [ctx.failure(FailureCategory.PERFORMANCE, "latency", message, Severity.MEDIUM)]


def check_stack_trace(ctx: CheckContext) -> list[Failure]:
    body = ctx.response.body
    pattern = next((p for p in STACK_TRACE_PATTERNS if p.search(body)), None)
    if pattern is None:
        return []
    return [
        ctx.failure(
            FailureCategory.SECURITY,
            "stack_trace_leak",
            f"response body leaks a stack trace (matched {pattern.pattern!r})",
            Severity.HIGH,
        )
    ]


DEFAULT_CHECKS: tuple[Check, ...] = (
    check_server_error,
    check_documented_status,
    check_content,
    check_expectation,
    check_latency,
    check_stack_trace,
)


class ResponseValidator:
    def __init__(
        self,
        registry: SchemaRegistry,
        latency_threshold_ms: float,
        checks: tuple[Check, ...] = DEFAULT_CHECKS,
    ) -> None:
        self.registry = registry
        self.latency_threshold_ms = latency_threshold_ms
        self.checks = checks

    def validate(
        self, endpoint: Endpoint, case: TestCase, request: RequestRecord, response: ResponseRecord
    ) -> list[Failure]:
        ctx = CheckContext(
            endpoint, case, request, response, self.registry, self.latency_threshold_ms
        )
        return [failure for check in self.checks for failure in check(ctx)]

    def timeout_failure(self, case: TestCase, request: RequestRecord, error: str) -> Failure:
        return make_failure(
            case,
            request,
            None,
            FailureCategory.PERFORMANCE,
            "timeout",
            f"request timed out: {error}",
            Severity.HIGH,
        )
