"""Integration: run positive + security strategies in-process against the buggy app."""

from __future__ import annotations

import pytest

from apitest.models import ApiSpec, RunReport, Strategy
from tests.helpers import found, run_buggy

pytestmark = pytest.mark.slow


async def test_positive_and_security_find_known_bugs(buggy_spec: ApiSpec) -> None:
    report = await run_buggy(buggy_spec, [Strategy.POSITIVE, Strategy.SECURITY])
    assert report.errors == []
    assert found(report, "POST /users", "status_undocumented")  # BUG-09
    assert found(report, "GET /health", "content_type_mismatch")  # BUG-17
    assert found(report, "DELETE /items/{item_id}", "auth_not_enforced")  # BUG-07
    probes = [r for r in report.results if r.test_case.strategy == Strategy.SECURITY]
    assert len(probes) == 10  # every endpoint except /health requires auth


async def test_same_seed_replays_identical_requests(buggy_spec: ApiSpec) -> None:
    first = await run_buggy(buggy_spec, [Strategy.POSITIVE])
    second = await run_buggy(buggy_spec, [Strategy.POSITIVE])
    other = await run_buggy(buggy_spec, [Strategy.POSITIVE], seed=99)

    def signature(report: RunReport) -> list[tuple[str, str, str | None]]:
        return [(r.request.method, r.request.url, r.request.body) for r in report.results]

    assert signature(first) == signature(second)
    assert signature(first) != signature(other)
