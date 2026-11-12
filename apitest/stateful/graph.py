"""Dependency graph between operations: which response values feed which requests.

Edges come from OpenAPI `links` when the spec declares them. Otherwise they are inferred
by matching a consumer's path parameters (and `*_id` body fields) to fields in the 2xx
response of a producer that creates that resource.
"""

from __future__ import annotations

import graphlib
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import unquote

from apitest.models import ApiSpec, Endpoint
from apitest.stateful.naming import (
    collection_path,
    field_candidates,
    resource_for_param,
    resource_for_path,
)

Target = Literal["path", "query", "header", "body"]
_TARGETS: dict[str, Target] = {"path": "path", "query": "query", "header": "header"}


@dataclass(frozen=True)
class Binding:
    target: Target
    name: str  # parameter name, or top-level body field
    pointer: str  # JSON pointer into the producer's response body, e.g. "/id"
    via: Literal["link", "inferred"]


@dataclass
class Edge:
    producer: str
    consumer: str
    bindings: list[Binding] = field(default_factory=list)


@dataclass
class DependencyGraph:
    edges: list[Edge]
    order: list[str]

    def consumers_of(self, producer: str) -> list[Edge]:
        rank = {key: i for i, key in enumerate(self.order)}
        found = [e for e in self.edges if e.producer == producer]
        return sorted(found, key=lambda e: rank.get(e.consumer, len(rank)))

    def producers_of(self, consumer: str) -> list[Edge]:
        return [e for e in self.edges if e.consumer == consumer]

    def creators(self) -> list[str]:
        """Producers that are not themselves waiting on another producer, in order."""
        producers = {e.producer for e in self.edges}
        return [key for key in self.order if key in producers]


def _response_properties(endpoint: Endpoint) -> dict[str, Any]:
    for status in endpoint.success_statuses():
        schema = endpoint.responses[str(status)].json_schema or {}
        props: dict[str, Any] = dict(schema.get("properties") or {})
        for sub in schema.get("allOf") or []:
            if isinstance(sub, dict):
                props.update(sub.get("properties") or {})
        if props:
            return props
    return {}


def _is_creator(endpoint: Endpoint) -> bool:
    """POSTs that return an object. (PUT /x/{id} consumes an id, so it is an update.)"""
    return endpoint.method == "POST" and bool(_response_properties(endpoint))


# --------------------------------------------------------------------------- links


def _link_target(
    spec: ApiSpec, operation_id: str | None, operation_ref: str | None
) -> Endpoint | None:
    if operation_id:
        return spec.by_operation_id(operation_id)
    if operation_ref and operation_ref.startswith("#/paths/"):
        _, _, rest = operation_ref.partition("#/paths/")
        path_part, _, method = rest.rpartition("/")
        path = unquote(path_part).replace("~1", "/").replace("~0", "~")
        return next(
            (e for e in spec.endpoints if e.path == path and e.method == method.upper()), None
        )
    return None


def _link_edges(spec: ApiSpec, producer: Endpoint) -> list[Edge]:
    edges: list[Edge] = []
    for status in producer.success_statuses():
        for link in producer.responses[str(status)].links:
            consumer = _link_target(spec, link.operation_id, link.operation_ref)
            if consumer is None:
                continue
            bindings = []
            for raw_name, expression in link.parameters.items():
                if not isinstance(expression, str) or not expression.startswith("$response.body#"):
                    continue
                location, _, name = raw_name.rpartition(".")
                param = next((p for p in consumer.parameters if p.name == name), None)
                target = _TARGETS.get(location or (param.location if param else "path"), "path")
                bindings.append(Binding(target, name, expression.split("#", 1)[1], "link"))
            if bindings:
                edges.append(Edge(producer.key, consumer.key, bindings))
    return edges


# --------------------------------------------------------------------------- inference


def _pick_field(param_name: str, producer_fields: dict[str, Any]) -> str | None:
    return next((f for f in field_candidates(param_name) if f in producer_fields), None)


def _match_param(
    name: str, consumer_path: str, producer: Endpoint, fields: dict[str, Any]
) -> str | None:
    """The producer response field that supplies consumer parameter `name`, if any."""
    same_collection = producer.path.rstrip("/") == collection_path(consumer_path).rstrip("/")
    resource = resource_for_param(name, consumer_path)
    if resource is not None and resource == resource_for_path(producer.path):
        return _pick_field(name, fields)
    if same_collection and name in fields:
        return name
    return None


def _body_id_fields(consumer: Endpoint) -> list[str]:
    body = consumer.request_body
    schema = (body.json_schema or {}) if body else {}
    props: dict[str, Any] = dict(schema.get("properties") or {})
    for sub in schema.get("allOf") or []:
        if isinstance(sub, dict):
            props.update(sub.get("properties") or {})
    return [name for name in props if resource_for_param(name) is not None]


def _inferred_edge(producer: Endpoint, consumer: Endpoint) -> Edge | None:
    fields = _response_properties(producer)
    bindings: list[Binding] = []
    for param in consumer.parameters:
        if param.location != "path":
            continue
        source = _match_param(param.name, consumer.path, producer, fields)
        if source is not None:
            bindings.append(Binding("path", param.name, f"/{source}", "inferred"))
    if consumer.method in ("POST", "PUT", "PATCH") and consumer.path != producer.path:
        for name in _body_id_fields(consumer):
            if resource_for_param(name) == resource_for_path(producer.path):
                source = _pick_field(name, fields)
                if source is not None:
                    bindings.append(Binding("body", name, f"/{source}", "inferred"))
    return Edge(producer.key, consumer.key, bindings) if bindings else None


def build_graph(spec: ApiSpec) -> DependencyGraph:
    edges: list[Edge] = []
    for producer in spec.endpoints:
        if not _is_creator(producer):
            continue
        linked = _link_edges(spec, producer)
        edges += linked
        linked_consumers = {e.consumer for e in linked}
        for consumer in spec.endpoints:
            if consumer.key == producer.key or consumer.key in linked_consumers:
                continue
            edge = _inferred_edge(producer, consumer)
            if edge is not None:
                edges.append(edge)
    return DependencyGraph(edges, _topological_order(spec, edges))


def _topological_order(spec: ApiSpec, edges: list[Edge]) -> list[str]:
    sorter: graphlib.TopologicalSorter[str] = graphlib.TopologicalSorter()
    for endpoint in spec.endpoints:
        sorter.add(endpoint.key)
    for edge in edges:
        sorter.add(edge.consumer, edge.producer)
    try:
        return list(sorter.static_order())
    except graphlib.CycleError:
        return [e.key for e in spec.endpoints]
