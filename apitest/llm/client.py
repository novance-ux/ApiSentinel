"""Thin, cached JSON-producing wrapper around the Anthropic Messages API.

Only spec content (schemas, summaries, descriptions) is ever sent to the model. Never
responses from the API under test, and never credentials. Replies are cached on disk
keyed by a hash of the full request, so a run with the same seed and cache replays exactly.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Protocol

DEFAULT_MODEL = "claude-opus-5-5"
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class LlmError(RuntimeError):
    pass


class JsonLlm(Protocol):
    async def complete_json(
        self, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return an object matching `schema`, or None if the model declined."""


class AnthropicJsonLlm:
    def __init__(self, model: str = DEFAULT_MODEL, effort: Effort = "medium") -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the installed extras
            raise LlmError("the --llm option needs the extra: pip install 'apitest[llm]'") from exc
        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic()
        self.model = model
        self.effort = effort

    async def complete_json(
        self, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any] | None:
        try:
            response = await self._client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except self._anthropic.AuthenticationError as exc:
            raise LlmError("Anthropic credentials are invalid") from exc
        except TypeError as exc:
            # The SDK raises a plain TypeError when no credential source is configured.
            if "authentication" not in str(exc):
                raise
            raise LlmError(
                "no Anthropic credentials: set ANTHROPIC_API_KEY or run `ant auth login`"
            ) from exc
        except self._anthropic.APIStatusError as exc:
            raise LlmError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise LlmError(f"cannot reach the Anthropic API: {exc}") from exc
        if response.stop_reason == "refusal":
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            return None
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise LlmError(
                f"model returned invalid JSON (stop_reason={response.stop_reason})"
            ) from exc
        return data if isinstance(data, dict) else None


class CachedJsonLlm:
    """Wraps another JsonLlm with an on-disk, content-addressed cache."""

    def __init__(self, inner: JsonLlm, cache_dir: Path, model: str) -> None:
        self.inner = inner
        self.cache_dir = cache_dir
        self.model = model
        self.hits = 0
        self.misses = 0

    def _path(self, system: str, prompt: str, schema: dict[str, Any]) -> Path:
        key = json.dumps([self.model, system, prompt, schema], sort_keys=True)
        return self.cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"

    async def complete_json(
        self, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any] | None:
        path = self._path(system, prompt, schema)
        if path.is_file():
            self.hits += 1
            cached: dict[str, Any] | None = json.loads(path.read_text("utf-8"))["reply"]
            return cached
        self.misses += 1
        reply = await self.inner.complete_json(system, prompt, schema)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"reply": reply}, indent=2), encoding="utf-8")
        return reply
