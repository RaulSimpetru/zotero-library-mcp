"""Unit tests for BibTeX abstract stripping."""

import asyncio
from unittest.mock import MagicMock

import bibtexparser
import pytest

from zotero_mcp.library import register


def _setup_mcp():
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


def _make_bibdb(entries):
    """Create a BibDatabase with the given entries."""
    db = bibtexparser.bibdatabase.BibDatabase()
    db.entries = entries
    return db


SAMPLE_ENTRIES = [
    {
        "ID": "vaswani2017attention",
        "ENTRYTYPE": "article",
        "title": "Attention Is All You Need",
        "author": "Vaswani, Ashish",
        "year": "2017",
        "journal": "NeurIPS",
        "abstract": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.",
    },
    {
        "ID": "he2016deep",
        "ENTRYTYPE": "article",
        "title": "Deep Residual Learning",
        "author": "He, Kaiming",
        "year": "2016",
        "journal": "CVPR",
        "abstract": "Deeper neural networks are more difficult to train.",
    },
]


class TestBibtexStripAbstract:
    def test_abstract_stripped_by_default(self, monkeypatch):
        registered = _setup_mcp()
        mock_zot = MagicMock()
        mock_zot.item.return_value = _make_bibdb([SAMPLE_ENTRIES[0].copy()])

        import zotero_mcp.library as lib_mod
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: mock_zot)

        result = asyncio.run(registered["get_bibtex"](item_keys=["K001"]))
        assert "Attention Is All You Need" in result
        assert "abstract" not in result.lower()
        assert "dominant sequence" not in result

    def test_abstract_included_when_requested(self, monkeypatch):
        registered = _setup_mcp()
        mock_zot = MagicMock()
        mock_zot.item.return_value = _make_bibdb([SAMPLE_ENTRIES[0].copy()])

        import zotero_mcp.library as lib_mod
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: mock_zot)

        result = asyncio.run(registered["get_bibtex"](item_keys=["K001"], include_abstract=True))
        assert "abstract" in result.lower()
        assert "dominant sequence" in result

    def test_multiple_entries_stripped(self, monkeypatch):
        registered = _setup_mcp()
        mock_zot = MagicMock()
        entries = [e.copy() for e in SAMPLE_ENTRIES]
        mock_zot.item.side_effect = [_make_bibdb([entries[0]]), _make_bibdb([entries[1]])]

        import zotero_mcp.library as lib_mod
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: mock_zot)

        result = asyncio.run(registered["get_bibtex"](item_keys=["K001", "K002"]))
        assert "Attention Is All You Need" in result
        assert "Deep Residual Learning" in result
        assert "abstract" not in result.lower()

    def test_collection_export_stripped(self, monkeypatch):
        registered = _setup_mcp()
        mock_zot = MagicMock()
        entries = [e.copy() for e in SAMPLE_ENTRIES]
        mock_zot.collection_items.return_value = _make_bibdb(entries)

        import zotero_mcp.library as lib_mod
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: mock_zot)

        result = asyncio.run(registered["get_bibtex"](collection_id="COL1"))
        assert "abstract" not in result.lower()

    def test_raw_string_fallback_stripped(self, monkeypatch):
        """When Zotero returns a raw string instead of BibDatabase."""
        registered = _setup_mcp()
        mock_zot = MagicMock()
        raw_bib = """@article{vaswani2017,
  title = {Attention Is All You Need},
  author = {Vaswani, Ashish},
  abstract = {The dominant sequence transduction models are complex.},
  year = {2017},
}"""
        mock_zot.item.return_value = raw_bib

        import zotero_mcp.library as lib_mod
        monkeypatch.setattr(lib_mod, "_get_zot", lambda: mock_zot)

        result = asyncio.run(registered["get_bibtex"](item_keys=["K001"]))
        assert "Attention Is All You Need" in result
        assert "dominant sequence" not in result
