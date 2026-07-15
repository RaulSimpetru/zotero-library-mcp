"""Unit tests for versioned item mutation helpers."""

import asyncio
from unittest.mock import MagicMock

from zotero_mcp.library import register


def _setup_mcp():
    mcp = MagicMock()
    registered = {}

    def tool_decorator(**_kwargs):
        def wrapper(fn):
            registered[fn.__name__] = fn
            return fn

        return wrapper

    mcp.tool = tool_decorator
    register(mcp)
    return registered


def _mock_zotero():
    zot = MagicMock()
    zot.endpoint = "https://api.zotero.org"
    zot.library_type = "users"
    zot.library_id = "123"
    zot.item.return_value = {
        "version": 7,
        "data": {
            "key": "ABCD1234",
            "version": 7,
            "itemType": "journalArticle",
            "title": "Original title",
        },
    }
    zot.item_template.return_value = {
        "itemType": "journalArticle",
        "title": "",
        "publicationTitle": "",
        "creators": [],
    }
    return zot


def test_metadata_update_uses_type_template_and_partial_patch(monkeypatch):
    registered = _setup_mcp()
    zot = _mock_zotero()

    import zotero_mcp.library as library

    monkeypatch.setattr(library, "_get_zot", lambda: zot)
    result = asyncio.run(
        registered["update_item_metadata"](
            "ABCD1234",
            {"publicationTitle": "Testing Quarterly"},
        )
    )

    assert result == "Updated [ABCD1234] Original title"
    zot.client.patch.assert_called_once_with(
        "https://api.zotero.org/users/123/items/ABCD1234",
        headers={"If-Unmodified-Since-Version": "7"},
        json={"publicationTitle": "Testing Quarterly"},
    )


def test_metadata_update_rejects_field_invalid_for_item_type(monkeypatch):
    registered = _setup_mcp()
    zot = _mock_zotero()

    import zotero_mcp.library as library

    monkeypatch.setattr(library, "_get_zot", lambda: zot)
    result = asyncio.run(
        registered["update_item_metadata"]("ABCD1234", {"publisher": "Wrong field"})
    )

    assert result.isError is True
    assert "Unsupported field" in result.structuredContent["result"]
    zot.client.patch.assert_not_called()


def test_trash_and_restore_use_deleted_partial_patch(monkeypatch):
    registered = _setup_mcp()
    zot = _mock_zotero()

    import zotero_mcp.library as library

    monkeypatch.setattr(library, "_get_zot", lambda: zot)
    trashed = asyncio.run(registered["trash_item"]("ABCD1234"))
    restored = asyncio.run(registered["restore_item"]("ABCD1234"))

    assert "to trash" in trashed
    assert "from trash" in restored
    assert zot.client.patch.call_args_list[0].kwargs["json"] == {"deleted": True}
    assert zot.client.patch.call_args_list[1].kwargs["json"] == {"deleted": False}
    for call in zot.client.patch.call_args_list:
        assert call.kwargs["headers"] == {"If-Unmodified-Since-Version": "7"}
