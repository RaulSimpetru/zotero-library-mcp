"""Unit tests for versioned item mutation helpers."""

import asyncio
from unittest.mock import MagicMock

from zotero_mcp.library import _key_write_access, register


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


def test_key_write_access_supports_user_and_group_libraries():
    key_info = {
        "access": {
            "user": {"library": True, "write": True},
            "groups": {
                "all": {"library": True, "write": False},
                "456": {"library": True, "write": True},
            },
        }
    }

    assert _key_write_access(key_info, "users", "123") is True
    assert _key_write_access(key_info, "groups", "456") is True
    assert _key_write_access(key_info, "group", "789") is False
    assert _key_write_access({}, "users", "123") is None


def test_health_check_reports_write_access_without_mutating(monkeypatch):
    registered = _setup_mcp()
    zot = _mock_zotero()
    zot.items.return_value = []
    zot.key_info.return_value = {
        "access": {"user": {"library": True, "write": True}}
    }

    import zotero_mcp.library as library

    monkeypatch.setattr(library, "_get_zot", lambda: zot)
    result = asyncio.run(registered["health_check"]())

    assert result["read_access"] is True
    assert result["write_access"] is True
    assert "non-destructively" in result["write_access_note"]
    zot.key_info.assert_called_once_with()
    zot.create_items.assert_not_called()
    zot.update_item.assert_not_called()
    zot.delete_item.assert_not_called()
