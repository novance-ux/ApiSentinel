"""Ground-truth checks: every bug listed in BUGS.md is actually present in the example app."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from openapi_spec_validator import validate

from examples.buggy_api.app import API_KEY, SPEC_PATH, create_app

AUTH = {"X-API-Key": API_KEY}
APP_DIR = Path(__file__).resolve().parent.parent / "examples" / "buggy_api"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def _item(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"name": "Widget", "price": 9.99, "quantity": 5, "category": "toys"}
    body.update(overrides)
    return body


def _create_item(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post("/items", json=_item(**overrides), headers=AUTH)
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


def _nested(depth: int) -> dict[str, Any]:
    node: dict[str, Any] = {}
    for _ in range(depth - 1):
        node = {"k": node}
    return node


# --------------------------------------------------------------------------- sanity


def test_spec_is_valid_openapi() -> None:
    validate(yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8")))


def test_every_bug_is_documented_and_tagged() -> None:
    bugs_md = (APP_DIR / "BUGS.md").read_text("utf-8")
    documented = set(re.findall(r"^\| (BUG-\d\d) \|", bugs_md, re.M))
    tagged = set(re.findall(r"BUG-\d\d", (APP_DIR / "app.py").read_text("utf-8")))
    assert len(documented) >= 15
    assert documented == tagged


def test_happy_path_item_and_order(client: TestClient) -> None:
    item = _create_item(client)
    order = client.post("/orders", json={"item_id": item["id"], "quantity": 2}, headers=AUTH)
    assert order.status_code == 201
    assert order.json()["total"] == pytest.approx(19.98)
    assert client.get(f"/orders/{order.json()['id']}", headers=AUTH).status_code == 200


def test_auth_is_enforced_on_most_endpoints(client: TestClient) -> None:
    assert client.get("/items").status_code == 401
    assert client.post("/items", json=_item()).status_code == 401


def test_spec_is_served(client: TestClient) -> None:
    assert "openapi: 3.0.3" in client.get("/openapi.yaml").text


# --------------------------------------------------------------------------- bugs


def test_bug_01_pagination_off_by_one(client: TestClient) -> None:
    for _ in range(3):
        _create_item(client)
    body = client.get("/items", params={"page": 1, "page_size": 3}, headers=AUTH).json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_bug_02_empty_name_crashes(client: TestClient) -> None:
    assert client.post("/items", json=_item(name=""), headers=AUTH).status_code == 500


def test_bug_03_negative_price_accepted(client: TestClient) -> None:
    assert client.post("/items", json=_item(price=-5), headers=AUTH).status_code == 201


def test_bug_04_detail_view_missing_created_at(client: TestClient) -> None:
    item = _create_item(client)
    assert "created_at" in item
    assert "created_at" not in client.get(f"/items/{item['id']}", headers=AUTH).json()


def test_bug_05_undocumented_teapot(client: TestClient) -> None:
    assert client.get("/search", params={"q": "a" * 51}, headers=AUTH).status_code == 418
    assert client.get("/search", params={"q": "a" * 50}, headers=AUTH).status_code == 200


def test_bug_06_stack_trace_leak(client: TestClient) -> None:
    response = client.get("/users/abc", headers=AUTH)
    assert response.status_code == 500
    assert "Traceback (most recent call last)" in response.json()["traceback"]


def test_bug_07_delete_without_auth(client: TestClient) -> None:
    item = _create_item(client)
    assert client.delete(f"/items/{item['id']}").status_code == 204


def test_bug_08_put_returns_price_as_string(client: TestClient) -> None:
    item = _create_item(client)
    updated = client.put(f"/items/{item['id']}", json=_item(price=3.5), headers=AUTH)
    assert updated.status_code == 200
    assert updated.json()["price"] == "3.5"


def test_bug_09_create_user_returns_200(client: TestClient) -> None:
    user = {"username": "alice", "email": "alice@example.com", "age": 30}
    assert client.post("/users", json=user, headers=AUTH).status_code == 200


def test_bug_10_deleted_item_still_readable(client: TestClient) -> None:
    item = _create_item(client)
    assert client.delete(f"/items/{item['id']}", headers=AUTH).status_code == 204
    assert client.get(f"/items/{item['id']}", headers=AUTH).status_code == 200
    assert client.put(f"/items/{item['id']}", json=_item(), headers=AUTH).status_code == 404


def test_bug_11_max_quantity_rejected(client: TestClient) -> None:
    assert client.post("/items", json=_item(quantity=10_000), headers=AUTH).status_code == 422
    assert client.post("/items", json=_item(quantity=9_999), headers=AUTH).status_code == 201


def test_bug_12_null_description_crashes(client: TestClient) -> None:
    assert client.post("/items", json=_item(description=None), headers=AUTH).status_code == 500
    assert client.post("/items", json=_item(description=" x "), headers=AUTH).status_code == 201


def test_bug_13_non_ascii_search_crashes(client: TestClient) -> None:
    assert client.get("/search", params={"q": "café"}, headers=AUTH).status_code == 500
    assert client.get("/search", params={"q": "cafe"}, headers=AUTH).status_code == 200


def test_bug_14_mass_assignment(client: TestClient) -> None:
    user = {"username": "mallory", "email": "m@example.com", "age": 40, "is_admin": True}
    response = client.post("/users", json=user, headers=AUTH)
    assert response.json()["is_admin"] is True
    assert client.get(f"/users/{response.json()['id']}", headers=AUTH).json()["is_admin"] is True


@pytest.mark.slow
def test_bug_15_large_pages_are_slow(client: TestClient) -> None:
    started = time.perf_counter()
    assert client.get("/items", params={"page_size": 51}, headers=AUTH).status_code == 200
    assert time.perf_counter() - started > 1.0


def test_bug_16_deep_metadata_crashes(client: TestClient) -> None:
    assert client.post("/items", json=_item(metadata=_nested(40)), headers=AUTH).status_code == 500
    assert client.post("/items", json=_item(metadata=_nested(5)), headers=AUTH).status_code == 201


def test_bug_17_health_is_text_plain(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_bug_18_zero_quantity_order_crashes(client: TestClient) -> None:
    item = _create_item(client)
    order = client.post("/orders", json={"item_id": item["id"], "quantity": 0}, headers=AUTH)
    assert order.status_code == 500


@pytest.mark.parametrize("name", ["{0}", "{", "}", "a {name} b"])
def test_bug_19_format_string_names_crash(client: TestClient, name: str) -> None:
    assert client.post("/items", json=_item(name=name), headers=AUTH).status_code == 500
