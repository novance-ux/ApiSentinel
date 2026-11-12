"""Heuristics linking parameter names, response fields, and resource collections."""

from __future__ import annotations

import re

_ID_SUFFIX = re.compile(r"^(?P<stem>.+?)(?:_id|Id|ID|-id)$")
_PARAM = re.compile(r"^\{[^}]+\}$")


def singular(word: str) -> str:
    lower = word.lower()
    if lower.endswith("ies") and len(lower) > 3:
        return lower[:-3] + "y"
    if lower.endswith(("sses", "xes", "ches", "shes")):
        return lower[:-2]
    if lower.endswith("s") and not lower.endswith("ss"):
        return lower[:-1]
    return lower


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).replace("-", "_").lower()


def static_segments(path: str) -> list[str]:
    return [s for s in path.strip("/").split("/") if s and not _PARAM.match(s)]


def resource_for_path(path: str) -> str | None:
    """`/items` and `/items/{id}` → "item"; `/users/{u}/orders` → "order"."""
    segments = static_segments(path)
    return singular(segments[-1]) if segments else None


def collection_path(path: str) -> str:
    """The path up to (not including) its last `{param}` segment."""
    parts = path.rstrip("/").split("/")
    while parts and not _PARAM.match(parts[-1]):
        parts.pop()
    return "/".join(parts[:-1]) or "/"


def resource_for_param(name: str, path: str | None = None) -> str | None:
    """`item_id`/`itemId` → "item". A bare `id` takes the resource from its path segment."""
    match = _ID_SUFFIX.match(name)
    if match:
        return singular(snake(match.group("stem")))
    if name.lower() == "id" and path is not None:
        before = path.split("{" + name + "}")[0]
        return resource_for_path(before)
    return None


def field_candidates(param_name: str) -> list[str]:
    """Response fields that may hold the value for `param_name`, most specific first."""
    names = [param_name, snake(param_name)]
    match = _ID_SUFFIX.match(param_name)
    if match:
        stem = match.group("stem")
        names += [f"{stem}_id", f"{stem}Id"]
    names.append("id")
    return list(dict.fromkeys(names))
