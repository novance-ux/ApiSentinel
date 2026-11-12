"""Turn TestCases into httpx requests aimed only at the configured base URL."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from apitest.execution.auth import AuthMaterial
from apitest.models import RequestRecord, TestCase

REDACTED = "<redacted>"
QueryItems = list[tuple[str, str | int | float | bool | None]]


class UnsafeTargetError(RuntimeError):
    """A request would leave the target base URL. Never sent."""


def serialize_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def expand_path(template: str, params: dict[str, Any]) -> str:
    path = template
    for name, value in params.items():
        if isinstance(value, list):
            encoded = ",".join(quote(serialize_scalar(v), safe="") for v in value)
        else:
            encoded = quote(serialize_scalar(value), safe="")
        path = path.replace("{" + name + "}", encoded)
    return path


def query_items(query: dict[str, Any]) -> QueryItems:
    items: QueryItems = []
    for name, value in query.items():
        values = value if isinstance(value, list) else [value]
        items.extend((name, serialize_scalar(v)) for v in values)
    return items


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def ensure_same_origin(url: str, base_url: str) -> None:
    if _origin(url) != _origin(base_url):
        raise UnsafeTargetError(f"refusing to send a request to {url} (target is {base_url})")


def _encode_body(case: TestCase) -> tuple[bytes | None, str | None]:
    if case.raw_body is not None:
        return case.raw_body.encode("utf-8", "surrogatepass"), case.content_type
    if not case.has_body:
        return None, None
    content_type = case.content_type or "application/json"
    if content_type.startswith("application/x-www-form-urlencoded") and isinstance(case.body, dict):
        encoded = httpx.QueryParams(query_items(case.body))
        return str(encoded).encode("utf-8"), content_type
    text = json.dumps(case.body, ensure_ascii=False)
    return text.encode("utf-8", "surrogatepass"), content_type


def build_request(
    case: TestCase,
    base_url: str,
    auth: AuthMaterial,
    extra_headers: dict[str, str],
    *,
    endpoint_secured: bool = True,
) -> httpx.Request:
    url = base_url.rstrip("/") + expand_path(case.path, case.path_params)
    params = query_items(case.query)
    headers = {**extra_headers, **case.headers}
    cookies = dict(case.cookies)
    if case.authenticate and endpoint_secured:
        params += list(auth.query.items())
        headers.update(auth.headers)
        cookies.update(auth.cookies)
    content, content_type = _encode_body(case)
    if content_type:
        headers["Content-Type"] = content_type
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    request = httpx.Request(case.method, url, params=params, headers=headers, content=content)
    ensure_same_origin(str(request.url), base_url)
    return request


def record_request(request: httpx.Request, secrets: set[str], redact: bool) -> RequestRecord:
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        secret = name.lower() in secrets or name.lower() in ("authorization", "cookie")
        headers[name] = REDACTED if redact and secret and value else value
    url = request.url
    if redact and secrets:
        params = [(k, REDACTED if k.lower() in secrets else v) for k, v in url.params.multi_items()]
        url = url.copy_with(params=params) if params else url
    body = request.content.decode("utf-8", "replace") if request.content else None
    return RequestRecord(method=request.method, url=str(url), headers=headers, body=body)
