"""Compatibility checks for Codex and ChatGPT MCP clients."""

import asyncio

import pytest

from zotero_mcp.server import build_parser, configure_server, mcp


def test_streamable_http_configuration_accepts_tunnel_host():
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

    options = configure_server(args)

    assert options["port"] == 9000
    assert options["streamable_http_path"] == "/mcp"
    assert "zotero.example.com" in options["transport_security"].allowed_hosts
    assert "https://chatgpt.com" in options["transport_security"].allowed_origins


def test_stdio_transport_takes_no_run_options():
    assert configure_server(build_parser().parse_args([])) == {}


def test_non_loopback_http_requires_an_allowed_host(monkeypatch):
    monkeypatch.delenv("ZOTERO_MCP_ALLOWED_HOSTS", raising=False)
    args = build_parser().parse_args(
        ["--transport", "streamable-http", "--host", "0.0.0.0"]
    )

    with pytest.raises(ValueError, match="requires at least one --allowed-host"):
        configure_server(args)


def test_non_loopback_http_requires_auth_or_explicit_acknowledgement():
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
        assert tool.annotations.read_only_hint is not None, tool.name
        assert tool.annotations.destructive_hint is not None, tool.name
        assert tool.annotations.open_world_hint is not None, tool.name
        assert tool.title, tool.name
        assert tool.output_schema is not None, tool.name


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
        assert tools[name].annotations.destructive_hint is True

    assert tools["search_library"].annotations.read_only_hint is True
    assert tools["download_pdf"].annotations.read_only_hint is True


def test_chatgpt_file_input_schema_is_complete():
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    tool = tools["attach_file"]

    assert tool.meta["openai/fileParams"] == ["file"]
    file_schema = tool.input_schema["$defs"]["OpenAIFile"]
    assert set(file_schema["properties"]) == {
        "download_url",
        "file_id",
        "mime_type",
        "file_name",
    }
    assert set(file_schema["required"]) == {"download_url", "file_id"}


def test_download_progress_context_is_not_exposed_as_tool_input():
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    assert set(tools["download_pdf"].input_schema["properties"]) == {
        "item_key",
        "attachment_key",
    }


def test_file_resource_templates_preserve_media_types():
    templates = {
        template.uri_template: template.mime_type
        for template in asyncio.run(mcp.list_resource_templates())
    }

    assert templates["zotero://pdf/{token}"] == "application/pdf"
    assert templates["zotero://image/{token}"] == "image/png"


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

    assert result.is_error is True
    assert result.structured_content["result"] == "query must not be empty"
