"""Ids harvested from successful responses, reused so later requests hit real resources."""

from __future__ import annotations

import json
import random
from collections import defaultdict
from typing import Any

from apitest.models import Endpoint, ResponseRecord
from apitest.stateful.naming import resource_for_path

_MISSING = object()


def response_json(response: ResponseRecord | None) -> Any:
    if response is None or not response.body:
        return None
    try:
        return json.loads(response.body)
    except ValueError:
        return None


class ValuePool:
    def __init__(self) -> None:
        self._values: dict[str, list[Any]] = defaultdict(list)

    def add(self, resource: str, value: Any) -> None:
        if isinstance(value, str | int) and not isinstance(value, bool):
            bucket = self._values[resource]
            if value not in bucket:
                bucket.append(value)

    def discard(self, resource: str, value: Any) -> None:
        if value in self._values.get(resource, []):
            self._values[resource].remove(value)

    def values(self, resource: str) -> list[Any]:
        return list(self._values.get(resource, []))

    def pick(self, resource: str | None, rng: random.Random, probability: float = 0.8) -> Any:
        """A pooled value for `resource` (consuming RNG only when the pool has values)."""
        if resource is None:
            return _MISSING
        values = self._values.get(resource)
        if not values or rng.random() >= probability:
            return _MISSING
        return rng.choice(values)

    def harvest(self, endpoint: Endpoint, response: ResponseRecord | None) -> dict[str, Any] | None:
        """Record the id from a successful create; returns the created body if any."""
        if response is None or not 200 <= response.status < 300 or endpoint.method != "POST":
            return None
        body = response_json(response)
        resource = resource_for_path(endpoint.path)
        if not isinstance(body, dict) or resource is None:
            return None
        for field in ("id", f"{resource}_id", f"{resource}Id"):
            if field in body:
                self.add(resource, body[field])
                break
        return body


MISSING = _MISSING
