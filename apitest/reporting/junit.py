"""JUnit XML report for CI: one <testsuite> per strategy, one <testcase> per test case."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict

from apitest.models import Failure, Result, RunReport

# XML 1.0 forbids most control characters, which fuzz payloads are full of.
_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def _clean(text: str) -> str:
    return _ILLEGAL_XML.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def _failure_text(failure: Failure) -> str:
    lines = [failure.message, "", f"reproduce: {failure.curl}"]
    if failure.response is not None:
        lines += ["", f"response {failure.response.status}:", failure.response.body[:2000]]
    return _clean("\n".join(lines))


def _testcase(parent: ET.Element, result: Result) -> None:
    case = result.test_case
    elapsed = (result.response.elapsed_ms / 1000) if result.response else 0.0
    element = ET.SubElement(
        parent,
        "testcase",
        classname=_clean(case.endpoint),
        name=_clean(f"{case.id} {case.technique}"),
        time=f"{elapsed:.3f}",
    )
    if result.error:
        ET.SubElement(element, "error", message=_clean(result.error), type="transport")
    for failure in result.failures:
        node = ET.SubElement(
            element,
            "failure",
            message=_clean(f"[{failure.category.value}] {failure.message}"),
            type=failure.check,
        )
        node.text = _failure_text(failure)


def render_junit(report: RunReport) -> str:
    by_strategy: dict[str, list[Result]] = defaultdict(list)
    for result in report.results:
        by_strategy[result.test_case.strategy.value].append(result)
    root = ET.Element(
        "testsuites",
        name="apitest",
        tests=str(len(report.results)),
        failures=str(sum(1 for r in report.results if r.failures)),
        errors=str(len(report.errors)),
        time=f"{report.duration_s:.3f}",
    )
    for strategy, results in by_strategy.items():
        suite = ET.SubElement(
            root,
            "testsuite",
            name=f"apitest.{strategy}",
            tests=str(len(results)),
            failures=str(sum(1 for r in results if r.failures)),
            errors=str(sum(1 for r in results if r.error)),
            timestamp=report.started_at,
        )
        for result in results:
            _testcase(suite, result)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
