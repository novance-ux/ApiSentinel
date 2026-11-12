"""Machine-readable JSON report."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from apitest.models import RunReport


def report_to_dict(report: RunReport) -> dict[str, Any]:
    groups = report.groups()
    summary = {
        "test_cases": len(report.results),
        "with_findings": sum(1 for r in report.results if r.failures),
        "errors": len(report.errors),
        "requests": report.request_count,
        "unique_findings": len(groups),
        "findings_by_category": dict(Counter(g.example.category.value for g in groups)),
        "findings_by_strategy": dict(Counter(s.value for g in groups for s in g.strategies)),
    }
    return {
        "tool": {"name": "apitest", "version": report.tool_version},
        "run": {
            "spec_title": report.spec_title,
            "spec_source": report.spec_source,
            "base_url": report.base_url,
            "seed": report.seed,
            "strategies": [s.value for s in report.strategies],
            "started_at": report.started_at,
            "duration_s": report.duration_s,
            "executed_cases": report.executed_cases,
        },
        "summary": summary,
        "findings": [
            {
                "endpoint": g.key[0],
                "status": g.key[1],
                "check": g.key[2],
                "count": g.count,
                "strategies": [s.value for s in g.strategies],
                "example": g.example.model_dump(mode="json"),
            }
            for g in groups
        ],
        "results": [r.model_dump(mode="json") for r in report.results],
        "skipped": report.skipped,
        "notes": report.notes,
    }


def render_json(report: RunReport) -> str:
    return json.dumps(report_to_dict(report), indent=2, ensure_ascii=False)
