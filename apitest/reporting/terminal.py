"""Rich terminal report."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from apitest.models import FailureCategory, FailureGroup, RunReport, Severity

CATEGORY_TITLES = {
    FailureCategory.CONTRACT: "Contract violations",
    FailureCategory.SERVER_ERROR: "Server errors",
    FailureCategory.UNDOCUMENTED: "Undocumented responses",
    FailureCategory.PERFORMANCE: "Performance",
    FailureCategory.SECURITY: "Security smells",
}
SEVERITY_STYLES = {Severity.HIGH: "bold red", Severity.MEDIUM: "yellow", Severity.LOW: "dim"}


def _summary_table(report: RunReport, groups: list[FailureGroup]) -> Table:
    table = Table(title="Findings by category", show_lines=False)
    table.add_column("Category")
    table.add_column("Unique", justify="right")
    table.add_column("Occurrences", justify="right")
    for category, title in CATEGORY_TITLES.items():
        members = [g for g in groups if g.example.category == category]
        style = "red" if members else "green"
        table.add_row(title, f"[{style}]{len(members)}[/]", str(sum(g.count for g in members)))
    return table


def _category_table(title: str, groups: list[FailureGroup], show_curl: bool) -> Table:
    table = Table(title=title, show_lines=True, expand=True)
    table.add_column("Sev", no_wrap=True)
    table.add_column("Endpoint", no_wrap=True)
    table.add_column("Status", justify="right")
    table.add_column("Found by")
    table.add_column("Finding", ratio=1)
    for group in groups:
        failure = group.example
        detail = escape(failure.message)
        if group.count > 1:
            detail += f" [dim](x{group.count})[/]"
        if show_curl:
            detail += f"\n[dim]{escape(failure.curl)}[/]"
        found_by = ", ".join(s.value for s in group.strategies) + f"\n[dim]{failure.technique}[/]"
        table.add_row(
            f"[{SEVERITY_STYLES[failure.severity]}]{failure.severity.value}[/]",
            failure.endpoint,
            str(failure.status or "-"),
            found_by,
            detail,
        )
    return table


def print_report(report: RunReport, console: Console, *, show_curl: bool = True) -> None:
    total = len(report.results)
    failed = sum(1 for r in report.results if r.failures)
    console.rule(f"apitest {report.tool_version}: {escape(report.spec_title)}")
    console.print(
        f"target [bold]{report.base_url}[/]  seed [bold]{report.seed}[/]  "
        f"strategies {', '.join(s.value for s in report.strategies)}"
    )
    console.print(
        f"{total} test cases, {report.request_count} requests in {report.duration_s:.1f}s: "
        f"[green]{total - failed - len(report.errors)} passed[/], "
        f"[red]{failed} with findings[/], {len(report.errors)} errors"
    )
    groups = report.groups()
    console.print(_summary_table(report, groups))
    for category, title in CATEGORY_TITLES.items():
        members = [g for g in groups if g.example.category == category]
        if members:
            members.sort(key=lambda g: (list(Severity).index(g.example.severity), g.key[0]))
            console.print(_category_table(title, members, show_curl))
    for result in report.errors[:10]:
        console.print(f"[red]error[/] {result.test_case.endpoint}: {escape(result.error or '')}")
    for note in report.notes:
        console.print(f"[cyan]note:[/] {escape(note)}")
    for skipped in report.skipped:
        console.print(f"[yellow]skipped:[/] {escape(skipped)}")
