"""HTML, JSON, JUnit reporters and the --format/--output CLI flags."""

from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from typer.testing import CliRunner

import apitest.session
from apitest.cli import app
from apitest.models import (
    ApiSpec,
    Expectation,
    Failure,
    FailureCategory,
    RequestRecord,
    ResponseRecord,
    Result,
    RunReport,
    Severity,
    Strategy,
    TestCase,
)
from apitest.reporting.html import render_html
from apitest.reporting.json_report import render_json
from apitest.reporting.junit import render_junit
from apitest.reporting.writers import ReportFormat, output_paths
from tests.conftest import SPECS
from tests.helpers import BASE_URL, asgi_client, run_buggy


@pytest.fixture(scope="module")
def report(buggy_spec: ApiSpec) -> RunReport:
    return asyncio.run(run_buggy(buggy_spec, [Strategy.POSITIVE, Strategy.SECURITY]))


def _hostile_report() -> RunReport:
    case = TestCase(
        id="negative-00001",
        endpoint="GET /search",
        strategy=Strategy.NEGATIVE,
        technique="wrong_type:q",
        method="GET",
        path="/search",
        expect=Expectation.ANY,
    )
    request = RequestRecord(method="GET", url="http://x/search?q=%3Cscript%3E")
    response = ResponseRecord(
        status=500,
        headers={"content-type": "text/html"},
        body="<script>alert('xss')</script>\x00\x07 nul",
    )
    failure = Failure(
        category=FailureCategory.SERVER_ERROR,
        check="server_error",
        message="server error 500 <b>bold</b>",
        severity=Severity.HIGH,
        endpoint=case.endpoint,
        test_case_id=case.id,
        strategy=case.strategy,
        technique=case.technique,
        status=500,
        request=request,
        response=response,
        curl="curl -X GET 'http://x/search?q=%3Cscript%3E'",
    )
    return RunReport(
        tool_version="0",
        spec_title="t",
        spec_source="s",
        base_url="http://x",
        seed=1,
        strategies=[Strategy.NEGATIVE],
        started_at="2026-01-01T00:00:00+00:00",
        results=[Result(test_case=case, request=request, response=response, failures=[failure])],
    )


def test_json_report(report: RunReport) -> None:
    data = json.loads(render_json(report))
    assert data["run"]["seed"] == 0
    assert data["summary"]["test_cases"] == len(report.results)
    assert data["summary"]["unique_findings"] == len(data["findings"]) > 0
    assert all(f["example"]["curl"].startswith("curl ") for f in data["findings"])
    assert set(data["summary"]["findings_by_strategy"]) <= {"positive", "security"}


def test_junit_report(report: RunReport) -> None:
    root = ET.fromstring(render_junit(report))
    assert root.tag == "testsuites"
    assert {s.get("name") for s in root} == {"apitest.positive", "apitest.security"}
    failing_cases = [tc for tc in root.iter("testcase") if tc.find("failure") is not None]
    assert len(failing_cases) == sum(1 for r in report.results if r.failures)
    assert int(root.get("tests", "0")) == len(report.results)


def test_junit_sanitizes_control_characters() -> None:
    xml = render_junit(_hostile_report())
    root = ET.fromstring(xml)  # would raise on raw \x00
    failure = root.find(".//failure")
    assert failure is not None and failure.text is not None
    assert "\\x00" in failure.text


def test_html_report_structure(report: RunReport) -> None:
    html = render_html(report)
    assert html.startswith("<!doctype html>")
    for anchor in ("contract_violation", "server_error", "undocumented_response", "performance"):
        assert f'id="{anchor}"' in html
    assert "curl -sS -i -X" in html
    assert "Reproduce" in html


def test_html_escapes_response_content() -> None:
    html = render_html(_hostile_report())
    assert "<script>alert('xss')</script>" not in html
    assert "&lt;script&gt;alert(" in html
    assert "<b>bold</b>" not in html


def test_output_paths() -> None:
    assert output_paths([ReportFormat.HTML], None) == {
        ReportFormat.HTML: Path("apitest-report.html")
    }
    assert output_paths([ReportFormat.JUNIT], Path("out/junit.xml")) == {
        ReportFormat.JUNIT: Path("out/junit.xml")
    }
    several = output_paths([ReportFormat.HTML, ReportFormat.JSON], Path("out"))
    assert several == {
        ReportFormat.HTML: Path("out/apitest-report.html"),
        ReportFormat.JSON: Path("out/apitest-report.json"),
    }


def test_cli_run_writes_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apitest.session, "make_client", lambda config: asgi_client())
    monkeypatch.setenv("BUGGY_API_KEY", "secret-key")
    result = CliRunner().invoke(
        app,
        [
            "run",
            str(SPECS / "buggy_api.yaml"),
            "--config",
            "examples/buggy_api/apitest.yaml",
            "--base-url",
            BASE_URL,
            "-f",
            "html",
            "-f",
            "json",
            "-f",
            "junit",
            "-o",
            str(tmp_path),
            "--quiet",
        ],
    )
    assert result.exit_code == 1, result.output  # findings exist
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["apitest-report.html", "apitest-report.json", "apitest-report.xml"]
    assert "secret-key" not in (tmp_path / "apitest-report.json").read_text("utf-8")


def test_cli_run_requires_base_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run", str(SPECS / "buggy_api.yaml")])
    assert result.exit_code == 2
    assert "base-url" in result.output
