"""Ground-truth parsing, failure attribution, and the evaluation table."""

from __future__ import annotations

import re

import pytest

from apitest.evaluation import (
    Condition,
    RequestView,
    attribute,
    depth,
    holds,
    load_bugs,
    render_markdown,
    request_view,
    score,
)
from apitest.models import (
    ApiSpec,
    Failure,
    FailureCategory,
    RequestRecord,
    Strategy,
)
from tests.conftest import ROOT
from tests.helpers import run_buggy

BUGS_MD = ROOT / "examples" / "buggy_api" / "BUGS.md"


def _failure(
    endpoint: str, check: str, url: str, body: str | None = None, status: int = 500
) -> Failure:
    return Failure(
        category=FailureCategory.SERVER_ERROR,
        check=check,
        message="m",
        endpoint=endpoint,
        test_case_id="t",
        strategy=Strategy.NEGATIVE,
        technique="x",
        status=status,
        request=RequestRecord(method=endpoint.split()[0], url=url, body=body),
    )


def test_bugs_md_parses_and_matches_source_tags() -> None:
    bugs = load_bugs(BUGS_MD)
    tagged = set(re.findall(r"BUG-\d\d", (ROOT / "examples/buggy_api/app.py").read_text("utf-8")))
    assert {b.id for b in bugs} == tagged
    assert len(bugs) == 19


def test_request_view_extracts_path_query_body() -> None:
    failure = _failure(
        "GET /users/{user_id}", "server_error", "http://x/users/ab%20c?q=caf%C3%A9&n=1"
    )
    view = request_view(failure)
    assert view.path == {"user_id": "ab c"}
    assert view.query == {"q": "café", "n": "1"}


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (Condition("body.name", "equals", ""), True),
        (Condition("body.description", "is_null"), True),
        (Condition("body.missing", "is_null"), False),
        (Condition("body.price", "lt", 0), True),
        (Condition("body.meta", "depth_gt", 2), True),
        (Condition("body.meta", "depth_gt", 3), False),
        (Condition("query.q", "non_ascii"), True),
        (Condition("query.q", "len_gt", 3), True),
        (Condition("body.name", "regex", "^$"), True),
        (Condition("path.id", "not_int"), True),
    ],
)
def test_conditions(condition: Condition, expected: bool) -> None:
    view = RequestView(
        path={"id": "abc"},
        query={"q": "café"},
        body={"name": "", "description": None, "price": -1, "meta": {"a": {"b": {}}}},
    )
    assert holds(condition, view) is expected


def test_depth() -> None:
    assert depth({}) == 1
    assert depth({"k": {"k": {}}}) == 3
    assert depth(5) == 0


def test_attribution_distinguishes_same_endpoint_bugs() -> None:
    bugs = load_bugs(BUGS_MD)
    url = "http://x/items"
    assert attribute(_failure("POST /items", "server_error", url, '{"name": ""}'), bugs) == [
        "BUG-02"
    ]
    assert attribute(
        _failure("POST /items", "server_error", url, '{"name": "a", "description": null}'), bugs
    ) == ["BUG-12"]
    assert attribute(_failure("POST /items", "server_error", url, '{"name": "{0}"}'), bugs) == [
        "BUG-19"
    ]
    assert attribute(_failure("POST /items", "server_error", url, '{"name": "ok"}'), bugs) == []


@pytest.mark.slow
async def test_score_and_markdown(buggy_spec: ApiSpec) -> None:
    bugs = load_bugs(BUGS_MD)
    report = await run_buggy(buggy_spec, [Strategy.STATEFUL])
    result = score("stateful", report, buggy_spec, bugs)
    assert {"BUG-01", "BUG-04", "BUG-07", "BUG-08", "BUG-10"} <= set(result.found)
    assert result.false_positives == {}
    table = render_markdown([result], bugs)
    assert table.startswith("| Strategy | Bugs found |")
    assert "| BUG-10 | ✅ |" in table
