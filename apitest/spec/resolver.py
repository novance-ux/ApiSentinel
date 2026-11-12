"""$ref resolution with safe handling of circular references.

Non-circular refs are inlined. A ref that points back into its own resolution chain is
replaced by an absolute ref of the form ``urn:apitest:doc:<n>#<pointer>``. Those targets
are collected into sparse "schema resources" (see :meth:`RefResolver.schema_resources`)
so jsonschema can validate recursive schemas and the generator can follow them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote, unquote, urldefrag, urljoin

from apitest.spec.errors import SpecError

URN_PREFIX = "urn:apitest:doc:"
_IGNORED_REF_SIBLINGS = frozenset({"$ref", "description", "summary"})

RefKey = tuple[str, str]  # (document uri, json pointer)


def split_pointer(pointer: str) -> list[str]:
    if not pointer:
        return []
    if not pointer.startswith("/"):
        raise SpecError(f"invalid JSON pointer {pointer!r}")
    return [unquote(part).replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def lookup_pointer(document: Any, pointer: str) -> Any:
    node = document
    for part in split_pointer(pointer):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise SpecError(f"unresolvable JSON pointer {pointer!r} (missing {part!r})")
    return node


def _set_pointer(document: dict[str, Any], parts: list[str], value: Any) -> None:
    node = document
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


class RefResolver:
    def __init__(self, root: Any, base_uri: str, fetch: Callable[[str], Any]) -> None:
        self._base_uri = base_uri
        self._documents: dict[str, Any] = {base_uri: root}
        self._doc_ids: dict[str, int] = {}
        self._memo: dict[RefKey, Any] = {}
        self._cyclic: set[RefKey] = set()
        self._fetch = fetch

    def resolve(self, node: Any) -> Any:
        """Return a copy of `node` (from the root document) with every $ref resolved."""
        return self._resolve(node, self._base_uri, ())

    def urn(self, key: RefKey) -> str:
        doc_id = self._doc_ids.setdefault(key[0], len(self._doc_ids))
        return f"{URN_PREFIX}{doc_id}#{quote(key[1], safe='/~')}"

    def schema_resources(self, transform: Callable[[Any], Any]) -> dict[str, Any]:
        """Sparse documents holding every circular target, keyed by resource URN."""
        resources: dict[str, Any] = {}
        for key in sorted(self._cyclic):
            resource_id = self.urn(key).split("#", 1)[0]
            value = transform(self._memo[key])
            parts = split_pointer(key[1])
            if not parts:
                resources[resource_id] = value
                continue
            _set_pointer(resources.setdefault(resource_id, {}), parts, value)
        return resources

    # ------------------------------------------------------------------ internals

    def _resolve(self, node: Any, base: str, stack: tuple[RefKey, ...]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                return self._resolve_ref(node, ref, base, stack)
            return {key: self._resolve(value, base, stack) for key, value in node.items()}
        if isinstance(node, list):
            return [self._resolve(item, base, stack) for item in node]
        return node

    def _resolve_ref(
        self, node: dict[str, Any], ref: str, base: str, stack: tuple[RefKey, ...]
    ) -> Any:
        if ref.startswith(URN_PREFIX):
            return dict(node)
        uri, fragment = urldefrag(urljoin(base, ref))
        key: RefKey = (uri, fragment)
        if key in stack:
            self._cyclic.add(key)
            resolved: Any = {"$ref": self.urn(key)}
        elif key in self._memo:
            resolved = self._memo[key]
        else:
            target = lookup_pointer(self._document(uri), fragment)
            resolved = self._resolve(target, uri, (*stack, key))
            self._memo[key] = resolved
        siblings = {k: v for k, v in node.items() if k not in _IGNORED_REF_SIBLINGS}
        if not siblings:
            return resolved
        return {"allOf": [resolved, self._resolve(siblings, base, stack)]}

    def _document(self, uri: str) -> Any:
        if uri not in self._documents:
            self._documents[uri] = self._fetch(uri)
        return self._documents[uri]
