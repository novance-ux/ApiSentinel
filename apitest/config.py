"""Run configuration, loaded from apitest.yaml and overridden by CLI flags."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from apitest.models import Strategy

_ENV_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
DEFAULT_STRATEGIES = [Strategy.POSITIVE, Strategy.SECURITY]


class ConfigError(Exception):
    pass


class ApiKeyAuth(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["api_key"]
    value: str
    name: str | None = None  # defaults to the name of the spec's apiKey scheme
    location: Literal["header", "query", "cookie"] | None = Field(default=None, alias="in")


class BearerAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["bearer"]
    token: str


class BasicAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["basic"]
    username: str
    password: str


AuthConfig = Annotated[ApiKeyAuth | BearerAuth | BasicAuth, Field(discriminator="type")]


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    auth: AuthConfig | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    concurrency: int = Field(default=8, ge=1, le=256)
    timeout: float = Field(default=10.0, gt=0)
    retries: int = Field(default=2, ge=0, le=10)
    retry_backoff: float = Field(default=0.25, ge=0)
    rate_limit: float | None = Field(default=None, gt=0, description="max requests per second")
    latency_threshold_ms: float = Field(default=1000.0, gt=0)
    cases_per_endpoint: int = Field(default=3, ge=1)
    seed: int = 0
    strategies: list[Strategy] = Field(default_factory=lambda: list(DEFAULT_STRATEGIES))
    redact_secrets: bool = True
    verify_tls: bool = True
    cleanup: bool = True  # delete resources the run created
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_cache_dir: str = ".apitest-cache/llm"
    llm_examples: int = Field(default=3, ge=1, le=10)
    llm_rules: int = Field(default=4, ge=1, le=10)


def expand_env(value: Any) -> Any:
    """Replace ${VAR} references in strings with environment variables."""
    if isinstance(value, str):

        def lookup(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"environment variable {name} is not set")
            return os.environ[name]

        return _ENV_VAR.sub(lookup, value)
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    return value


def load_config(path: Path | None) -> RunConfig:
    """Load apitest.yaml. A missing default file yields the default config."""
    if path is None:
        default = Path("apitest.yaml")
        if not default.is_file():
            return RunConfig()
        path = default
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        return RunConfig.model_validate(expand_env(raw))
    except ValidationError as exc:
        raise ConfigError(f"invalid config {path}:\n{exc}") from exc
