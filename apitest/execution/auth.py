"""Turn the configured credentials into headers, query params, or cookies."""

from __future__ import annotations

import base64

from pydantic import BaseModel, Field

from apitest.config import ApiKeyAuth, AuthConfig, BasicAuth, BearerAuth, ConfigError
from apitest.models import ApiSpec


class AuthMaterial(BaseModel):
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)

    @property
    def secret_names(self) -> set[str]:
        """Lower-cased names whose values must be redacted in reports."""
        return {name.lower() for name in (*self.headers, *self.query, *self.cookies)}


def _api_key_target(auth: ApiKeyAuth, spec: ApiSpec) -> tuple[str, str]:
    if auth.name and auth.location:
        return auth.name, auth.location
    scheme = next((s for s in spec.security_schemes.values() if s.type == "apiKey"), None)
    name = auth.name or (scheme.parameter_name if scheme else None)
    location = auth.location or (scheme.location if scheme else None) or "header"
    if not name:
        raise ConfigError("api_key auth needs `name` (the spec declares no apiKey scheme)")
    return name, location


def build_auth(auth: AuthConfig | None, spec: ApiSpec) -> AuthMaterial:
    if auth is None:
        return AuthMaterial()
    if isinstance(auth, BearerAuth):
        return AuthMaterial(headers={"Authorization": f"Bearer {auth.token}"})
    if isinstance(auth, BasicAuth):
        token = base64.b64encode(f"{auth.username}:{auth.password}".encode()).decode("ascii")
        return AuthMaterial(headers={"Authorization": f"Basic {token}"})
    name, location = _api_key_target(auth, spec)
    if location == "query":
        return AuthMaterial(query={name: auth.value})
    if location == "cookie":
        return AuthMaterial(cookies={name: auth.value})
    return AuthMaterial(headers={name: auth.value})
