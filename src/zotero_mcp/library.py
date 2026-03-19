"""Tools for browsing, searching, and managing library items."""

import os
import re
import shutil
import tempfile

import bibtexparser
import httpx
from fuzzysearch import find_near_matches

from ._helpers import (
    _download_pdf,
    _fmt_item,
    _get_zot,
    _resolve_doi,
)


def register(mcp):
    @mcp.tool()
    async def get_unfiled_items(limit: int = 25) -> str:
        """Get items that are not in any collection (unfiled items).

        Args:
            limit: Maximum number of items to return (default 25)
        """
        zot = _get_zot()

        try:
            all_items = zot.everything(zot.top())
        except Exception as e:
            return f"Could not fetch items: {e}"

        unfiled = []
        for item in all_items:
            data = item.get("data", {})
            if data.get("itemType") in ("attachment", "note"):
                continue
            if not data.get("collections"):
                unfiled.append(data)

        if not unfiled:
            return "No unfiled items."

        lines = [_fmt_item(item) for item in unfiled[:limit]]
        return f"{len(unfiled)} unfiled items (showing {len(lines)}):\n" + "\n".join(lines)

    def _fuzzy_search_items(zot, query: str, limit: int) -> list[dict]:
        """Fuzzy-match query against all library item titles and authors."""
        query_norm = re.sub(r"\s+", " ", query.strip().lower())
        if not query_norm:
            return []

        all_items = zot.everything(zot.top())
        max_dist = max(1, len(query_norm) // 4)
        scored = []

        for item in all_items:
            data = item.get("data", {})
            if data.get("itemType") in ("attachment", "note"):
                continue

            title = data.get("title", "")
            creators = data.get("creators", [])
            author_str = " ".join(
                f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
                for c in creators
            )
            searchable = f"{title} {author_str}".lower()

            matches = find_near_matches(query_norm, searchable, max_l_dist=max_dist)
            if matches:
                best_dist = min(m.dist for m in matches)
                scored.append((best_dist, data))

        scored.sort(key=lambda x: x[0])
        return [data for _, data in scored[:limit]]

    @mcp.tool()
    async def search_library(query: str, limit: int = 10) -> str:
        """Search your Zotero library. Falls back to fuzzy matching if the
        exact search returns no results.

        Args:
            query: Search query (searches titles, authors, tags, etc.)
            limit: Maximum number of results (default 10)
        """
        zot = _get_zot()
        results = zot.items(q=query, limit=limit)

        if results:
            lines = [_fmt_item(item.get("data", {})) for item in results]
            return "\n".join(lines)

        # Fuzzy fallback
        fuzzy_results = _fuzzy_search_items(zot, query, limit)
        if not fuzzy_results:
            return "No results."

        lines = [_fmt_item(item) for item in fuzzy_results]
        return f"No exact matches — fuzzy results:\n" + "\n".join(lines)

    @mcp.tool()
    async def get_item_details(item_key: str) -> str:
        """Get full details of a Zotero item by its key.

        Args:
            item_key: The Zotero item key
        """
        zot = _get_zot()

        try:
            item = zot.item(item_key)
        except Exception as e:
            return f"Could not find item {item_key}: {e}"

        data = item.get("data", {})
        lines = [f"[{item_key}] {data.get('title', '?')}"]

        creators = data.get("creators", [])
        if creators:
            names = [f"{c.get('firstName','')} {c.get('lastName','')}".strip() for c in creators]
            lines.append(f"Authors: {', '.join(names)}")

        fields = [
            ("Type", "itemType"), ("Date", "date"), ("DOI", "DOI"),
            ("Journal", "publicationTitle"), ("Vol", "volume"),
            ("Issue", "issue"), ("Pages", "pages"),
        ]
        for label, key in fields:
            val = data.get(key, "")
            if val:
                lines.append(f"{label}: {val}")

        tags = data.get("tags", [])
        if tags:
            lines.append(f"Tags: {', '.join(t.get('tag', '') for t in tags)}")

        abstract = data.get("abstractNote", "")
        if abstract:
            lines.append(f"Abstract: {abstract}")

        return "\n".join(lines)

    @mcp.tool()
    async def get_bibtex(
        item_keys: list[str] | None = None,
        collection_id: str | None = None,
        save_path: str | None = None,
    ) -> str:
        """Export BibTeX entries from your Zotero library.

        Can export specific items, an entire collection, or your whole library.
        Use save_path to write directly to a .bib file instead of returning the
        full content (recommended for large exports to save tokens).

        Args:
            item_keys: Optional list of item keys to export. If omitted, exports collection or full library.
            collection_id: Optional collection key to export all items from.
            save_path: Optional file path to save the .bib output to (e.g. "/path/to/refs.bib").
        """
        zot = _get_zot()

        def _bib_to_str(result: object) -> str:
            if isinstance(result, bibtexparser.bibdatabase.BibDatabase):
                return bibtexparser.dumps(result)
            return str(result)

        try:
            if item_keys:
                parts = []
                for key in item_keys:
                    result = zot.item(key, format="bibtex")
                    if result:
                        parts.append(_bib_to_str(result).strip())
                if not parts:
                    return "No BibTeX data available for the specified items."
                bib = "\n\n".join(parts)
            elif collection_id:
                bib = _bib_to_str(zot.collection_items(collection_id, format="bibtex"))
            else:
                bib = _bib_to_str(zot.items(format="bibtex"))
        except Exception as e:
            return f"Could not export BibTeX: {e}"

        if not bib.strip():
            return "No BibTeX data available."

        if save_path:
            try:
                with open(os.path.expanduser(save_path), "w", encoding="utf-8") as f:
                    f.write(bib)
                n_entries = bib.count("@")
                return f"Saved {n_entries} BibTeX entries to {save_path}"
            except Exception as e:
                return f"Failed to save file: {e}"

        return bib

    @mcp.tool()
    async def get_item_fulltext(item_key: str) -> str:
        """Get the full text of a paper by downloading its PDF.

        Downloads the PDF attachment to a temporary file and returns the
        path so you can read it directly. This gives you the actual
        formatted PDF content rather than Zotero's plain-text index.

        Args:
            item_key: The Zotero item key (the parent item, not the attachment)
        """
        zot = _get_zot()

        try:
            tmp_path, att_key = await _download_pdf(zot, item_key)
        except Exception:
            tmp_path = None

        if tmp_path:
            stable_path = tempfile.mktemp(suffix=".pdf", prefix="zotero_fulltext_")
            shutil.move(tmp_path, stable_path)
            return (
                f"PDF downloaded to: {stable_path}\n"
                f"Read this PDF file to access the full text of the paper."
            )

        # Fallback to Zotero's plain-text index if no PDF available
        try:
            children = zot.children(item_key)
        except Exception as e:
            return f"Could not find item {item_key}: {e}"

        for child in children:
            child_data = child.get("data", {})
            child_key = child_data.get("key", "")
            if child_data.get("itemType") == "attachment" and child_key:
                try:
                    ft = zot.fulltext_item(child_key)
                    content = ft.get("content", "")
                    if content:
                        return content
                except Exception:
                    continue

        return "No full-text content available for this item."

    @mcp.tool()
    async def delete_item(item_key: str) -> str:
        """Permanently delete an item from your Zotero library.

        Args:
            item_key: The Zotero item key to delete
        """
        zot = _get_zot()

        try:
            item = zot.item(item_key)
        except Exception as e:
            return f"Could not find item {item_key}: {e}"

        title = item.get("data", {}).get("title", item_key)

        try:
            zot.delete_item(item)
        except Exception as e:
            return f"Failed to delete item: {e}"

        return f"Deleted [{item_key}] {title}"

    @mcp.tool()
    async def get_recent_items(limit: int = 10) -> str:
        """Get recently added items from your Zotero library.

        Args:
            limit: Maximum number of items to return (default 10)
        """
        zot = _get_zot()

        try:
            results = zot.items(sort="dateAdded", direction="desc", limit=limit)
        except Exception as e:
            return f"Could not fetch recent items: {e}"

        if not results:
            return "No items."

        lines = []
        for item in results:
            data = item.get("data", {})
            if data.get("itemType") not in ("attachment", "note"):
                lines.append(_fmt_item(data))

        return "\n".join(lines) if lines else "No items."

    @mcp.tool()
    async def verify_items(limit: int = 10) -> str:
        """Verify that recently added items have valid DOIs that match CrossRef metadata.

        Re-resolves each item's DOI via CrossRef and compares the title. Reports
        items that have no DOI, DOIs that don't resolve, or title mismatches.

        Args:
            limit: Number of recent items to check (default 10)
        """
        zot = _get_zot()

        try:
            results = zot.items(sort="dateAdded", direction="desc", limit=limit)
        except Exception as e:
            return f"Could not fetch items: {e}"

        items = [
            item for item in results
            if item.get("data", {}).get("itemType") not in ("attachment", "note")
        ]

        if not items:
            return "No items to verify."

        lines = []
        ok_count = 0

        for item in items:
            data = item.get("data", {})
            key = data.get("key", "?")
            title = data.get("title", "Untitled")
            doi = data.get("DOI", "")

            if not doi:
                lines.append(f"[{key}] SKIP — no DOI: {title}")
                continue

            try:
                cr_data = await _resolve_doi(doi)
            except Exception:
                lines.append(f"[{key}] FAIL — DOI does not resolve: {doi}")
                continue

            msg = cr_data.get("message", cr_data)
            cr_titles = msg.get("title", [])
            cr_title = cr_titles[0] if cr_titles else ""

            zot_norm = re.sub(r"\s+", " ", title.strip().lower())
            cr_norm = re.sub(r"\s+", " ", cr_title.strip().lower())

            if zot_norm == cr_norm:
                lines.append(f"[{key}] OK — {title}")
                ok_count += 1
            else:
                lines.append(
                    f"[{key}] MISMATCH — Zotero: {title}\n"
                    f"        CrossRef: {cr_title}"
                )

        header = f"Verified {len(items)} items: {ok_count} OK"
        return header + "\n" + "\n".join(lines)
