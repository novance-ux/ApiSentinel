"""Dependency graph, CRUD workflows, value pool, and cleanup."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from apitest.models import ApiSpec, Severity, Strategy
from apitest.session import run_session
from apitest.stateful.graph import build_graph
from apitest.stateful.naming import (
    collection_path,
    field_candidates,
    resource_for_param,
    resource_for_path,
    singular,
)
from apitest.stateful.workflows import Workflow, resolve_pointer
from examples.buggy_api.app import API_KEY, create_app
from tests.helpers import BASE_URL, asgi_client, buggy_config, found, run_buggy

# --------------------------------------------------------------------------- naming


@pytest.mark.parametrize(
    ("word", "expected"),
    [("items", "item"), ("categories", "category"), ("boxes", "box"), ("address", "address")],
)
def test_singular(word: str, expected: str) -> None:
    assert singular(word) == expected


def test_resource_naming() -> None:
    assert resource_for_path("/items") == "item"
    assert resource_for_path("/users/{user_id}/orders") == "order"
    assert resource_for_param("item_id") == "item"
    assert resource_for_param("petId") == "pet"
    assert resource_for_param("id", "/orders/{id}") == "order"
    assert resource_for_param("isbn") is None
    assert collection_path("/items/{item_id}") == "/items"
    assert field_candidates("petId") == ["petId", "pet_id", "id"]


def test_resolve_pointer() -> None:
    assert resolve_pointer({"a": [{"b": 3}]}, "/a/0/b") == 3
    assert resolve_pointer({"a": 1}, "/missing") is None


# --------------------------------------------------------------------------- graph


def _edges(spec: ApiSpec) -> dict[tuple[str, str], list[tuple[str, str, str, str]]]:
    graph = build_graph(spec)
    return {
        (e.producer, e.consumer): [(b.target, b.name, b.pointer, b.via) for b in e.bindings]
        for e in graph.edges
    }


def test_graph_uses_links_then_inference(buggy_spec: ApiSpec) -> None:
    edges = _edges(buggy_spec)
    assert edges[("POST /items", "GET /items/{item_id}")] == [("path", "item_id", "/id", "link")]
    assert edges[("POST /items", "DELETE /items/{item_id}")] == [("path", "item_id", "/id", "link")]
    assert edges[("POST /items", "PUT /items/{item_id}")] == [
        ("path", "item_id", "/id", "inferred")
    ]
    assert edges[("POST /items", "POST /orders")] == [("body", "item_id", "/id", "inferred")]
    assert edges[("POST /orders", "GET /orders/{order_id}")] == [
        ("path", "order_id", "/id", "inferred")
    ]
    assert edges[("POST /users", "GET /users/{user_id}")] == [
        ("path", "user_id", "/id", "inferred")
    ]
    assert all(producer.startswith("POST") for producer, _ in edges)


def test_graph_topological_order(buggy_spec: ApiSpec) -> None:
    order = build_graph(buggy_spec).order
    assert (
        order.index("POST /items")
        < order.index("POST /orders")
        < order.index("GET /orders/{order_id}")
    )
    assert order.index("POST /items") < order.index("DELETE /items/{item_id}")


def test_graph_other_specs(library_spec: ApiSpec, petstore_spec: ApiSpec) -> None:
    assert _edges(library_spec) == {
        ("POST /books", "GET /books/{isbn}"): [("path", "isbn", "/isbn", "link")]
    }
    petstore = _edges(petstore_spec)
    assert petstore[("POST /pets", "GET /pets/{petId}")] == [("path", "petId", "/id", "inferred")]
    assert ("POST /pets", "DELETE /pets/{petId}") in petstore


# --------------------------------------------------------------------------- workflows


@pytest.mark.slow
async def test_stateful_finds_multi_step_bugs(buggy_spec: ApiSpec) -> None:
    report = await run_buggy(buggy_spec, [Strategy.STATEFUL])
    techniques = [r.test_case.technique for r in report.results]
    for expected in (
        "create",
        "read_after_create",
        "list_after_create",
        "update",
        "read_after_update",
        "unauthenticated_delete",
        "read_after_delete",
    ):
        assert expected in techniques, expected
    assert found(report, "GET /items", "pagination_short_page")  # BUG-01
    assert found(report, "GET /items/{item_id}", "schema_mismatch")  # BUG-04
    unauthenticated = found(report, "DELETE /items/{item_id}", "auth_not_enforced")
    assert unauthenticated[0].severity == Severity.HIGH  # BUG-07
    assert found(report, "PUT /items/{item_id}", "schema_mismatch")  # BUG-08
    assert found(report, "GET /items/{item_id}", "deleted_resource_retrievable")  # BUG-10
    assert found(report, "POST /users", "status_undocumented")  # BUG-09
    orders = [r for r in report.results if r.test_case.endpoint == "POST /orders"]
    assert orders and orders[0].response is not None and orders[0].response.status == 201


async def _live_items(app: Any) -> int:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        response = await client.get("/items", headers={"X-API-Key": API_KEY})
    total: int = response.json()["total"]
    return total


@pytest.mark.slow
async def test_cleanup_deletes_created_resources(buggy_spec: ApiSpec) -> None:
    app = create_app()
    config = buggy_config([Strategy.POSITIVE, Strategy.STATEFUL, Strategy.BOUNDARY])
    report = await run_session(buggy_spec, config, BASE_URL, client=asgi_client(app))
    assert any("cleanup: deleted" in note for note in report.notes)
    assert await _live_items(app) == 0


@pytest.mark.slow
async def test_cleanup_runs_even_when_a_step_raises(
    buggy_spec: ApiSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(self: Workflow, *args: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(Workflow, "listing", explode)
    app = create_app()
    config = buggy_config([Strategy.STATEFUL])
    with pytest.raises(RuntimeError, match="boom"):
        await run_session(buggy_spec, config, BASE_URL, client=asgi_client(app))
    assert await _live_items(app) == 0


@pytest.mark.slow
async def test_pool_lets_boundary_cases_reach_real_resources(buggy_spec: ApiSpec) -> None:
    report = await run_buggy(buggy_spec, [Strategy.BOUNDARY])
    assert any("setup:" in note for note in report.notes)
    zero = found(report, "POST /orders", "server_error")
    assert any(f.technique == "boundary:quantity=min-1" for f in zero)  # BUG-18
