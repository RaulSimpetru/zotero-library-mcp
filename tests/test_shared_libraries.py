"""Library discovery, request isolation, and group storage regression tests."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import bibtexparser
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from zotero_mcp import _helpers as helpers
from zotero_mcp.runtime import configure_runtime
from zotero_mcp.server import mcp


@pytest.fixture
def clients(monkeypatch):
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    monkeypatch.setenv("ZOTERO_API_KEY", "test-key")
    created = []

    def factory(library_id, library_type, api_key):
        assert api_key == "test-key"
        zot = MagicMock()
        zot.library_id = library_id
        zot.library_type = library_type + "s"
        zot.endpoint = "https://api.zotero.org"
        zot.request.headers = {"Total-Results": "1"}
        zot.items.return_value = [
            {
                "data": {
                    "key": "SAMEKEY1",
                    "title": f"{library_type}/{library_id}",
                    "itemType": "journalArticle",
                }
            }
        ]
        zot.item.return_value = {
            "version": 7,
            "data": {
                "key": "SAMEKEY1",
                "title": f"{library_type}/{library_id}",
                "itemType": "journalArticle",
                "abstractNote": "Fallback abstract",
            },
        }
        zot.children.return_value = []
        zot.key_info.return_value = {
            "userID": 123,
            "access": {
                "user": {"library": True, "write": True},
                "groups": {
                    "all": {"library": True, "write": False},
                    "456": {"library": True, "write": True},
                },
            },
        }
        zot.groups.return_value = [{"id": 456, "data": {"name": "Shared Review"}}]
        created.append(zot)
        return zot

    monkeypatch.setattr(helpers.zotero, "Zotero", factory)
    return created


def call(name, **arguments):
    return asyncio.run(mcp.call_tool(name, arguments))


def test_discovery_uses_key_owner_when_default_is_group(clients, monkeypatch):
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "456")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "group")
    result = call("list_libraries").structured_content
    assert result["default_library"] == {"library_id": "456", "library_type": "group"}
    assert result["libraries"][0]["library_id"] == "123"
    shared = result["libraries"][1]
    assert shared["name"] == "Shared Review"
    assert shared["is_default"] is True
    assert shared["key_permissions"]["write"] is True
    clients[1].groups.assert_called_once_with(limit=100, start=0)
    assert clients[1].library_type == "users"
    assert "test-key" not in str(result)


def test_discovery_pagination_and_read_only_permissions(clients, monkeypatch):
    original = helpers.zotero.Zotero

    def factory(*args):
        zot = original(*args)
        zot.request.headers = {"Total-Results": "3"}
        zot.groups.return_value = [{"id": 789, "data": {"name": "Read only"}}]
        return zot

    monkeypatch.setattr(helpers.zotero, "Zotero", factory)
    result = call("list_libraries", limit=1, start=1).structured_content
    assert result["next_start"] == 2
    assert result["group_total"] == 3
    assert result["libraries"][1]["key_permissions"]["write"] is False
    clients[1].groups.assert_called_once_with(limit=1, start=1)


@pytest.mark.parametrize(
    "arguments",
    [
        {"library_id": "456"},
        {"library_type": "group"},
        {"library_id": "../123", "library_type": "group"},
        {"library_id": "", "library_type": "group"},
        {"library_id": "0", "library_type": "group"},
    ],
)
def test_invalid_target_never_creates_a_client(clients, arguments):
    result = call("search_library", query="paper", **arguments)
    assert result.is_error is True
    assert not clients


def test_invalid_library_type_is_rejected_by_mcp_schema(clients):
    with pytest.raises(ToolError, match="Input should be 'user' or 'group'"):
        call("search_library", query="paper", library_id="456", library_type="invalid")
    assert not clients


def test_concurrent_requests_keep_their_own_library_and_default(clients):
    async def searches():
        return await asyncio.gather(
            mcp.call_tool("search_library", {"query": "paper"}),
            mcp.call_tool(
                "search_library",
                {
                    "query": "paper",
                    "library_id": "456",
                    "library_type": "group",
                },
            ),
            mcp.call_tool(
                "search_library",
                {
                    "query": "paper",
                    "library_id": "789",
                    "library_type": "group",
                },
            ),
        )

    results = asyncio.run(searches())
    for result, target in zip(results, ["user/123", "group/456", "group/789"]):
        assert result.is_error is False
        assert target in result.structured_content["result"]
    assert (
        "user/123" in call("search_library", query="paper").structured_content["result"]
    )


def test_mutation_uses_selected_group_only(clients):
    result = call(
        "trash_item", item_key="SAMEKEY1", library_id="456", library_type="group"
    )
    assert result.is_error is False
    assert len(clients) == 1
    clients[0].client.patch.assert_called_once_with(
        "https://api.zotero.org/groups/456/items/SAMEKEY1",
        headers={"If-Unmodified-Since-Version": "7"},
        json={"deleted": True},
    )


def test_denied_group_does_not_fall_back_to_personal_library(clients, monkeypatch):
    original = helpers.zotero.Zotero

    def factory(*args):
        zot = original(*args)
        zot.items.side_effect = PermissionError("Forbidden")
        return zot

    monkeypatch.setattr(helpers.zotero, "Zotero", factory)
    result = call(
        "search_library", query="paper", library_id="456", library_type="group"
    )
    assert result.is_error is True
    assert len(clients) == 1
    assert clients[0].library_id == "456"


def test_nested_search_and_fetch_preserve_library(clients):
    result = call("search", query="paper", library_id="456", library_type="group")
    assert result.structured_content["results"][0]["title"] == "group/456"
    result = call("fetch", id="SAMEKEY1", library_id="456", library_type="group")
    assert result.structured_content["title"] == "group/456"
    assert all(z.library_id == "456" for z in clients)


def test_save_bibtex_forwards_library_to_export(clients, monkeypatch, tmp_path):
    original = helpers.zotero.Zotero

    def factory(*args):
        zot = original(*args)
        zot.item.return_value = bibtexparser.loads(
            "@article{shared, title={Shared library paper}, year={2026}}"
        )
        return zot

    monkeypatch.setattr(helpers.zotero, "Zotero", factory)
    configure_runtime(transport="stdio")
    destination = tmp_path / "shared.bib"
    result = call(
        "save_bibtex",
        save_path=str(destination),
        item_keys=["SAMEKEY1"],
        library_id="456",
        library_type="group",
    )
    assert result.is_error is False
    assert "Shared library paper" in destination.read_text()
    assert [z.library_id for z in clients] == ["456"]


def test_every_library_tool_exposes_optional_target():
    for tool in asyncio.run(mcp.list_tools()):
        if tool.name == "list_libraries":
            continue
        schema = tool.input_schema
        for field in ["library_id", "library_type"]:
            assert field in schema["properties"], tool.name
            assert field not in schema.get("required", []), tool.name


def test_webdav_is_only_for_configured_personal_library(clients, monkeypatch):
    monkeypatch.setattr(
        helpers, "_webdav_config", lambda: ("https://dav.example", "u", "p")
    )
    assert helpers._use_webdav(helpers._get_zot()) is True
    assert helpers._use_webdav(helpers._get_zot("456", "group")) is False
    assert helpers._use_webdav(helpers._get_zot("789", "user")) is False


def test_group_pdf_uses_zotero_storage_with_webdav_configured(clients, monkeypatch):
    monkeypatch.setattr(
        helpers, "_webdav_config", lambda: ("https://dav.example", "u", "p")
    )
    zot = helpers._get_zot("456", "group")
    zot.children.return_value = [
        {
            "data": {
                "key": "PDF12345",
                "itemType": "attachment",
                "contentType": "application/pdf",
            }
        }
    ]
    zot.file.return_value = b"%PDF-1.7\n%%EOF\n"
    path, _ = asyncio.run(helpers._download_pdf(zot, "SAMEKEY1"))
    try:
        assert Path(path).read_bytes().startswith(b"%PDF-")
        zot.file.assert_called_once_with("PDF12345")
    finally:
        Path(path).unlink()


def test_group_pdf_upload_uses_zotero_storage(clients, monkeypatch, tmp_path):
    monkeypatch.setattr(
        helpers, "_webdav_config", lambda: ("https://dav.example", "u", "p")
    )
    zot = helpers._get_zot("456", "group")
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
    monkeypatch.setattr(
        helpers,
        "_download_file_from_url",
        AsyncMock(return_value=(str(pdf), "application/pdf")),
    )
    local_upload = AsyncMock(return_value="PDF12345")
    webdav_upload = AsyncMock()
    monkeypatch.setattr(helpers, "_attach_file_local", local_upload)
    monkeypatch.setattr(helpers, "_attach_file_webdav", webdav_upload)
    result = asyncio.run(
        helpers._attach_pdf_from_url(zot, "SAMEKEY1", "https://example.org/paper.pdf")
    )
    assert result == "PDF12345"
    local_upload.assert_awaited_once_with(zot, "SAMEKEY1", str(pdf))
    webdav_upload.assert_not_awaited()
    assert not pdf.exists()
