"""Regression tests for remote file, URL, and metadata safety helpers."""

import asyncio
import socket

import pytest

from zotero_mcp._helpers import _crossref_to_zotero, _validate_public_url
from zotero_mcp.file_resources import read_temp_resource, register_temp_resource
from zotero_mcp.responses import resource_result, sanitize_error_message
from zotero_mcp.runtime import configure_runtime, validate_server_path
from zotero_mcp.papers import _normalize_isbn


def test_http_transport_denies_server_paths_by_default(tmp_path):
    source = tmp_path / "secret.txt"
    source.write_text("private", encoding="utf-8")
    configure_runtime(transport="streamable-http")
    try:
        with pytest.raises(PermissionError, match="disabled for HTTP"):
            validate_server_path(str(source))
    finally:
        configure_runtime(transport="stdio")


def test_http_path_access_is_confined_to_roots(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inside = allowed / "paper.pdf"
    inside.write_bytes(b"%PDF-test")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-test")

    configure_runtime(
        transport="streamable-http",
        allow_server_files=True,
        file_roots=[str(allowed)],
    )
    try:
        assert validate_server_path(str(inside)) == inside.resolve()
        with pytest.raises(PermissionError, match="outside"):
            validate_server_path(str(outside))
    finally:
        configure_runtime(transport="stdio")


def test_public_url_validation_rejects_private_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )

    with pytest.raises(ValueError, match="private or special-use"):
        asyncio.run(_validate_public_url("https://files.example.test/paper.pdf"))


def test_crossref_mapping_uses_type_specific_fields():
    book = _crossref_to_zotero(
        {
            "message": {
                "type": "book",
                "title": ["A Book"],
                "container-title": ["Not a Journal"],
                "publisher": "Example Press",
                "ISBN": ["9780000000000"],
                "DOI": "10.1000/book",
            }
        }
    )
    conference = _crossref_to_zotero(
        {
            "message": {
                "type": "proceedings-article",
                "title": ["A Paper"],
                "container-title": ["Proceedings of Testing"],
            }
        }
    )

    assert book["itemType"] == "book"
    assert book["publisher"] == "Example Press"
    assert "publicationTitle" not in book
    assert conference["proceedingsTitle"] == "Proceedings of Testing"
    assert "publicationTitle" not in conference


def test_isbn_normalization_validates_checksum():
    assert _normalize_isbn("978-0-262-04682-4") == "9780262046824"
    assert _normalize_isbn("0-262-03384-4") == "0262033844"
    with pytest.raises(ValueError, match="checksum"):
        _normalize_isbn("9780262046825")


def test_temporary_resource_uses_opaque_uri_and_reads_bytes(tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-resource")

    uri = register_temp_resource(
        str(path),
        name="paper.pdf",
        mime_type="application/pdf",
    )
    token = uri.rsplit("/", 1)[-1]

    assert uri.startswith("zotero://pdf/")
    assert "paper.pdf" not in uri
    assert read_temp_resource(token, expected_mime_type="application/pdf") == b"%PDF-resource"

    with pytest.raises(ValueError, match="media type"):
        read_temp_resource(token, expected_mime_type="image/png")


def test_resource_result_validates_with_string_tool_schema(tmp_path):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("resource-test")
    path = tmp_path / "result.pdf"
    path.write_bytes(b"%PDF-result")
    uri = register_temp_resource(str(path), name="result.pdf", mime_type="application/pdf")

    @server.tool()
    async def make_file() -> str:
        return resource_result(
            "ready",
            uri=uri,
            name="result.pdf",
            mime_type="application/pdf",
            size=path.stat().st_size,
        )

    result = asyncio.run(server.call_tool("make_file", {}))

    assert result.is_error is False
    assert result.structured_content["result"] == "ready"
    assert any(block.type == "resource_link" for block in result.content)


def test_error_sanitizer_redacts_secrets_and_url_paths(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "top-secret-token")
    message = (
        "GET https://api.zotero.org/users/123/items/ABC?key=top-secret-token "
        "with top-secret-token"
    )

    sanitized = sanitize_error_message(message)

    assert "top-secret-token" not in sanitized
    assert "/users/123" not in sanitized
    assert sanitized.count("[redacted]") >= 2
