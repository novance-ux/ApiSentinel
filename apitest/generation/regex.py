"""Generate strings that match a regular expression, driven by an explicit random.Random.

Walks the parse tree from Python's own regex parser. Lookarounds are ignored during
generation and enforced afterwards by checking the candidate with ``re.search`` and
retrying, which is how JSON Schema applies `pattern` (unanchored search).
"""

from __future__ import annotations

import importlib
import random
import re
import string
from typing import Any

_parser: Any = importlib.import_module("re._parser")

PRINTABLE = string.ascii_letters + string.digits + " -_.,:;!@#$%&*+=/?"
_CATEGORY_CHARS = {
    "CATEGORY_DIGIT": string.digits,
    "CATEGORY_NOT_DIGIT": string.ascii_letters + " -_.",
    "CATEGORY_SPACE": " ",
    "CATEGORY_NOT_SPACE": string.ascii_letters + string.digits,
    "CATEGORY_WORD": string.ascii_letters + string.digits + "_",
    "CATEGORY_NOT_WORD": " -.,!@#",
}
_ECMA_NAMED_GROUP = re.compile(r"\(\?<(?![=!])")


class RegexGenerationError(ValueError):
    pass


def to_python_pattern(pattern: str) -> str:
    """Translate the ECMA-262 constructs Python spells differently (named groups)."""
    return _ECMA_NAMED_GROUP.sub("(?P<", pattern)


class RegexGenerator:
    def __init__(self, rng: random.Random, max_repeat_extra: int = 6) -> None:
        self.rng = rng
        self.max_repeat_extra = max_repeat_extra

    def generate(
        self, pattern: str, min_length: int = 0, max_length: int | None = None, attempts: int = 100
    ) -> str:
        python_pattern = to_python_pattern(pattern)
        try:
            compiled = re.compile(python_pattern)
            parsed = _parser.parse(python_pattern)
        except re.error as exc:
            raise RegexGenerationError(f"unsupported pattern {pattern!r}: {exc}") from exc
        upper = max_length if max_length is not None else min_length + 64
        for _ in range(attempts):
            text = self._pad(self._emit(parsed, {}), min_length, upper)
            if min_length <= len(text) <= upper and compiled.search(text):
                return text
        raise RegexGenerationError(f"could not generate a match for {pattern!r}")

    def _pad(self, text: str, min_length: int, max_length: int) -> str:
        """Unanchored patterns may be padded; the caller re-checks the match."""
        if len(text) >= min_length or max_length < min_length:
            return text
        filler = "".join(self.rng.choice(string.ascii_lowercase) for _ in range(min_length))
        needed = min_length - len(text)
        return text + filler[:needed] if self.rng.random() < 0.5 else filler[:needed] + text

    # ------------------------------------------------------------------ tree walk

    def _emit(self, parsed: Any, groups: dict[int, str]) -> str:
        return "".join(self._emit_node(op, av, groups) for op, av in parsed)

    def _emit_node(self, op: Any, av: Any, groups: dict[int, str]) -> str:
        name = op.name
        if name == "LITERAL":
            return chr(av)
        if name == "NOT_LITERAL":
            return self.rng.choice([c for c in PRINTABLE if c != chr(av)])
        if name == "ANY":
            return self.rng.choice(PRINTABLE)
        if name == "IN":
            return self._emit_in(av)
        if name == "BRANCH":
            return self._emit(self.rng.choice(av[1]), groups)
        if name == "SUBPATTERN":
            group, _add, _del, sub = av
            text = self._emit(sub, groups)
            if group is not None:
                groups[group] = text
            return text
        if name == "ATOMIC_GROUP":
            return self._emit(av, groups)
        if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            low, high, sub = av
            high = min(high, low + self.max_repeat_extra)
            return "".join(self._emit(sub, groups) for _ in range(self.rng.randint(low, high)))
        if name == "GROUPREF":
            return groups.get(av, "")
        if name == "GROUPREF_EXISTS":
            group, yes, no = av
            branch = yes if group in groups else no
            return self._emit(branch, groups) if branch is not None else ""
        if name == "CATEGORY":
            return self.rng.choice(_CATEGORY_CHARS.get(av.name, PRINTABLE))
        # AT (anchors), ASSERT, ASSERT_NOT: zero-width, verified after generation.
        return ""

    def _emit_in(self, items: list[tuple[Any, Any]]) -> str:
        if items and items[0][0].name == "NEGATE":
            allowed = [c for c in PRINTABLE if not _in_set(items[1:], c)]
            if not allowed:
                raise RegexGenerationError("negated character class excludes every candidate")
            return self.rng.choice(allowed)
        op, av = self.rng.choice(items)
        name = op.name
        if name == "LITERAL":
            return chr(av)
        if name == "RANGE":
            return chr(self.rng.randint(av[0], av[1]))
        if name == "CATEGORY":
            return self.rng.choice(_CATEGORY_CHARS.get(av.name, PRINTABLE))
        return self.rng.choice(PRINTABLE)


def _in_set(items: list[tuple[Any, Any]], char: str) -> bool:
    code = ord(char)
    for op, av in items:
        name = op.name
        if name == "LITERAL" and av == code:
            return True
        if name == "RANGE" and av[0] <= code <= av[1]:
            return True
        if name == "CATEGORY" and char in _CATEGORY_CHARS.get(av.name, ""):
            return True
    return False
