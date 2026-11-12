"""Negative and boundary case generation, and what they find in the buggy app."""

from __future__ import annotations

import pytest

from apitest.generation.cases import CaseFactory
from apitest.generation.negative import boundary_cases, negative_cases
from apitest.models import ApiSpec, Expectation, Strategy, TestCase
from apitest.spec.registry import SchemaRegistry
from tests.helpers import found, run_buggy


def _factory(spec: ApiSpec, seed: int = 0) -> CaseFactory:
    return CaseFactory(spec, SchemaRegistry(spec.schema_resources), seed)


def _by_technique(cases: list[TestCase]) -> dict[str, TestCase]:
    return {case.technique: case for case in cases}


def test_negative_body_cases(buggy_spec: ApiSpec) -> None:
    cases = _by_technique(negative_cases(_factory(buggy_spec), buggy_spec.endpoint("POST /items")))
    expected_invalid = [
        "missing_body",
        "malformed_json",
        "body_not_object",
        "missing_required:name",
        "wrong_type:price",
        "null:name",
        "invalid_enum:category",
    ]
    for technique in expected_invalid:
        assert cases[technique].expect == Expectation.CLIENT_ERROR, technique
    assert cases["null:description"].expect == Expectation.SUCCESS  # nullable: true
    assert cases["malformed_json"].raw_body == '{"truncated": '
    assert cases["wrong_type:price"].body["price"] == "not-a-number"
    assert "name" not in cases["missing_required:name"].body


def test_negative_mass_assignment_pattern_and_format(buggy_spec: ApiSpec) -> None:
    cases = _by_technique(negative_cases(_factory(buggy_spec), buggy_spec.endpoint("POST /users")))
    assert cases["unknown_field:is_admin"].body["is_admin"] is True
    assert cases["unknown_field:is_admin"].expect == Expectation.CLIENT_ERROR
    assert cases["pattern_violation:username"].expect == Expectation.CLIENT_ERROR
    assert cases["format_violation:email"].body["email"] == "not-a-email"


def test_negative_parameter_cases(buggy_spec: ApiSpec) -> None:
    factory = _factory(buggy_spec)
    user = _by_technique(negative_cases(factory, buggy_spec.endpoint("GET /users/{user_id}")))
    assert user["wrong_type_param:path.user_id"].path_params == {"user_id": "abc"}
    search = _by_technique(negative_cases(factory, buggy_spec.endpoint("GET /search")))
    assert "q" not in search["missing_required_param:query.q"].query


def test_boundary_cases(buggy_spec: ApiSpec) -> None:
    factory = _factory(buggy_spec)
    items = _by_technique(boundary_cases(factory, buggy_spec.endpoint("POST /items")))
    assert items["boundary:quantity=max"].body["quantity"] == 10_000
    assert items["boundary:quantity=max"].expect == Expectation.SUCCESS
    assert items["boundary:quantity=max+1"].expect == Expectation.CLIENT_ERROR
    assert items["boundary:price=min-1"].body["price"] == pytest.approx(-0.01)
    assert items["boundary:price=min-1"].expect == Expectation.CLIENT_ERROR
    assert items["boundary:name=min_length-1"].body["name"] == ""
    assert len(items["boundary:name=max_length"].body["name"]) == 100
    assert items["boundary:tags=max_items+1"].expect == Expectation.CLIENT_ERROR
    search = _by_technique(boundary_cases(factory, buggy_spec.endpoint("GET /search")))
    assert len(search["boundary:query.q=max_length"].query["q"]) == 200
    assert search["boundary:query.q=max_length"].expect == Expectation.SUCCESS
    pages = _by_technique(boundary_cases(factory, buggy_spec.endpoint("GET /items")))
    assert pages["boundary:query.page_size=max+1"].query["page_size"] == 101


def test_cases_are_tagged_and_deterministic(buggy_spec: ApiSpec) -> None:
    def build(seed: int) -> list[TestCase]:
        factory = _factory(buggy_spec, seed)
        cases: list[TestCase] = []
        for endpoint in buggy_spec.endpoints:
            cases += negative_cases(factory, endpoint) + boundary_cases(factory, endpoint)
        return cases

    first, second = build(3), build(3)
    assert [c.model_dump() for c in first] == [c.model_dump() for c in second]
    assert len({c.id for c in first}) == len(first)
    assert {c.strategy for c in first} == {Strategy.NEGATIVE, Strategy.BOUNDARY}
    assert all(c.technique for c in first)


def test_works_on_other_specs(petstore_spec: ApiSpec, library_spec: ApiSpec) -> None:
    for spec in (petstore_spec, library_spec):
        factory = _factory(spec)
        for endpoint in spec.endpoints:
            negative_cases(factory, endpoint)
            boundary_cases(factory, endpoint)
        assert factory.skipped == []


@pytest.mark.slow
async def test_negative_and_boundary_find_known_bugs(buggy_spec: ApiSpec) -> None:
    report = await run_buggy(buggy_spec, [Strategy.NEGATIVE, Strategy.BOUNDARY])
    assert report.errors == []
    empty_name = found(report, "POST /items", "server_error")
    assert any(f.technique == "boundary:name=min_length-1" for f in empty_name)  # BUG-02
    assert any(f.technique == "null:description" for f in empty_name)  # BUG-12
    accepted = found(report, "POST /items", "invalid_input_accepted")
    assert any(f.technique == "boundary:price=min-1" for f in accepted)  # BUG-03
    assert any(
        f.status == 418 for f in found(report, "GET /search", "status_undocumented")
    )  # BUG-05
    assert found(report, "GET /users/{user_id}", "stack_trace_leak")  # BUG-06
    rejected = found(report, "POST /items", "valid_input_rejected")
    assert any(f.technique == "boundary:quantity=max" for f in rejected)  # BUG-11
    assert found(report, "POST /users", "invalid_input_accepted")  # BUG-14
    assert found(report, "GET /items", "latency")  # BUG-15
    strategies = {f.strategy for f in report.failures}
    assert strategies == {Strategy.NEGATIVE, Strategy.BOUNDARY}
