"""Render a recorded request as a runnable curl command."""

from __future__ import annotations

import shlex

from apitest.models import RequestRecord

_NOISE_HEADERS = frozenset(
    {"host", "content-length", "accept-encoding", "connection", "user-agent", "accept"}
)
_ESCAPES = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def shell_quote(text: str) -> str:
    """POSIX-quote `text`; fall back to ANSI-C $'...' quoting for control characters."""
    if all(ch.isprintable() or ch in "\n\t" for ch in text) and "\x00" not in text:
        return shlex.quote(text)
    out = []
    for ch in text:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ch.isprintable():
            out.append(ch)
        else:
            out.append(f"\\u{ord(ch):04x}" if ord(ch) > 0xFF else f"\\x{ord(ch):02x}")
    return "$'" + "".join(out) + "'"


def to_curl(request: RequestRecord) -> str:
    parts = ["curl", "-sS", "-i", "-X", request.method, shell_quote(request.url)]
    for name, value in request.headers.items():
        if name.lower() not in _NOISE_HEADERS:
            parts += ["-H", shell_quote(f"{name}: {value}")]
    if request.body:
        parts += ["--data-raw", shell_quote(request.body)]
    return " ".join(parts)
