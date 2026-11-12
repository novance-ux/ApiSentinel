"""Unit tests for each response check and its failure category."""

from __future__ import annotations

import json
from typing import Any

from apitest.models import (
    ApiSpec,
    Expectation,
    FailureCategory,
    RequestRecord,
    ResponseRecord,
    Severity,
    Strategy,
    TestCase,
)
from apitest.spec.registry import SchemaRegistry
from apitest.validation.checks import ResponseValidator


def _validate(
    spec: ApiSpec,
    endpoint_key: str,
    status: int,
    body: Any = None,
    *,
    content_type: str = "application/json",
    expect: Expectation = Expectation.ANY,
    elapsed_ms: float = 5,
    raw: str | None = None,
) -> list[tuple[FailureCategory, str, Severity]]:
    endpoint = spec.endpoint(endpoint_key)
    case = TestCase(
        id="t",
        endpoint=endpoint_key,
        strategy=Strategy.POSITIVE,
        technique="x",
        method=endpoint.method,
        path=endpoint.path,
        expect=expect,
    )
    text = raw if raw is not None else ("" if body is None else json.dumps(body))
    response = ResponseRecord(
        status=status,
        headers={"content-type": content_type} if text else {},
        body=text,
        elapsed_ms=elapsed_ms,
    )
    request = RequestRecord(method=endpoint.method, url="http://x" + endpoint.path)
    validator = ResponseValidator(SchemaRegistry(spec.schema_resources), 1000)
    return [
        (f.category, f.check, f.severity)
        for f in validator.validate(endpoint, case, request, response)
    ]


ITEM = {
    "id": 1,
    "name": "n",
    "sku": "TOY-N00001",
    "price": 1.0,
    "quantity": 1,
    "category": "toys",
    "tags": [],
    "created_at": "2024-01-01T00:00:00Z",
}


def test_valid_response_passes(buggy_spec: ApiSpec) -> None:
    assert _validate(buggy_spec, "GET /items/{item_id}", 200, ITEM) == []


def test_server_error(buggy_spec: ApiSpec) -> None:
    found = _validate(
        buggy_spec,
        "GET /items/{item_id}",
        500,
        raw="Internal Server Error",
        content_type="text/plain",
    )
    assert found == [(FailureCategory.SERVER_ERROR, "server_error", Severity.HIGH)]


def test_undocumented_status(buggy_spec: ApiSpec) -> None:
    found = _validate(buggy_spec, "GET /search", 418, {"detail": "teapot"})
    assert found == [(FailureCategory.UNDOCUMENTED, "status_undocumented", Severity.MEDIUM)]


def test_schema_mismatch(buggy_spec: ApiSpec) -> None:
    item = {k: v for k, v in ITEM.items() if k != "created_at"}
    found = _validate(buggy_spec, "GET /items/{item_id}", 200, item)
    assert found == [(FailureCategory.CONTRACT, "schema_mismatch", Severity.MEDIUM)]


def test_content_type_mismatch(buggy_spec: ApiSpec) -> None:
    found = _validate(buggy_spec, "GET /health", 200, {"status": "ok"}, content_type="text/plain")
    assert found == [(FailureCategory.CONTRACT, "content_type_mismatch", Severity.MEDIUM)]


def test_invalid_json(buggy_spec: ApiSpec) -> None:
    found = _validate(buggy_spec, "GET /health", 200, raw="{not json")
    assert found == [(FailureCategory.CONTRACT, "invalid_json", Severity.HIGH)]


def test_unexpected_body_on_204(buggy_spec: ApiSpec) -> None:
    found = _validate(buggy_spec, "DELETE /items/{item_id}", 204, {"deleted": True})
    assert found == [(FailureCategory.CONTRACT, "unexpected_body", Severity.LOW)]


def test_valid_input_rejected(buggy_spec: ApiSpec) -> None:
    found = _validate(buggy_spec, "POST /items", 422, {"detail": "no"}, expect=Expectation.SUCCESS)
    assert found == [(FailureCategory.CONTRACT, "valid_input_rejected", Severity.MEDIUM)]
    assert (
        _validate(
            buggy_spec, "GET /items/{item_id}", 404, {"detail": "x"}, expect=Expectation.SUCCESS
        )
        == []
    )


def test_invalid_input_accepted(buggy_spec: ApiSpec) -> None:
    found = _validate(buggy_spec, "POST /items", 201, dict(ITEM), expect=Expectation.CLIENT_ERROR)
    assert found == [(FailureCategory.CONTRACT, "invalid_input_accepted", Severity.MEDIUM)]


def test_auth_not_enforced(buggy_spec: ApiSpec) -> None:
    found = _validate(buggy_spec, "DELETE /items/{item_id}", 204, expect=Expectation.UNAUTHORIZED)
    assert found == [(FailureCategory.SECURITY, "auth_not_enforced", Severity.HIGH)]
    found = _validate(
        buggy_spec, "DELETE /items/{item_id}", 404, {"detail": "x"}, expect=Expectation.UNAUTHORIZED
    )
    assert found == [(FailureCategory.SECURITY, "auth_not_enforced", Severity.LOW)]
    assert (
        _validate(
            buggy_spec,
            "DELETE /items/{item_id}",
            401,
            {"detail": "x"},
            expect=Expectation.UNAUTHORIZED,
        )
        == []
    )


def test_latency(buggy_spec: ApiSpec) -> None:
    found = _validate(buggy_spec, "GET /items/{item_id}", 200, ITEM, elapsed_ms=1500)
    assert found == [(FailureCategory.PERFORMANCE, "latency", Severity.MEDIUM)]


def test_stack_trace_leak(buggy_spec: ApiSpec) -> None:
    body = {"detail": 'Traceback (most recent call last):\n  File "app.py", line 3, in f'}
    found = _validate(buggy_spec, "GET /items/{item_id}", 404, body)
    assert found == [(FailureCategory.SECURITY, "stack_trace_leak", Severity.HIGH)]


def test_java_stack_trace(buggy_spec: ApiSpec) -> None:
    raw = "java.lang.NullPointerException\n\tat com.acme.Foo.bar(Foo.java:42)"
    found = _validate(buggy_spec, "GET /items/{item_id}", 500, raw=raw, content_type="text/plain")
    assert (FailureCategory.SECURITY, "stack_trace_leak", Severity.HIGH) in found
