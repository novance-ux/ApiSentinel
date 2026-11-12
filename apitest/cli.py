"""Typer entry point for the apitest CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from apitest import __version__
from apitest.config import ConfigError, RunConfig, load_config
from apitest.models import Strategy
from apitest.reporting.inspect_view import print_spec
from apitest.reporting.terminal import print_report
from apitest.reporting.writers import ReportFormat, write_reports
from apitest.session import run_session
from apitest.spec.errors import SpecError
from apitest.spec.loader import load_spec

app = typer.Typer(
    help="Generate and run tests from an OpenAPI 3.x spec.",
    no_args_is_help=True,
    add_completion=False,
)

SpecArg = Annotated[str, typer.Argument(help="OpenAPI 3.x spec: file path or http(s) URL.")]


def _console() -> Console:
    return Console(width=160, soft_wrap=False)


def _fail(message: str) -> typer.Exit:
    typer.secho(f"error: {message}", fg="red", err=True)
    return typer.Exit(2)


@app.callback()
def main() -> None:
    """apitest: OpenAPI-driven API testing."""


@app.command()
def version() -> None:
    """Print the apitest version."""
    typer.echo(__version__)


@app.command()
def inspect(
    spec: SpecArg,
    as_json: Annotated[bool, typer.Option("--json", help="Print endpoints as JSON.")] = False,
    strict: Annotated[bool, typer.Option(help="Fail if the spec is not valid OpenAPI.")] = False,
) -> None:
    """Load a spec, resolve all $refs, and print its endpoints."""
    try:
        api = load_spec(spec, strict=strict)
    except SpecError as exc:
        raise _fail(str(exc)) from exc
    if as_json:
        endpoints = [e.model_dump(mode="json") for e in api.endpoints]
        typer.echo(json.dumps(endpoints, indent=2))
        return
    print_spec(api, _console())


def _apply_overrides(config: RunConfig, overrides: dict[str, Any]) -> RunConfig:
    updates = {key: value for key, value in overrides.items() if value is not None}
    return RunConfig.model_validate({**config.model_dump(), **updates})


@app.command()
def run(
    spec: SpecArg,
    base_url: Annotated[
        str | None, typer.Option(help="Target API base URL. Only this origin is contacted.")
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="apitest.yaml (default: ./apitest.yaml)")
    ] = None,
    seed: Annotated[int | None, typer.Option(help="Seed; the same seed replays the run.")] = None,
    strategy: Annotated[
        list[Strategy] | None, typer.Option("--strategy", "-s", help="Repeatable.")
    ] = None,
    concurrency: Annotated[int | None, typer.Option(help="Max concurrent requests.")] = None,
    timeout: Annotated[float | None, typer.Option(help="Per-request timeout (s).")] = None,
    retries: Annotated[int | None, typer.Option(help="Retries on transport errors.")] = None,
    rate_limit: Annotated[float | None, typer.Option(help="Max requests per second.")] = None,
    latency_threshold: Annotated[
        float | None, typer.Option(help="Latency threshold in ms for performance findings.")
    ] = None,
    cases_per_endpoint: Annotated[
        int | None, typer.Option(help="Positive cases per endpoint.")
    ] = None,
    show_secrets: Annotated[
        bool, typer.Option(help="Do not redact credentials in reports.")
    ] = False,
    no_curl: Annotated[bool, typer.Option(help="Hide curl reproducers in the terminal.")] = False,
    report_format: Annotated[
        list[ReportFormat] | None,
        typer.Option("--format", "-f", help="File report format; repeatable."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Report file (one format) or directory (several formats)."
        ),
    ] = None,
    quiet: Annotated[bool, typer.Option(help="Skip the terminal report.")] = False,
    llm: Annotated[
        bool,
        typer.Option(
            "--llm",
            help="Add LLM-generated cases (realistic values, business rules) via the Anthropic "
            "API. Sends endpoint schemas and descriptions, never responses or credentials.",
        ),
    ] = False,
    exit_zero: Annotated[bool, typer.Option(help="Exit 0 even when findings exist.")] = False,
) -> None:
    """Generate test cases from SPEC and run them against --base-url."""
    try:
        api = load_spec(spec)
        config = _apply_overrides(
            load_config(config_path),
            {
                "base_url": base_url,
                "seed": seed,
                "strategies": strategy,
                "concurrency": concurrency,
                "timeout": timeout,
                "retries": retries,
                "rate_limit": rate_limit,
                "latency_threshold_ms": latency_threshold,
                "cases_per_endpoint": cases_per_endpoint,
                "redact_secrets": False if show_secrets else None,
            },
        )
    except (SpecError, ConfigError) as exc:
        raise _fail(str(exc)) from exc
    target = config.base_url
    if not target:
        raise _fail("no target: pass --base-url or set base_url in apitest.yaml")
    if llm and Strategy.LLM not in config.strategies:
        config = config.model_copy(update={"strategies": [*config.strategies, Strategy.LLM]})
    report = asyncio.run(run_session(api, config, target))
    if not quiet:
        print_report(report, _console(), show_curl=not no_curl)
    if report_format:
        for path in write_reports(report, report_format, output):
            typer.echo(f"wrote {path}")
    if report.failures and not exit_zero:
        raise typer.Exit(1)
