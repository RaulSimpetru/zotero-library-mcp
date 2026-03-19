"""Unit tests for fuzzy library search in library.py."""

import asyncio
from unittest.mock import MagicMock

import pytest

from zotero_mcp.library import register


def _make_item(title, authors=None, item_type="journalArticle", key="ABC123"):
    """Create a fake Zotero item dict."""
    creators = []
    for name in (authors or []):
        parts = name.split(" ", 1)
        creators.append({
            "firstName": parts[0],
            "lastName": parts[1] if len(parts) > 1 else "",
        })
    return {
        "data": {
            "key": key,
            "title": title,
            "itemType": item_type,
            "creators": creators,
            "collections": [],
            "tags": [],
        }
    }


FAKE_ITEMS = [
    _make_item("Attention Is All You Need", ["Ashish Vaswani"], key="K001"),
    _make_item("Deep Residual Learning for Image Recognition", ["Kaiming He"], key="K002"),
    _make_item("BERT: Pre-training of Deep Bidirectional Transformers", ["Jacob Devlin"], key="K003"),
    _make_item("Generative Adversarial Networks", ["Ian Goodfellow"], key="K004"),
    _make_item("ImageNet Classification with Deep CNNs", ["Alex Krizhevsky"], key="K005"),
]


def _setup_mcp():
    """Register tools on a mock MCP and return registered functions."""
    mcp = MagicMock()
    registered = {}

    def tool_decorator():
        def wrapper(fn):
            registered[fn.__name__] = fn
            return fn
        return wrapper

    mcp.tool = tool_decorator
    register(mcp)
    return registered


class TestFuzzyLibrarySearch:
    def test_exact_search_returns_results(self, monkeypatch):
        registered = _setup_mcp()

        mock_zot = MagicMock()
        mock_zot.items.return_value = [FAKE_ITEMS[0]]

        import zotero_mcp.library as lib_mod
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: mock_zot)

        result = asyncio.run(registered["search_library"]("Attention Is All You Need"))
        assert "Attention Is All You Need" in result
        assert "fuzzy" not in result.lower()

    def test_fuzzy_fallback_on_typo(self, monkeypatch):
        registered = _setup_mcp()

        mock_zot = MagicMock()
        mock_zot.items.return_value = []
        mock_zot.top.return_value = "top_call"
        mock_zot.everything.return_value = FAKE_ITEMS

        import zotero_mcp.library as lib_mod
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: mock_zot)

        result = asyncio.run(registered["search_library"]("Atention Is All You Need"))
        assert "Attention Is All You Need" in result
        assert "fuzzy" in result.lower()

    def test_fuzzy_no_match(self, monkeypatch):
        registered = _setup_mcp()

        mock_zot = MagicMock()
        mock_zot.items.return_value = []
        mock_zot.top.return_value = "top_call"
        mock_zot.everything.return_value = FAKE_ITEMS

        import zotero_mcp.library as lib_mod
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: mock_zot)

        result = asyncio.run(registered["search_library"]("quantum teleportation xyz"))
        assert result == "No results."

    def test_fuzzy_matches_author(self, monkeypatch):
        registered = _setup_mcp()

        mock_zot = MagicMock()
        mock_zot.items.return_value = []
        mock_zot.top.return_value = "top_call"
        mock_zot.everything.return_value = FAKE_ITEMS

        import zotero_mcp.library as lib_mod
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: mock_zot)

        result = asyncio.run(registered["search_library"]("Goodfelow"))
        assert "Generative Adversarial" in result

    def test_fuzzy_skips_attachments(self, monkeypatch):
        registered = _setup_mcp()

        items_with_attachment = FAKE_ITEMS + [
            _make_item("Some PDF", item_type="attachment", key="ATT1"),
        ]

        mock_zot = MagicMock()
        mock_zot.items.return_value = []
        mock_zot.top.return_value = "top_call"
        mock_zot.everything.return_value = items_with_attachment

        import zotero_mcp.library as lib_mod
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: mock_zot)

        result = asyncio.run(registered["search_library"]("Atention Is All"))
        assert "Some PDF" not in result

    def test_fuzzy_respects_limit(self, monkeypatch):
        registered = _setup_mcp()

        deep_items = [
            _make_item(f"Deep Learning Paper {i}", key=f"D{i:03d}")
            for i in range(20)
        ]

        mock_zot = MagicMock()
        mock_zot.items.return_value = []
        mock_zot.top.return_value = "top_call"
        mock_zot.everything.return_value = deep_items

        import zotero_mcp.library as lib_mod
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: mock_zot)

        result = asyncio.run(registered["search_library"]("Deep Learning", limit=3))
        item_lines = [l for l in result.split("\n") if l.startswith("[")]
        assert len(item_lines) <= 3
