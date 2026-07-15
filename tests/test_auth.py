"""Configuration checks for optional OAuth-protected HTTP deployments."""

import pytest

from zotero_mcp.auth import oauth_config_from_env


def test_oauth_config_rejects_partial_environment(monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_OAUTH_ISSUER", "https://auth.example.com")
    monkeypatch.delenv("ZOTERO_MCP_OAUTH_RESOURCE", raising=False)
    monkeypatch.delenv("ZOTERO_MCP_OAUTH_JWKS_URL", raising=False)

    with pytest.raises(ValueError, match="incomplete"):
        oauth_config_from_env()


def test_oauth_config_loads_scopes(monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_OAUTH_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("ZOTERO_MCP_OAUTH_RESOURCE", "https://zotero.example.com")
    monkeypatch.setenv("ZOTERO_MCP_OAUTH_JWKS_URL", "https://auth.example.com/jwks.json")
    monkeypatch.setenv("ZOTERO_MCP_OAUTH_SCOPES", "zotero:read,zotero:write")

    config = oauth_config_from_env()

    assert config.issuer_url == "https://auth.example.com"
    assert config.scopes == ["zotero:read", "zotero:write"]
