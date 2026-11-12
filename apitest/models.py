"""Pydantic models shared across apitest: the normalized spec, test cases, results, failures."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

HttpMethod = Literal["GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"]
ParameterLocation = Literal["path", "query", "header", "cookie"]
JsonSchema = dict[str, Any]


# --------------------------------------------------------------------------- spec model


class Parameter(BaseModel):
    name: str
    location: ParameterLocation
    required: bool = False
    json_schema: JsonSchema = Field(default_factory=dict)
    description: str | None = None


class RequestBody(BaseModel):
    required: bool = False
    content_type: str
    content_types: list[str] = Field(default_factory=list)
    json_schema: JsonSchema | None = None


class Link(BaseModel):
    name: str
    operation_id: str | None = None
    operation_ref: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class ResponseSpec(BaseModel):
    status: str
    description: str = ""
    content_types: list[str] = Field(default_factory=list)
    json_schema: JsonSchema | None = None
    links: list[Link] = Field(default_factory=list)


class SecurityScheme(BaseModel):
    name: str
    type: str
    location: str | None = None
    parameter_name: str | None = None
    scheme: str | None = None


class Endpoint(BaseModel):
    method: HttpMethod
    path: str
    operation_id: str | None = None
    summary: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    parameters: list[Parameter] = Field(default_factory=list)
    request_body: RequestBody | None = None
    responses: dict[str, ResponseSpec] = Field(default_factory=dict)
    security: list[dict[str, list[str]]] = Field(default_factory=list)
    deprecated: bool = False

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def requires_auth(self) -> bool:
        """True when every security alternative needs at least one scheme."""
        return bool(self.security) and all(self.security)

    def params_in(self, location: ParameterLocation) -> list[Parameter]:
        return [param for param in self.parameters if param.location == location]

    def response_for(self, status: int) -> ResponseSpec | None:
        """The documented response for a status: exact code, then `NXX` range, then default."""
        for key in (str(status), f"{status // 100}XX", "default"):
            for candidate, spec in self.responses.items():
                if candidate.upper() == key.upper():
                    return spec
        return None

    def success_statuses(self) -> list[int]:
        return sorted(int(code) for code in self.responses if code.isdigit() and code[0] == "2")


class ApiSpec(BaseModel):
    title: str
    version: str
    openapi_version: str
    source: str
    servers: list[str] = Field(default_factory=list)
    endpoints: list[Endpoint] = Field(default_factory=list)
    security_schemes: dict[str, SecurityScheme] = Field(default_factory=dict)
    schema_resources: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    def endpoint(self, key: str) -> Endpoint:
        for endpoint in self.endpoints:
            if endpoint.key == key:
                return endpoint
        raise KeyError(key)

    def by_operation_id(self, operation_id: str) -> Endpoint | None:
        return next((e for e in self.endpoints if e.operation_id == operation_id), None)


# --------------------------------------------------------------------------- test execution


class Strategy(StrEnum):
    POSITIVE = "positive"
    SECURITY = "security"
    NEGATIVE = "negative"
    BOUNDARY = "boundary"
    STATEFUL = "stateful"
    LLM = "llm"


class Expectation(StrEnum):
    """What a test case expects the API to do with the request."""

    SUCCESS = "success"  # request is valid: expect 2xx (or 404 for unknown ids)
    CLIENT_ERROR = "client_error"  # request is invalid: expect 4xx
    UNAUTHORIZED = "unauthorized"  # credentials withheld: expect 401/403
    ANY = "any"  # no expectation beyond the generic checks


class NoBody(BaseModel):
    """Sentinel type: the request carries no body at all."""


class TestCase(BaseModel):
    __test__ = False  # not a pytest class

    id: str
    endpoint: str
    strategy: Strategy
    technique: str
    description: str = ""
    method: HttpMethod
    path: str
    path_params: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    has_body: bool = False
    content_type: str | None = None
    raw_body: str | None = None
    expect: Expectation = Expectation.ANY
    authenticate: bool = True


class RequestRecord(BaseModel):
    method: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None


class ResponseRecord(BaseModel):
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    elapsed_ms: float = 0.0

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


class FailureCategory(StrEnum):
    CONTRACT = "contract_violation"
    SERVER_ERROR = "server_error"
    UNDOCUMENTED = "undocumented_response"
    PERFORMANCE = "performance"
    SECURITY = "security_smell"


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Failure(BaseModel):
    category: FailureCategory
    check: str
    message: str
    severity: Severity = Severity.MEDIUM
    endpoint: str
    test_case_id: str
    strategy: Strategy
    technique: str
    status: int | None = None
    request: RequestRecord
    response: ResponseRecord | None = None
    curl: str = ""
    shrunk: bool = False

    @property
    def dedupe_key(self) -> tuple[str, int | None, str]:
        return (self.endpoint, self.status, self.check)


class Result(BaseModel):
    test_case: TestCase
    request: RequestRecord
    response: ResponseRecord | None = None
    error: str | None = None
    failures: list[Failure] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.error is None and not self.failures


class FailureGroup(BaseModel):
    """Failures sharing (endpoint, status, check); `example` is the first (or shrunk) one."""

    key: tuple[str, int | None, str]
    example: Failure
    count: int
    strategies: list[Strategy]


class RunReport(BaseModel):
    tool_version: str
    spec_title: str
    spec_source: str
    base_url: str
    seed: int
    strategies: list[Strategy]
    started_at: str
    duration_s: float = 0.0
    request_count: int = 0
    results: list[Result] = Field(default_factory=list)
    extra_failures: list[Failure] = Field(default_factory=list)
    executed_cases: dict[str, int] = Field(default_factory=dict)
    skipped: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def failures(self) -> list[Failure]:
        return [f for r in self.results for f in r.failures] + self.extra_failures

    @property
    def errors(self) -> list[Result]:
        return [r for r in self.results if r.error]

    def groups(self) -> list[FailureGroup]:
        grouped: dict[tuple[str, int | None, str], FailureGroup] = {}
        for failure in self.failures:
            group = grouped.get(failure.dedupe_key)
            if group is None:
                grouped[failure.dedupe_key] = FailureGroup(
                    key=failure.dedupe_key, example=failure, count=1, strategies=[failure.strategy]
                )
                continue
            group.count += 1
            if failure.strategy not in group.strategies:
                group.strategies.append(failure.strategy)
            if failure.shrunk and not group.example.shrunk:
                group.example = failure
        return list(grouped.values())
