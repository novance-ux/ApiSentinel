"""Self-contained HTML report rendered with Jinja2."""

from __future__ import annotations

import json

from jinja2 import Environment, PackageLoader, select_autoescape

from apitest.models import FailureCategory, RunReport, Severity
from apitest.reporting.terminal import CATEGORY_TITLES

MAX_BODY = 8000


def _pretty(body: str | None) -> str:
    if not body:
        return ""
    try:
        text = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
    except ValueError:
        text = body
    return text if len(text) <= MAX_BODY else text[:MAX_BODY] + "\n… (truncated)"


def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("apitest.reporting", "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pretty"] = _pretty
    return env


def render_html(report: RunReport) -> str:
    groups = report.groups()
    severity_order = list(Severity)
    sections = []
    for category, title in CATEGORY_TITLES.items():
        members = [g for g in groups if g.example.category == category]
        members.sort(key=lambda g: (severity_order.index(g.example.severity), g.key[0]))
        sections.append({"category": category.value, "title": title, "groups": members})
    template = _environment().get_template("report.html.j2")
    return template.render(
        report=report,
        sections=sections,
        total_cases=len(report.results),
        with_findings=sum(1 for r in report.results if r.failures),
        unique=len(groups),
        categories=FailureCategory,
    )
