"""Compatibility checks for Codex and ChatGPT MCP clients."""

import asyncio

import pytest

from zotero_mcp.server import build_parser, configure_server, mcp


def _restore_server_settings(monkeypatch):
    for name in (
        "host",
        "port",
        "streamable_http_path",
        "stateless_http",
        "transport_security",
    ):
        monkeypatch.setattr(mcp.settings, name, getattr(mcp.settings, name))


def test_streamable_http_configuration_accepts_tunnel_host(monkeypatch):
    _restore_server_settings(monkeypatch)
    args = build_parser().parse_args(
        [
            "--transport",
            "streamable-http",
            "--port",
            "9000",
            "--allowed-host",
            "zotero.example.com",
            "--allowed-origin",
            "https://chatgpt.com",
        ]
    )

    configure_server(args)

    assert mcp.settings.port == 9000
    assert mcp.settings.streamable_http_path == "/mcp"
    assert "zotero.example.com" in mcp.settings.transport_security.allowed_hosts
    assert "https://chatgpt.com" in mcp.settings.transport_security.allowed_origins


def test_non_loopback_http_requires_an_allowed_host(monkeypatch):
    _restore_server_settings(monkeypatch)
    monkeypatch.delenv("ZOTERO_MCP_ALLOWED_HOSTS", raising=False)
    args = build_parser().parse_args(
        ["--transport", "streamable-http", "--host", "0.0.0.0"]
    )

    with pytest.raises(ValueError, match="requires at least one --allowed-host"):
        configure_server(args)


def test_non_loopback_http_requires_auth_or_explicit_acknowledgement(monkeypatch):
    _restore_server_settings(monkeypatch)
    args = build_parser().parse_args(
        [
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--allowed-host",
            "zotero.example.com",
        ]
    )

    with pytest.raises(ValueError, match="requires OAuth configuration"):
        configure_server(args)

    args.allow_unauthenticated_http = True
    configure_server(args)


def test_all_tools_declare_chatgpt_safety_annotations():
    tools = asyncio.run(mcp.list_tools())

    assert tools
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.readOnlyHint is not None, tool.name
        assert tool.annotations.destructiveHint is not None, tool.name
        assert tool.annotations.openWorldHint is not None, tool.name
        assert tool.title, tool.name
        assert tool.outputSchema is not None, tool.name


def test_destructive_tools_are_labeled():
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    for name in (
        "delete_item",
        "delete_collection",
        "delete_tags",
        "delete_note",
        "delete_annotation",
        "save_pdf",
        "save_bibtex",
    ):
        assert tools[name].annotations.destructiveHint is True

    assert tools["search_library"].annotations.readOnlyHint is True
    assert tools["download_pdf"].annotations.readOnlyHint is True


def test_chatgpt_file_input_schema_is_complete():
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    tool = tools["attach_file"]

    assert tool.meta["openai/fileParams"] == ["file"]
    file_schema = tool.inputSchema["$defs"]["OpenAIFile"]
    assert set(file_schema["properties"]) == {
        "download_url",
        "file_id",
        "mime_type",
        "file_name",
    }
    assert set(file_schema["required"]) == {"download_url", "file_id"}


def test_high_value_tools_are_registered():
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {
        "health_check",
        "list_attachments",
        "update_item_metadata",
        "add_item_from_metadata",
        "search_fulltext",
        "find_duplicates",
        "trash_item",
        "restore_item",
        "rename_collection",
        "move_collection",
        "unset_tag_color",
        "list_notes",
        "update_note",
        "delete_note",
        "update_annotation",
        "delete_annotation",
        "search",
        "fetch",
    } <= names


def test_invalid_tool_input_returns_mcp_error():
    result = asyncio.run(mcp.call_tool("search_library", {"query": ""}))

    assert result.isError is True
    assert result.structuredContent["result"] == "query must not be empty"
