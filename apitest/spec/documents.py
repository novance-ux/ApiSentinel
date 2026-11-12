"""Fetch and parse spec documents from files or URLs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx
import yaml

from apitest.spec.errors import SpecError

_BOOL_TAG = "tag:yaml.org,2002:bool"
_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"


class _SpecYamlLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2-style scalars: no timestamps, and only true/false are booleans.

    Plain SafeLoader turns `2020-01-01` into a date and `yes`/`on` into True, which corrupts
    enums, examples, and defaults in real-world specs.
    """


_SpecYamlLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag not in (_BOOL_TAG, _TIMESTAMP_TAG)]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_SpecYamlLoader.add_implicit_resolver(
    _BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def is_url(source: str) -> bool:
    return urlparse(source).scheme in ("http", "https")


def source_to_uri(source: str) -> str:
    """Normalize a file path or URL into an absolute URI usable as a $ref base."""
    if is_url(source) or source.startswith("file:"):
        return source
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise SpecError(f"spec file not found: {source}")
    return path.as_uri()


def parse_text(text: str, origin: str) -> Any:
    try:
        if text.lstrip().startswith(("{", "[")):
            return json.loads(text)
        return yaml.load(text, Loader=_SpecYamlLoader)  # SafeLoader subclass
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise SpecError(f"cannot parse {origin}: {exc}") from exc


def fetch_uri(uri: str, timeout: float = 30.0) -> Any:
    """Load and parse the document at a file:// or http(s):// URI."""
    parsed = urlparse(uri)
    if parsed.scheme in ("http", "https"):
        try:
            response = httpx.get(uri, timeout=timeout, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SpecError(f"cannot fetch {uri}: {exc}") from exc
        return parse_text(response.text, uri)
    if parsed.scheme == "file":
        path = Path(url2pathname(parsed.path))
        try:
            return parse_text(path.read_text(encoding="utf-8"), str(path))
        except OSError as exc:
            raise SpecError(f"cannot read {path}: {exc}") from exc
    raise SpecError(f"unsupported URI scheme in {uri!r}")
