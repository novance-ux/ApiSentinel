"""Write file reports selected with --format / --output."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from apitest.models import RunReport
from apitest.reporting.html import render_html
from apitest.reporting.json_report import render_json
from apitest.reporting.junit import render_junit


class ReportFormat(StrEnum):
    HTML = "html"
    JSON = "json"
    JUNIT = "junit"


RENDERERS: dict[ReportFormat, tuple[Callable[[RunReport], str], str]] = {
    ReportFormat.HTML: (render_html, "html"),
    ReportFormat.JSON: (render_json, "json"),
    ReportFormat.JUNIT: (render_junit, "xml"),
}
DEFAULT_STEM = "apitest-report"


def output_paths(formats: list[ReportFormat], output: Path | None) -> dict[ReportFormat, Path]:
    """One format: `output` is the file. Several: `output` is a directory."""
    unique = list(dict.fromkeys(formats))
    if len(unique) == 1 and (output is None or output.suffix):
        extension = RENDERERS[unique[0]][1]
        return {unique[0]: output or Path(f"{DEFAULT_STEM}.{extension}")}
    directory = output or Path("reports")
    return {fmt: directory / f"{DEFAULT_STEM}.{RENDERERS[fmt][1]}" for fmt in unique}


def write_reports(
    report: RunReport, formats: list[ReportFormat], output: Path | None
) -> list[Path]:
    written = []
    for fmt, path in output_paths(formats, output).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(RENDERERS[fmt][0](report), encoding="utf-8")
        written.append(path)
    return written
