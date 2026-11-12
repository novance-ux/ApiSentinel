"""Seeded generators for common JSON Schema / OpenAPI string formats."""

from __future__ import annotations

import base64
import ipaddress
import random
import string
import uuid
from collections.abc import Callable
from datetime import date, timedelta

_TLDS = ("com", "org", "net", "io", "dev")
_EPOCH = date(1970, 1, 1)
_SPAN_DAYS = (date(2035, 12, 31) - _EPOCH).days


def _word(rng: random.Random, low: int = 3, high: int = 10) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(low, high)))


def _date(rng: random.Random) -> str:
    return (_EPOCH + timedelta(days=rng.randint(0, _SPAN_DAYS))).isoformat()


def _time(rng: random.Random) -> str:
    return f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}Z"


def _email(rng: random.Random) -> str:
    return f"{_word(rng)}.{_word(rng, 2, 6)}@{_word(rng)}.{rng.choice(_TLDS)}"


def _uri(rng: random.Random) -> str:
    return f"https://{_word(rng)}.example.{rng.choice(_TLDS)}/{_word(rng)}"


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _byte(rng: random.Random) -> str:
    raw = bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 24)))
    return base64.b64encode(raw).decode("ascii")


FORMATS: dict[str, Callable[[random.Random], str]] = {
    "email": _email,
    "idn-email": _email,
    "uuid": _uuid,
    "date": _date,
    "date-time": lambda rng: f"{_date(rng)}T{_time(rng)}",
    "time": _time,
    "uri": _uri,
    "url": _uri,
    "iri": _uri,
    "uri-reference": lambda rng: f"/{_word(rng)}/{_word(rng)}",
    "hostname": lambda rng: f"{_word(rng)}.example.{rng.choice(_TLDS)}",
    "idn-hostname": lambda rng: f"{_word(rng)}.example.{rng.choice(_TLDS)}",
    "ipv4": lambda rng: str(ipaddress.IPv4Address(rng.getrandbits(32))),
    "ipv6": lambda rng: ipaddress.IPv6Address(rng.getrandbits(128)).compressed,
    "byte": _byte,
    "password": lambda rng: _word(rng, 12, 20),
    "binary": lambda rng: _word(rng, 4, 16),
}
