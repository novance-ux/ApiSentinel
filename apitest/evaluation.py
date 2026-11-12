"""Score apitest findings against a ground-truth bug list (examples/buggy_api/BUGS.md).

BUGS.md holds a fenced ```yaml block with one matcher list per bug. A failure matches a bug
when it satisfies every key of any one matcher entry (endpoint, checks, status, request
conditions). Failures matching no bug are reported as false-positive candidates.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import yaml

from apitest.models import ApiSpec, Failure, RunReport

_YAML_BLOCK = re.compile(r"```yaml\n(.*?)```", re.S)


# --------------------------------------------------------------------------- ground truth


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: Any = None


@dataclass(frozen=True)
class Matcher:
    endpoints: tuple[str, ...]
    checks: tuple[str, ...] = ()
    statuses: tuple[int, ...] = ()
    request: tuple[Condition, ...] = ()
    message: str | None = None


@dataclass(frozen=True)
class Bug:
    id: str
    matchers: tuple[Matcher, ...]


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    return tuple(value) if isinstance(value, list) else (value,)


def parse_bugs(markdown: str) -> list[Bug]:
    match = _YAML_BLOCK.search(markdown)
    if match is None:
        raise ValueError("no ```yaml block with bug matchers found")
    data = yaml.safe_load(match.group(1))
    bugs = []
    for entry in data["bugs"]:
        matchers = tuple(
            Matcher(
                endpoints=_as_tuple(m["endpoint"]),
                checks=_as_tuple(m.get("checks")),
                statuses=_as_tuple(m.get("status")),
                request=tuple(Condition(**c) for c in m.get("request") or []),
                message=m.get("message"),
            )
            for m in entry["match"]
        )
        bugs.append(Bug(entry["id"], matchers))
    return bugs


def load_bugs(path: Path) -> list[Bug]:
    return parse_bugs(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- request view


@dataclass
class RequestView:
    """The parts of a recorded request that matcher conditions can refer to."""

    path: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: Any = None

    def lookup(self, dotted: str) -> tuple[bool, Any]:
        source, _, rest = dotted.partition(".")
        if source == "path":
            return (rest in self.path, self.path.get(rest))
        if source == "query":
            return (rest in self.query, self.query.get(rest))
        node = self.body
        for key in rest.split("."):
            if not isinstance(node, dict) or key not in node:
                return (False, None)
            node = node[key]
        return (True, node)


def request_view(failure: Failure) -> RequestView:
    template = failure.endpoint.split(" ", 1)[1]
    url = urlsplit(failure.request.url)
    pattern = "^" + re.sub(r"\\\{([^}]+)\\\}", r"(?P<\1>[^/]*)", re.escape(template)) + "$"
    found = re.match(pattern, url.path)
    path = {k: unquote(v) for k, v in found.groupdict().items()} if found else {}
    try:
        body = json.loads(failure.request.body) if failure.request.body else None
    except ValueError:
        body = None
    return RequestView(path, dict(parse_qsl(url.query, keep_blank_values=True)), body)


def depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((depth(v) for v in value), default=0)
    return 0


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def holds(condition: Condition, view: RequestView) -> bool:
    present, value = view.lookup(condition.field)
    if not present:
        return False
    op, expected = condition.op, condition.value
    if op == "equals":
        return bool(value == expected or (isinstance(value, str) and value == str(expected)))
    if op == "is_null":
        return value is None
    if op in ("lt", "gt"):
        number = _number(value)
        if number is None:
            return False
        return bool(number < expected if op == "lt" else number > expected)
    if op == "len_gt":
        return hasattr(value, "__len__") and len(value) > expected
    if op == "depth_gt":
        return bool(depth(value) > expected)
    if op == "non_ascii":
        return isinstance(value, str) and not value.isascii()
    if op == "regex":
        return isinstance(value, str) and re.search(expected, value) is not None
    if op == "not_int":
        return isinstance(value, str) and not value.lstrip("-").isdigit()
    raise ValueError(f"unknown condition op {op!r}")


def matches(matcher: Matcher, failure: Failure) -> bool:
    if failure.endpoint not in matcher.endpoints:
        return False
    if matcher.checks and failure.check not in matcher.checks:
        return False
    if matcher.statuses and failure.status not in matcher.statuses:
        return False
    if matcher.message and not re.search(matcher.message, failure.message):
        return False
    if matcher.request:
        view = request_view(failure)
        return all(holds(c, view) for c in matcher.request)
    return True


def attribute(failure: Failure, bugs: list[Bug]) -> list[str]:
    return [bug.id for bug in bugs if any(matches(m, failure) for m in bug.matchers)]


# --------------------------------------------------------------------------- scoring


@dataclass
class Score:
    label: str
    found: list[str]
    false_positives: dict[tuple[str, int | None, str], Failure]
    unique_findings: int
    endpoints_hit: int
    endpoints_total: int
    statuses_hit: int
    statuses_total: int
    requests: int
    duration_s: float


def _documented_pairs(spec: ApiSpec) -> set[tuple[str, str]]:
    return {(e.key, code) for e in spec.endpoints for code in e.responses if code.isdigit()}


def score(label: str, report: RunReport, spec: ApiSpec, bugs: list[Bug]) -> Score:
    found: set[str] = set()
    false_positives: dict[tuple[str, int | None, str], Failure] = {}
    for failure in report.failures:
        ids = attribute(failure, bugs)
        found.update(ids)
        if not ids:
            false_positives.setdefault(failure.dedupe_key, failure)
    documented = _documented_pairs(spec)
    observed = {
        (r.test_case.endpoint, str(r.response.status)) for r in report.results if r.response
    }
    order = [bug.id for bug in bugs]
    return Score(
        label=label,
        found=sorted(found, key=order.index),
        false_positives=false_positives,
        unique_findings=len(report.groups()),
        endpoints_hit=len({r.test_case.endpoint for r in report.results if r.response}),
        endpoints_total=len(spec.endpoints),
        statuses_hit=len(documented & observed),
        statuses_total=len(documented),
        requests=report.request_count,
        duration_s=report.duration_s,
    )


# --------------------------------------------------------------------------- markdown


def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.0f}%)" if whole else "-"


def render_markdown(scores: list[Score], bugs: list[Bug]) -> str:
    total = len(bugs)
    lines = [
        "| Strategy | Bugs found | Unique findings | False positives | Endpoint coverage "
        "| Documented status coverage | Requests |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        lines.append(
            f"| {s.label} | {_pct(len(s.found), total)} | {s.unique_findings} "
            f"| {len(s.false_positives)} | {_pct(s.endpoints_hit, s.endpoints_total)} "
            f"| {_pct(s.statuses_hit, s.statuses_total)} | {s.requests} |"
        )
    lines += ["", "| Bug | " + " | ".join(s.label for s in scores) + " |"]
    lines.append("|---|" + "---|" * len(scores))
    for bug in bugs:
        cells = ["✅" if bug.id in s.found else "·" for s in scores]
        lines.append(f"| {bug.id} | " + " | ".join(cells) + " |")
    fps = [(s.label, f) for s in scores for f in s.false_positives.values()]
    if fps:
        lines += ["", "False-positive candidates (matched no injected bug):", ""]
        seen: set[tuple[str, int | None, str]] = set()
        for label, failure in fps:
            if failure.dedupe_key in seen:
                continue
            seen.add(failure.dedupe_key)
            where = f"`{failure.endpoint}` {failure.status} `{failure.check}`"
            lines.append(f"- {where} ({label}): {failure.message}")
    return "\n".join(lines) + "\n"
