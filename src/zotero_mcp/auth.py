"""Optional OAuth 2.1 resource-server support for hosted MCP deployments."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings


def _csv_env(name: str) -> list[str]:
    return [value.strip() for value in os.getenv(name, "").split(",") if value.strip()]


@dataclass(frozen=True)
class OAuthConfig:
    issuer_url: str
    resource_server_url: str
    jwks_url: str
    scopes: list[str]


def oauth_config_from_env() -> OAuthConfig | None:
    """Load an external OAuth issuer configuration, rejecting partial setups."""

    issuer = os.getenv("ZOTERO_MCP_OAUTH_ISSUER", "").rstrip("/")
    resource = os.getenv("ZOTERO_MCP_OAUTH_RESOURCE", "").rstrip("/")
    jwks = os.getenv("ZOTERO_MCP_OAUTH_JWKS_URL", "")
    values = (issuer, resource, jwks)
    if not any(values):
        return None
    if not all(values):
        raise ValueError(
            "OAuth configuration is incomplete. Set ZOTERO_MCP_OAUTH_ISSUER, "
            "ZOTERO_MCP_OAUTH_RESOURCE, and ZOTERO_MCP_OAUTH_JWKS_URL together."
        )
    scopes = _csv_env("ZOTERO_MCP_OAUTH_SCOPES") or ["zotero:read", "zotero:write"]
    return OAuthConfig(issuer, resource, jwks, scopes)


class JWTTokenVerifier:
    """Verify externally issued JWT access tokens against a JWKS endpoint."""

    def __init__(self, config: OAuthConfig):
        self.config = config
        self._jwks = jwt.PyJWKClient(config.jwks_url, cache_keys=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            algorithm = signing_key.algorithm_name or "RS256"
            claims = await asyncio.to_thread(
                jwt.decode,
                token,
                signing_key.key,
                algorithms=[algorithm],
                audience=self.config.resource_server_url,
                issuer=self.config.issuer_url,
                options={"require": ["exp", "iss", "aud"]},
            )
        except Exception:
            return None

        raw_scopes = claims.get("scope", claims.get("scp", []))
        scopes = raw_scopes.split() if isinstance(raw_scopes, str) else list(raw_scopes)
        client_id = str(
            claims.get("client_id") or claims.get("azp") or claims.get("sub") or "unknown"
        )
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.config.resource_server_url,
        )


def build_mcp_auth() -> tuple[AuthSettings | None, JWTTokenVerifier | None]:
    """Build MCP server auth objects when an external issuer is configured."""

    config = oauth_config_from_env()
    if config is None:
        return None, None
    settings = AuthSettings(
        issuer_url=config.issuer_url,
        resource_server_url=config.resource_server_url,
        required_scopes=config.scopes,
        service_documentation_url="https://github.com/RaulSimpetru/zotero-library-mcp",
    )
    return settings, JWTTokenVerifier(config)


def oauth_tool_meta(extra: dict | None = None) -> dict | None:
    """Return ChatGPT-compatible OAuth descriptor metadata when enabled."""

    config = oauth_config_from_env()
    if config is None and not extra:
        return None
    meta = dict(extra or {})
    if config is not None:
        schemes = [{"type": "oauth2", "scopes": config.scopes}]
        meta["securitySchemes"] = schemes
        meta["openai/securitySchemes"] = schemes
    return meta
