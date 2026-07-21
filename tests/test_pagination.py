"""Pagination tests for search_library and get_collection_items (issue #2)."""

import asyncio
from unittest.mock import MagicMock

import zotero_mcp.collections as col_mod
import zotero_mcp.library as lib_mod


def _make_item(title, item_type="journalArticle", key="ABC123"):
    return {"data": {"key": key, "title": title, "itemType": item_type, "creators": [], "tags": []}}


def _setup(register):
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


def _mock_zot(total="566"):
    zot = MagicMock()
    zot.request.headers = {"Total-Results": total}
    return zot


class TestSearchLibraryPagination:
    def test_start_and_limit_passed_through(self, monkeypatch):
        registered = _setup(lib_mod.register)
        zot = _mock_zot()
        zot.items.return_value = [_make_item("Paper A", key="K100")]
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: zot)

        result = asyncio.run(registered["search_library"]("deep", limit=100, start=100))

        zot.items.assert_called_once_with(q="deep", limit=100, start=100)
        assert "Paper A" in result
        assert "Showing items 101–101 of 566." in result
        assert "start=101" in result

    def test_limit_cap_is_now_100(self, monkeypatch):
        registered = _setup(lib_mod.register)
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: _mock_zot())

        result = asyncio.run(registered["search_library"]("q", limit=101))
        assert "between 1 and 100" in str(result)

    def test_paging_past_exact_results_does_not_go_fuzzy(self, monkeypatch):
        registered = _setup(lib_mod.register)
        zot = _mock_zot(total="30")
        zot.items.return_value = []
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: zot)

        result = asyncio.run(registered["search_library"]("deep", start=30))
        assert "No more results." in result
        assert "fuzzy" not in result.lower()
        zot.top.assert_not_called()

    def test_footer_when_complete_has_no_next_page_hint(self, monkeypatch):
        registered = _setup(lib_mod.register)
        zot = _mock_zot(total="1")
        zot.items.return_value = [_make_item("Only One")]
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: zot)

        result = asyncio.run(registered["search_library"]("only"))
        assert "Showing items 1–1 of 1." in result
        assert "start=" not in result


class TestGetCollectionItemsPagination:
    def test_start_and_limit_passed_through(self, monkeypatch):
        registered = _setup(col_mod.register)
        zot = _mock_zot()
        zot.collection_items_top.return_value = [
            _make_item("Paper B", key="K101"),
            _make_item("Standalone note", item_type="note", key="N001"),
        ]
        monkeypatch.setattr(col_mod, "_get_zot", lambda: zot)

        result = asyncio.run(registered["get_collection_items"]("COLL1", limit=2, start=100))

        zot.collection_items_top.assert_called_once_with("COLL1", limit=2, start=100)
        assert "Paper B" in result
        assert "Standalone note" not in result
        # cursor advances by raw page length (2), not displayed count (1)
        assert "start=102" in result
        assert "of 566" in result

    def test_empty_collection(self, monkeypatch):
        registered = _setup(col_mod.register)
        zot = _mock_zot(total="0")
        zot.collection_items_top.return_value = []
        monkeypatch.setattr(col_mod, "_get_zot", lambda: zot)

        result = asyncio.run(registered["get_collection_items"]("COLL1"))
        assert result == "Empty collection."

    def test_paging_past_end(self, monkeypatch):
        registered = _setup(col_mod.register)
        zot = _mock_zot(total="566")
        zot.collection_items_top.return_value = []
        monkeypatch.setattr(col_mod, "_get_zot", lambda: zot)

        result = asyncio.run(registered["get_collection_items"]("COLL1", start=600))
        assert "No more items." in result
        assert "566 total" in result

    def test_invalid_start_rejected(self, monkeypatch):
        registered = _setup(col_mod.register)
        monkeypatch.setattr(col_mod, "_get_zot", lambda: _mock_zot())

        result = asyncio.run(registered["get_collection_items"]("COLL1", start=-1))
        assert "non-negative" in str(result)
