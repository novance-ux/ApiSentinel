"""Rich rendering of a loaded spec for `apitest inspect`."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from apitest.models import ApiSpec, Endpoint, ParameterLocation

METHOD_STYLES = {
    "GET": "green",
    "POST": "yellow",
    "PUT": "blue",
    "PATCH": "cyan",
    "DELETE": "red",
}


def _params_summary(endpoint: Endpoint) -> str:
    parts = []
    locations: tuple[ParameterLocation, ...] = ("path", "query", "header", "cookie")
    for location in locations:
        params = endpoint.params_in(location)
        if params:
            names = ", ".join(p.name + ("*" if p.required else "") for p in params)
            parts.append(f"{location}: {names}")
    return "\n".join(parts) or "-"


def _body_summary(endpoint: Endpoint) -> str:
    body = endpoint.request_body
    if body is None:
        return "-"
    return body.content_type + (" *" if body.required else "")


def _auth_summary(endpoint: Endpoint) -> str:
    if not endpoint.security:
        return "none"
    alternatives = [" + ".join(req) or "anonymous" for req in endpoint.security]
    return " | ".join(alternatives)


def endpoints_table(spec: ApiSpec) -> Table:
    table = Table(title=f"{spec.title} {spec.version} (OpenAPI {spec.openapi_version})")
    table.add_column("Method", no_wrap=True)
    table.add_column("Path", no_wrap=True)
    table.add_column("Operation")
    table.add_column("Parameters")
    table.add_column("Body")
    table.add_column("Responses")
    table.add_column("Auth")
    for endpoint in spec.endpoints:
        style = METHOD_STYLES.get(endpoint.method, "white")
        table.add_row(
            f"[{style}]{endpoint.method}[/]",
            endpoint.path,
            endpoint.operation_id or "-",
            _params_summary(endpoint),
            _body_summary(endpoint),
            ", ".join(endpoint.responses),
            _auth_summary(endpoint),
        )
    return table


def print_spec(spec: ApiSpec, console: Console) -> None:
    for warning in spec.warnings:
        console.print(f"[yellow]warning:[/] {warning}")
    console.print(endpoints_table(spec))
    console.print(
        f"{len(spec.endpoints)} endpoints; required marked with *; "
        f"{len(spec.schema_resources)} circular schema resource(s)"
    )
