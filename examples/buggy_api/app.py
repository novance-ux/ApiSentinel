"""A deliberately buggy inventory API used as a test target for apitest.

The documented contract lives in ``examples/specs/buggy_api.yaml``. This
implementation deviates from it on purpose; every deviation is tagged with a
``BUG-NN`` comment matching an entry in ``BUGS.md``. Do not fix them: they are
the ground truth used to evaluate apitest's detection rate.

Run with:  uv run uvicorn examples.buggy_api.app:app --port 8000
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

API_KEY = "secret-key"
SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "buggy_api.yaml"

CATEGORIES = ("books", "electronics", "toys")
USERNAME_RE = re.compile(r"[a-z0-9_]{3,20}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MAX_QUANTITY = 10_000
MAX_TAGS = 10
MAX_METADATA_DEPTH = 32  # internal limit, not documented in the spec
SLOW_PAGE_THRESHOLD = 50
SLOW_PAGE_DELAY_S = 1.2


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Store:
    items: dict[int, dict[str, Any]] = field(default_factory=dict)
    users: dict[int, dict[str, Any]] = field(default_factory=dict)
    orders: dict[int, dict[str, Any]] = field(default_factory=dict)
    audit_log: list[str] = field(default_factory=list)
    next_id: int = 1

    def new_id(self) -> int:
        value = self.next_id
        self.next_id += 1
        return value


# --------------------------------------------------------------------------- helpers


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> TypeGuard[float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _require_auth(request: Request) -> None:
    if request.headers.get("x-api-key") != API_KEY:
        raise ApiError(401, "missing or invalid API key")


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, RecursionError) as exc:
        raise ApiError(422, "request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise ApiError(422, "request body must be a JSON object")
    return body


def _parse_id(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(422, f"{name} must be an integer") from exc
    if value < 1:
        raise ApiError(422, f"{name} must be >= 1")
    return value


def _query_int(request: Request, name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(422, f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ApiError(422, f"{name} must be between {minimum} and {maximum}")
    return value


# --------------------------------------------------------------------------- items


def _check_metadata_depth(value: object, depth: int) -> None:
    """Walk nested metadata. The spec places no limit on nesting depth."""
    if depth > MAX_METADATA_DEPTH:
        # BUG-16: an undocumented internal limit surfaces as an unhandled 500.
        raise RecursionError("metadata nested too deeply")
    if isinstance(value, dict):
        children: list[object] = list(value.values())
    elif isinstance(value, list):
        children = list(value)
    else:
        children = []
    for child in children:
        _check_metadata_depth(child, depth + 1)


def _validate_description(body: dict[str, Any]) -> str | None:
    if "description" not in body:
        return None
    raw = body["description"]
    if raw is not None and (not isinstance(raw, str) or len(raw) > 500):
        raise ApiError(422, "description must be a string of at most 500 characters, or null")
    # BUG-12: the schema says `nullable: true`, but an explicit null crashes here.
    return str(raw.strip())  # type: ignore[union-attr]  # intentional, see BUG-12


def _validate_item(body: dict[str, Any]) -> dict[str, Any]:
    name = body.get("name")
    if not isinstance(name, str):
        raise ApiError(422, "name is required and must be a string")
    if len(name) > 100:
        raise ApiError(422, "name must be at most 100 characters")
    # BUG-02: `minLength: 1` is never checked; empty names crash later in _make_sku.

    price = body.get("price")
    if not _is_number(price) or not math.isfinite(price):
        raise ApiError(422, "price is required and must be a number")
    # BUG-03: `minimum: 0` is not enforced, so negative prices are accepted.

    quantity = body.get("quantity")
    if not _is_int(quantity):
        raise ApiError(422, "quantity is required and must be an integer")
    if not 0 <= quantity < MAX_QUANTITY:  # BUG-11: maximum is inclusive; should be `<=`
        raise ApiError(422, f"quantity must be between 0 and {MAX_QUANTITY}")

    category = body.get("category")
    if category not in CATEGORIES:
        raise ApiError(422, f"category must be one of {list(CATEGORIES)}")

    tags = body.get("tags", [])
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise ApiError(422, f"tags must be an array of at most {MAX_TAGS} strings")
    if not all(isinstance(tag, str) and 1 <= len(tag) <= 30 for tag in tags):
        raise ApiError(422, "each tag must be a string of 1-30 characters")

    metadata = body.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ApiError(422, "metadata must be an object")
    _check_metadata_depth(metadata, depth=1)

    return {
        "name": name,
        "price": price,
        "quantity": quantity,
        "category": category,
        "tags": tags,
        "description": _validate_description(body),
        "metadata": metadata,
    }


def _make_sku(item_id: int, name: str, category: str) -> str:
    # BUG-02 (crash site): name[0] raises IndexError when name == "".
    return f"{category[:3].upper()}-{name[0].upper()}{item_id:05d}"


def _audit_created(store: Store, item: dict[str, Any]) -> None:
    # BUG-19: user input is concatenated into a format template, so names containing
    # braces such as "{0}", "{name}" or a lone "{" crash the request.
    message = ("created item '" + item["name"] + "' with id {id}").format(id=item["id"])
    store.audit_log.append(message)


def _item_view(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def _item_detail_view(item: dict[str, Any]) -> dict[str, Any]:
    # BUG-04: the detail view drops created_at, which the Item schema marks required.
    return {key: value for key, value in _item_view(item).items() if key != "created_at"}


def _register_items(app: FastAPI, store: Store) -> None:
    def live_item(raw_id: str) -> dict[str, Any]:
        item = store.items.get(_parse_id(raw_id, "item_id"))
        if item is None or item["_deleted"]:
            raise ApiError(404, "item not found")
        return item

    @app.get("/items")
    async def list_items(request: Request) -> JSONResponse:
        _require_auth(request)
        page = _query_int(request, "page", default=1, minimum=1, maximum=10_000)
        page_size = _query_int(request, "page_size", default=20, minimum=1, maximum=100)
        if page_size > SLOW_PAGE_THRESHOLD:
            # BUG-15: large pages trigger a "re-index" that blows the latency budget.
            await asyncio.sleep(SLOW_PAGE_DELAY_S)
        live = [item for item in store.items.values() if not item["_deleted"]]
        offset = (page - 1) * page_size
        # BUG-01: off-by-one in the slice end; every page returns one item too few.
        window = live[offset : offset + page_size - 1]
        body = {
            "items": [_item_view(item) for item in window],
            "total": len(live),
            "page": page,
            "page_size": page_size,
        }
        return JSONResponse(body)

    @app.post("/items")
    async def create_item(request: Request) -> JSONResponse:
        _require_auth(request)
        fields = _validate_item(await _json_object(request))
        item_id = store.new_id()
        item = {
            "id": item_id,
            **fields,
            "sku": _make_sku(item_id, fields["name"], fields["category"]),
            "created_at": _now(),
            "_deleted": False,
        }
        _audit_created(store, item)
        store.items[item_id] = item
        return JSONResponse(_item_view(item), status_code=201)

    @app.get("/items/{item_id}")
    async def get_item(item_id: str, request: Request) -> JSONResponse:
        _require_auth(request)
        # BUG-10: soft-deleted items are still served; `_deleted` is never checked here.
        item = store.items.get(_parse_id(item_id, "item_id"))
        if item is None:
            raise ApiError(404, "item not found")
        return JSONResponse(_item_detail_view(item))

    @app.put("/items/{item_id}")
    async def replace_item(item_id: str, request: Request) -> JSONResponse:
        _require_auth(request)
        item = live_item(item_id)
        item.update(_validate_item(await _json_object(request)))
        view = _item_view(item)
        # BUG-08: price is serialised as a string instead of a number.
        view["price"] = str(item["price"])
        return JSONResponse(view)

    @app.delete("/items/{item_id}")
    async def delete_item(item_id: str) -> Response:
        # BUG-07: no auth check, so anyone can delete items.
        item = live_item(item_id)
        item["_deleted"] = True
        return Response(status_code=204)


# --------------------------------------------------------------------------- users


def _validate_user(body: dict[str, Any]) -> None:
    username = body.get("username")
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        raise ApiError(422, "username must match ^[a-z0-9_]{3,20}$")
    email = body.get("email")
    if not isinstance(email, str) or not EMAIL_RE.fullmatch(email):
        raise ApiError(422, "email must be a valid email address")
    age = body.get("age")
    if not _is_int(age) or not 13 <= age <= 120:
        raise ApiError(422, "age must be an integer between 13 and 120")


def _register_users(app: FastAPI, store: Store) -> None:
    @app.post("/users")
    async def create_user(request: Request) -> JSONResponse:
        _require_auth(request)
        body = await _json_object(request)
        _validate_user(body)
        user_id = store.new_id()
        # BUG-14: the whole request body is persisted (mass assignment), so unknown
        # fields such as "is_admin" are stored and echoed despite additionalProperties: false.
        user = {**body, "id": user_id}
        store.users[user_id] = user
        # BUG-09: the documented status is 201 Created.
        return JSONResponse(user, status_code=200)

    @app.get("/users/{user_id}")
    async def get_user(user_id: str, request: Request) -> JSONResponse:
        _require_auth(request)
        try:
            uid = int(user_id)
        except ValueError:
            # BUG-06: a debug error handler leaks the Python traceback to the client.
            body = {"error": "internal server error", "traceback": traceback.format_exc()}
            return JSONResponse(body, status_code=500)
        user = store.users.get(uid)
        if user is None:
            raise ApiError(404, "user not found")
        return JSONResponse(user)


# --------------------------------------------------------------------------- search & orders


def _register_search(app: FastAPI, store: Store) -> None:
    @app.get("/search")
    async def search(request: Request) -> JSONResponse:
        _require_auth(request)
        query = request.query_params.get("q")
        if query is None or not 1 <= len(query) <= 200:
            raise ApiError(422, "q must be between 1 and 200 characters")
        if len(query) > 50:
            # BUG-05: undocumented status code for "long" queries.
            return JSONResponse({"detail": "I'm a teapot: query too long"}, status_code=418)
        # BUG-13: the legacy index only supports ASCII; any non-ASCII query crashes.
        needle = query.encode("ascii").decode().lower()
        results = [
            _item_view(item)
            for item in store.items.values()
            if not item["_deleted"] and needle in item["name"].lower()
        ]
        return JSONResponse({"query": query, "results": results})


def _register_orders(app: FastAPI, store: Store) -> None:
    @app.post("/orders")
    async def create_order(request: Request) -> JSONResponse:
        _require_auth(request)
        body = await _json_object(request)
        item_id, quantity = body.get("item_id"), body.get("quantity")
        if not _is_int(item_id):
            raise ApiError(422, "item_id must be an integer")
        if not _is_int(quantity):
            raise ApiError(422, "quantity must be an integer")
        # BUG-18: `minimum: 1` is checked as `>= 0`, so quantity 0 slips through.
        if not 0 <= quantity <= 1000:
            raise ApiError(422, "quantity must be between 1 and 1000")
        item = store.items.get(item_id)
        if item is None or item["_deleted"]:
            raise ApiError(404, "item not found")
        total = round(item["price"] * quantity, 2)
        order = {
            "id": store.new_id(),
            "item_id": item_id,
            "quantity": quantity,
            # BUG-18 (crash site): ZeroDivisionError when quantity == 0.
            "unit_price": round(total / quantity, 2),
            "total": total,
        }
        store.orders[order["id"]] = order
        return JSONResponse(order, status_code=201)

    @app.get("/orders/{order_id}")
    async def get_order(order_id: str, request: Request) -> JSONResponse:
        _require_auth(request)
        order = store.orders.get(_parse_id(order_id, "order_id"))
        if order is None:
            raise ApiError(404, "order not found")
        return JSONResponse(order)


# --------------------------------------------------------------------------- app


def create_app() -> FastAPI:
    """Build a fresh app with empty in-memory state."""
    app = FastAPI(title="Buggy Inventory API", openapi_url=None, docs_url=None, redoc_url=None)
    store = Store()

    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    @app.get("/health")
    async def health() -> Response:
        # BUG-17: documented as application/json, served as text/plain.
        return PlainTextResponse(json.dumps({"status": "ok"}))

    @app.get("/openapi.yaml", include_in_schema=False)
    async def spec() -> FileResponse:
        return FileResponse(SPEC_PATH, media_type="application/yaml")

    _register_items(app, store)
    _register_users(app, store)
    _register_search(app, store)
    _register_orders(app, store)
    return app


app = create_app()
