"""Tools for browsing, searching, and managing library items."""

import asyncio
import difflib
import re
from collections import defaultdict
from functools import partial
from pathlib import Path
from typing import Literal

import bibtexparser
import httpx
import pymupdf as fitz
from bibtexparser.bwriter import BibTexWriter
from fuzzysearch import find_near_matches
from mcp.types import CallToolResult

from ._helpers import (
    _all_children,
    _patch_item,
    _resolve_doi_csl,
    _download_pdf,
    _fmt_item,
    _get_zot,
    _page_footer,
    _resolve_doi,
    _total_results,
    _unpaywall_email,
    _use_webdav,
    _validate_limit,
    _validate_start,
    _zot_call,
)
from .papers import _first_isbn13, _normalize_doi
from .responses import tool_error
from .runtime import atomic_write, validate_server_path
from .tool_annotations import DESTRUCTIVE, READ_ONLY, READ_ONLY_OPEN_WORLD, WRITE


def _filter_top_level(items: list[dict]) -> list[dict]:
    return [
        item
        for item in items
        if item.get("data", {}).get("itemType") not in ("attachment", "note", "annotation")
    ]


def _key_write_access(
    key_info: dict[str, object],
    library_type: str,
    library_id: str,
) -> bool | None:
    """Read library write access from Zotero's non-mutating key metadata."""

    access = key_info.get("access")
    if not isinstance(access, dict):
        return None

    if library_type.rstrip("s") == "user":
        user_access = access.get("user")
        if not isinstance(user_access, dict):
            return None
        if "write" in user_access:
            return bool(user_access["write"])
        # Zotero lists only granted flags, so a read-only key omits "write".
        return False if user_access.get("library") else None

    groups_access = access.get("groups")
    if not isinstance(groups_access, dict):
        return None
    group_access = groups_access.get(str(library_id)) or groups_access.get("all")
    if not isinstance(group_access, dict) or "write" not in group_access:
        return None
    return bool(group_access["write"])


def _extract_pdf_text(path: str, max_chars: int) -> tuple[str, bool]:
    chunks: list[str] = []
    length = 0
    truncated = False
    with fitz.open(path) as document:
        for page in document:
            text = page.get_text("text")
            remaining = max_chars - length
            if remaining <= 0:
                truncated = True
                break
            chunks.append(text[:remaining])
            length += len(chunks[-1])
            if len(text) > remaining:
                truncated = True
                break
    return "".join(chunks), truncated


async def _recent_top_level(zot, limit: int) -> list[dict]:
    # ponytail: standalone notes are filtered after the fetch, so a library
    # whose newest items are standalone notes can return fewer than ``limit``.
    page = await _zot_call(
        zot.top,
        sort="dateAdded",
        direction="desc",
        limit=limit,
        itemType="-attachment",
    )
    return _filter_top_level(page)


MAX_CACHED_ITEMS = 5000
# (library type, library id) -> (library version, {item key: item})
_library_items: dict[tuple[str, str], tuple[int, dict[str, dict]]] = {}


def _library_version(zot) -> int | None:
    headers = getattr(getattr(zot, "request", None), "headers", None)
    value = headers.get("Last-Modified-Version") if headers is not None else None
    return int(value) if isinstance(value, str) and value.isdigit() else None


async def _cached_top_items(zot, new_zot) -> list[dict]:
    """Top-level items (up to MAX_CACHED_ITEMS), cached and kept current.

    The first call downloads the library; later calls ask Zotero only for
    what changed since the cached library version (two small requests).
    """

    cache_key = (str(zot.library_type), str(zot.library_id))
    cached = _library_items.get(cache_key)
    if cached is not None:
        version, items = cached
        # All changed items, not just top-level ones: an item that was moved
        # under a parent no longer appears in /items/top and must be dropped.
        changed: list[dict] = []
        new_version = None
        while True:
            page = await _zot_call(
                # Oldest change first, so edits made while paging move to the
                # end instead of shifting earlier pages. (Zotero rejects sort=version.)
                zot.items, since=version, includeTrashed=1, sort="dateModified",
                direction="asc", limit=100, start=len(changed),
            )
            if new_version is None:
                new_version = _library_version(zot)
            changed.extend(page)
            if len(page) < 100 or len(changed) >= 1000:
                break
        # ponytail: 1,000+ changes since the last call (bulk import, sync from
        # another device) triggers a full reload instead of further paging.
        if new_version is not None and len(changed) < 1000:
            deleted = await _zot_call(zot.deleted, since=version)
            for item in changed:
                data = item.get("data", {})
                if data.get("parentItem") or data.get("deleted"):
                    items.pop(data.get("key"), None)
                else:
                    items[data.get("key")] = item
            for key in deleted.get("items", []):
                items.pop(key, None)
            _library_items[cache_key] = (new_version, items)
            return list(items.values())

    fetched = await _top_items_bounded(zot, MAX_CACHED_ITEMS, new_zot)
    # ``zot`` made only the first page request, so this is the version the
    # download started from; anything changed later is picked up next time.
    version = _library_version(zot)
    if version is not None:
        _library_items[cache_key] = (
            version,
            {item.get("data", {}).get("key"): item for item in fetched},
        )
    return fetched


async def _top_items_bounded(zot, maximum: int, new_zot) -> list[dict]:
    """Fetch up to ``maximum`` top-level items, remaining pages in parallel."""

    first = await _zot_call(zot.top, limit=min(100, maximum), start=0)
    # Without a Total-Results header, assume more pages while page one is full;
    # pages past the end just come back empty.
    fallback = maximum if len(first) == min(100, maximum) else len(first)
    total = min(maximum, _total_results(zot, fallback))
    semaphore = asyncio.Semaphore(5)

    async def _page(start: int) -> list[dict]:
        # Separate Pyzotero instances: concurrent calls on one instance race
        # on its stored query parameters.
        async with semaphore:
            return await _zot_call(
                new_zot().top, limit=min(100, total - start), start=start
            )

    pages = await asyncio.gather(*(_page(start) for start in range(len(first), total, 100)))
    return first + [item for page in pages for item in page]


_web_library_urls: dict[tuple[str, str], str] = {}


async def _web_library_url(zot) -> str:
    """zotero.org base URL for a library's items, or "" when it is unknown."""

    library_type = str(zot.library_type).rstrip("s")
    cache_key = (library_type, str(zot.library_id))
    if cache_key not in _web_library_urls:
        url = ""
        if library_type == "group":
            url = f"https://www.zotero.org/groups/{zot.library_id}"
        else:
            # Personal web libraries live under the lowercase username slug;
            # /users/<id>/items/... returns 404.
            # ponytail: assumes slug = lowercased username with "_" for spaces.
            try:
                info = await _zot_call(zot.key_info)
                if str(info.get("userID")) == str(zot.library_id) and info.get("username"):
                    slug = info["username"].lower().replace(" ", "_")
                    url = f"https://www.zotero.org/{slug}"
            except Exception:
                pass
        _web_library_urls[cache_key] = url
    return _web_library_urls[cache_key]


def register(mcp):
    @mcp.tool(annotations=READ_ONLY)
    async def get_unfiled_items(
        limit: int = 25,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Get items that are not in any collection (unfiled items).

        Args:
            limit: Maximum number of items to return (default 25)
        """
        try:
            limit = _validate_limit(limit)
            _get_zot(library_id, library_type)  # validate the library arguments
        except Exception as e:
            return tool_error(str(e))

        unfiled = []
        start = 0
        page_size = 100
        batch_pages = 5

        async def _page(page_start: int) -> list[dict]:
            # Own instance per concurrent request (Pyzotero keeps query state).
            return await _zot_call(
                _get_zot(library_id, library_type).top, limit=page_size, start=page_start
            )

        # Stay bounded even for very large libraries, and fetch a few pages at
        # a time so early hits still stop the scan after at most one batch.
        exhausted = False
        while len(unfiled) < limit and start < 5000 and not exhausted:
            starts = range(start, min(start + batch_pages * page_size, 5000), page_size)
            try:
                pages = await asyncio.gather(*(_page(page_start) for page_start in starts))
            except Exception as e:
                return tool_error(f"Could not fetch items: {e}")
            for page in pages:
                for item in page:
                    data = item.get("data", {})
                    if data.get("itemType") in ("attachment", "note", "annotation"):
                        continue
                    if not data.get("collections"):
                        unfiled.append(data)
                        if len(unfiled) >= limit:
                            break
                if len(page) < page_size:
                    exhausted = True
                if len(unfiled) >= limit or exhausted:
                    break
            start += len(starts) * page_size

        if not unfiled:
            return "No unfiled items."

        lines = [_fmt_item(item) for item in unfiled]
        capped = start >= 5000 and not exhausted and len(unfiled) < limit
        suffix = " (scan capped at 5,000 items)" if capped else ""
        return f"Unfiled items (showing {len(lines)}){suffix}:\n" + "\n".join(lines)

    def _rank_fuzzy_items(all_items: list[dict], query: str) -> list[dict]:
        """Fuzzy-match query against a bounded set of item titles, authors, and tags."""
        query_norm = re.sub(r"\s+", " ", query.strip().lower())
        if not query_norm:
            return []

        # Short queries get no edit distance: one edit on 3 characters
        # matches roughly half of any library.
        max_dist = len(query_norm) // 4
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
            tag_str = " ".join(tag.get("tag", "") for tag in data.get("tags", []))
            searchable = f"{title} {author_str} {tag_str}".lower()

            matches = find_near_matches(query_norm, searchable, max_l_dist=max_dist)
            if matches:
                best_dist = min(m.dist for m in matches)
                scored.append((best_dist, data))

        scored.sort(key=lambda x: x[0])
        return [data for _, data in scored]

    @mcp.tool(annotations=READ_ONLY)
    async def search_library(
        query: str,
        limit: int = 10,
        start: int = 0,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Search your Zotero library. Falls back to fuzzy matching if the
        exact search returns no results.

        Args:
            query: Search query matched against titles, creators, and year
                (use search_fulltext for abstracts, notes, and PDF text)
            limit: Maximum number of results per page (default 10, max 100)
            start: Offset of the first result; pass the value suggested by the
                previous call's footer to fetch the next page (default 0)
        """
        try:
            limit = _validate_limit(limit, maximum=100)
            start = _validate_start(start)
            if not query.strip():
                return tool_error("query must not be empty")
            zot = _get_zot(library_id, library_type)
            # /items/top: /items would return matching attachments and notes
            # (e.g. "preprint" hits "Preprint PDF" attachments, not preprints).
            results = await _zot_call(zot.top, q=query.strip(), limit=limit, start=start)
        except Exception as exc:
            return tool_error(f"Could not search the Zotero library: {exc}")

        total = _total_results(zot, start + len(results))
        if results:
            lines = [_fmt_item(item.get("data", {})) for item in results]
            return "\n".join(lines) + _page_footer(start, len(results), total)
        if total:
            return "No more results." + _page_footer(start, 0, total)

        # Fuzzy fallback
        try:
            top_query = await _cached_top_items(
                zot, lambda: _get_zot(library_id, library_type)
            )
            # CPU-bound over up to 5,000 items; keep the event loop responsive.
            ranked = await asyncio.to_thread(_rank_fuzzy_items, top_query, query)
        except Exception as exc:
            return tool_error(f"Exact search returned no results and fuzzy search failed: {exc}")
        fuzzy_results = ranked[start : start + limit]
        if not ranked:
            return "No results."
        if not fuzzy_results:
            return "No more results." + _page_footer(start, 0, len(ranked))

        lines = [_fmt_item(item) for item in fuzzy_results]
        return (
            "No exact matches — fuzzy results:\n"
            + "\n".join(lines)
            + _page_footer(start, len(fuzzy_results), len(ranked))
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_item_details(
        item_key: str,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Get full details of a Zotero item by its key.

        Args:
            item_key: The Zotero item key
        """
        try:
            zot = _get_zot(library_id, library_type)
            item = await _zot_call(zot.item, item_key)
        except Exception as e:
            return tool_error(f"Could not find item {item_key}: {e}")

        data = item.get("data", {})
        lines = [f"[{item_key}] {data.get('title', '?')}"]

        creators = data.get("creators", [])
        if creators:
            names = [f"{c.get('firstName','')} {c.get('lastName','')}".strip() for c in creators]
            lines.append(f"Authors: {', '.join(names)}")

        fields = [
            ("Type", "itemType"), ("Date", "date"), ("DOI", "DOI"),
            ("Journal", "publicationTitle"), ("Vol", "volume"),
            ("Issue", "issue"), ("Pages", "pages"), ("ISBN", "ISBN"),
            ("Publisher", "publisher"), ("Place", "place"),
            ("URL", "url"), ("Language", "language"),
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
            truncated = len(abstract) > 10000
            lines.append(f"Abstract: {abstract[:10000]}")
            if truncated:
                lines.append("[Abstract truncated at 10,000 characters]")

        collections = data.get("collections", [])
        if collections:
            lines.append(f"Collections: {', '.join(collections)}")

        try:
            children = await _all_children(zot, item_key)
            attachment_count = sum(
                child.get("data", {}).get("itemType") == "attachment" for child in children
            )
            note_count = sum(child.get("data", {}).get("itemType") == "note" for child in children)
            lines.append(f"Children: {attachment_count} attachment(s), {note_count} note(s)")
        except Exception:
            pass

        return "\n".join(lines)

    @mcp.tool(annotations=READ_ONLY)
    async def get_bibtex(
        item_keys: list[str] | None = None,
        collection_id: str | None = None,
        include_abstract: bool = False,
        biblatex: bool = False,
        max_chars: int | None = 100000,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Export BibTeX entries from your Zotero library.

        Can export specific items, an entire collection, or your whole library.
        Use save_bibtex when the export should be written to a local file.

        Args:
            item_keys: Optional list of item keys to export. If omitted, exports collection or full library.
            collection_id: Optional collection key to export all items from.
            include_abstract: Include abstracts in BibTeX output (default False to save tokens).
            biblatex: Convert output to BibLaTeX format (default False). Remaps fields like journal→journaltitle, address→location, and merges year+month into date.
            max_chars: Maximum response size; use save_bibtex for larger full-library exports
        """
        try:
            if item_keys is not None and not item_keys:
                raise ValueError("item_keys must not be an empty list")
            if item_keys and collection_id:
                raise ValueError("Provide item_keys or collection_id, not both")
            if item_keys and len(item_keys) > 50:
                raise ValueError("A maximum of 50 item keys can be exported at once")
            if max_chars is not None and not 1000 <= max_chars <= 1000000:
                raise ValueError("max_chars must be between 1,000 and 1,000,000, or null")
            zot = _get_zot(library_id, library_type)
        except Exception as exc:
            return tool_error(f"Invalid BibTeX export request: {exc}")

        # BibTeX → BibLaTeX field remapping
        _BIBLATEX_FIELD_MAP = {
            "journal": "journaltitle",
            "address": "location",
            "school": "institution",
        }
        _MONTH_MAP = {
            "january": "01", "february": "02", "march": "03", "april": "04",
            "may": "05", "june": "06", "july": "07", "august": "08",
            "september": "09", "october": "10", "november": "11", "december": "12",
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "jun": "06", "jul": "07", "aug": "08", "sep": "09",
            "oct": "10", "nov": "11", "dec": "12",
        }

        def _clean_braces(value: str) -> str:
            """Strip Zotero's over-eager case-protection braces from titles.

            Keeps braces around acronyms (all-caps like {EMG}, {IEEE}),
            LaTeX commands (e.g. {\\textless}), and mixed-case names
            (e.g. {OpenMonkeyStudio}, {DeepLabCut}). Removes braces from
            regular title-cased words (e.g. {Review} → Review).
            """
            def _should_keep(m: re.Match) -> str:
                inner = m.group(1)
                # Keep: all-caps (acronyms), contains backslash (LaTeX),
                # contains digits, or has internal caps (camelCase/PascalCase names)
                if (inner.isupper() or
                    "\\" in inner or
                    any(c.isdigit() for c in inner) or
                    (len(inner) > 1 and any(c.isupper() for c in inner[1:]))):
                    return m.group(0)
                return inner
            return re.sub(r"\{([^{}]+)\}", _should_keep, value)

        def _to_biblatex(entry: dict) -> dict:
            """Convert a single BibTeX entry dict to BibLaTeX conventions."""
            for old_field, new_field in _BIBLATEX_FIELD_MAP.items():
                if old_field in entry and new_field not in entry:
                    entry[new_field] = entry.pop(old_field)
            # Merge year + month into date field
            if "year" in entry and "date" not in entry:
                year = entry.pop("year")
                month = entry.pop("month", None)
                if month:
                    mm = _MONTH_MAP.get(month.strip().lower(), month)
                    entry["date"] = f"{year}-{mm}"
                else:
                    entry["date"] = year
            elif "month" in entry and "date" in entry:
                entry.pop("month", None)
            # Clean over-braced titles
            for field in ("title", "shorttitle", "booktitle"):
                if field in entry:
                    entry[field] = _clean_braces(entry[field])
            return entry

        def _dumps(database) -> str:
            writer = BibTexWriter()
            writer.order_entries_by = None  # keep Zotero's order, not citekey order
            return writer.write(database)

        seen_ids: set[str] = set()

        def _unique_ids(result: object) -> None:
            # Each 100-item page is a separate Zotero export, which only keeps
            # citekeys unique within that page.
            for entry in getattr(result, "entries", None) or []:
                base = entry.get("ID", "")
                candidate, suffix = base, 1
                while candidate in seen_ids:
                    suffix += 1
                    candidate = f"{base}-{suffix}"
                entry["ID"] = candidate
                seen_ids.add(candidate)

        def _bib_to_str(result: object) -> str:
            if isinstance(result, bibtexparser.bibdatabase.BibDatabase):
                if not include_abstract:
                    for entry in result.entries:
                        entry.pop("abstract", None)
                if biblatex:
                    for entry in result.entries:
                        _to_biblatex(entry)
                return _dumps(result)
            text = str(result)
            try:
                parsed = bibtexparser.loads(text)
                if parsed.entries:
                    if not include_abstract:
                        for entry in parsed.entries:
                            entry.pop("abstract", None)
                    if biblatex:
                        for entry in parsed.entries:
                            _to_biblatex(entry)
                    return _dumps(parsed)
            except Exception:
                pass
            # Last-resort fallback for malformed exporters. Match multiline
            # values conservatively; valid BibTeX takes the parser path above.
            if not include_abstract:
                text = re.sub(
                    r"(?ims)^\s*abstract\s*=\s*(?:\{.*?\}|\".*?\")\s*,?\s*$",
                    "",
                    text,
                )
            if biblatex:
                for old_f, new_f in _BIBLATEX_FIELD_MAP.items():
                    text = re.sub(rf"(?im)^(\s*){old_f}(\s*=)", rf"\1{new_f}\2", text)
            return text

        try:
            if item_keys:
                result = await _zot_call(
                    zot.items, itemKey=",".join(item_keys), format="bibtex"
                )
                bib = _bib_to_str(result) if result else ""
                if not bib.strip():
                    return "No BibTeX data available for the specified items."
            else:
                # Top-level items only: child notes/attachments never export.
                if collection_id:
                    fetch = partial(zot.collection_items_top, collection_id)
                else:
                    fetch = zot.top
                chunks: list[str] = []
                size = 0
                start = 0
                while True:
                    result = await _zot_call(fetch, format="bibtex", limit=100, start=start)
                    _unique_ids(result)
                    text = _bib_to_str(result).strip() if result else ""
                    if text:
                        chunks.append(text)
                        size += len(text)
                    # Stop downloading as soon as the export cannot fit.
                    if max_chars is not None and size > max_chars:
                        return tool_error(
                            f"BibTeX export exceeds max_chars={max_chars:,}. "
                            "Narrow the export, increase max_chars, or use save_bibtex."
                        )
                    start += 100
                    total = _total_results(zot, -1)
                    if total >= 0 and start >= total:
                        break
                    if total < 0 and len(getattr(result, "entries", None) or []) < 100:
                        break
                bib = "\n\n".join(chunks)
        except Exception as e:
            return tool_error(f"Could not export BibTeX: {e}")

        if not bib.strip():
            return "No BibTeX data available."
        if max_chars is not None and len(bib) > max_chars:
            return tool_error(
                f"BibTeX export is {len(bib):,} characters, above max_chars={max_chars:,}. "
                "Narrow the export, increase max_chars, or use save_bibtex."
            )

        return bib

    @mcp.tool(annotations=DESTRUCTIVE)
    async def save_bibtex(
        save_path: str,
        item_keys: list[str] | None = None,
        collection_id: str | None = None,
        include_abstract: bool = False,
        biblatex: bool = False,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Export BibTeX or BibLaTeX and atomically write it to a local file.

        Path writes are available by default over local stdio. HTTP deployments
        must explicitly allow a confined file root.
        """

        exported = await get_bibtex(
            item_keys=item_keys,
            collection_id=collection_id,
            include_abstract=include_abstract,
            biblatex=biblatex,
            max_chars=None,
            library_id=library_id,
            library_type=library_type,
        )
        if isinstance(exported, CallToolResult):
            return exported
        if exported.startswith("No BibTeX data"):
            return exported

        try:
            destination = validate_server_path(save_path, for_write=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(destination, lambda output: output.write(exported.encode("utf-8")))
            parsed = bibtexparser.loads(exported)
            count = len(parsed.entries)
            fmt = "BibLaTeX" if biblatex else "BibTeX"
            return f"Saved {count} {fmt} entries to {destination}"
        except Exception as exc:
            return tool_error(f"Failed to save BibTeX: {exc}")

    @mcp.tool(annotations=READ_ONLY_OPEN_WORLD)
    async def get_item_fulltext(
        item_key: str,
        attachment_key: str | None = None,
        max_chars: int = 50000,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Get bounded plain text from a paper's PDF or Zotero full-text index.

        Unlike download_pdf, this returns readable text directly and never
        exposes a server-local temporary path.

        Args:
            item_key: The Zotero item key (the parent item, not the attachment)
            attachment_key: Optional PDF attachment key when an item has several PDFs
            max_chars: Maximum number of characters to return (1,000-200,000)
        """
        try:
            if not 1000 <= max_chars <= 200000:
                raise ValueError("max_chars must be between 1,000 and 200,000")
            zot = _get_zot(library_id, library_type)
            item = await _zot_call(zot.item, item_key)
            if item.get("data", {}).get("itemType") == "attachment":
                children = [item]  # a standalone attachment has no parent
            else:
                children = await _all_children(zot, item_key)
        except Exception as e:
            return tool_error(f"Could not find item {item_key}: {e}")

        # Prefer the PDF's index over e.g. a web snapshot's.
        children = sorted(
            children,
            key=lambda child: child.get("data", {}).get("contentType") != "application/pdf",
        )
        attachment_keys = []
        for child in children:
            child_data = child.get("data", {})
            child_key = child_data.get("key", "")
            if child_data.get("itemType") == "attachment" and child_key:
                if attachment_key and child_key != attachment_key:
                    continue
                attachment_keys.append(child_key)
                try:
                    ft = await _zot_call(zot.fulltext_item, child_key)
                    content = ft.get("content", "")
                    if content:
                        truncated = len(content) > max_chars
                        result = content[:max_chars]
                        if truncated:
                            result += "\n\n[Truncated; request another bounded view if needed.]"
                        return result
                except Exception:
                    continue

        if attachment_key and attachment_key not in attachment_keys:
            return tool_error(
                f"Attachment {attachment_key} is not a child attachment of item {item_key}"
            )

        tmp_path = None
        try:
            tmp_path, _ = await _download_pdf(zot, item_key, attachment_key)
            content, truncated = await asyncio.to_thread(_extract_pdf_text, tmp_path, max_chars)
            if not content.strip():
                return "No extractable full-text content is available for this PDF."
            if truncated:
                content += "\n\n[Truncated; increase max_chars for a larger bounded result.]"
            return content
        except Exception as exc:
            return tool_error(f"Could not retrieve full text: {exc}")
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_item(
        item_key: str,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Permanently delete an item from your Zotero library.

        Args:
            item_key: The Zotero item key to delete
        """
        try:
            zot = _get_zot(library_id, library_type)
            item = await _zot_call(zot.item, item_key)
        except Exception as e:
            return tool_error(f"Could not find item {item_key}: {e}")

        title = item.get("data", {}).get("title", item_key)

        try:
            await _zot_call(zot.delete_item, item)
        except Exception as e:
            return tool_error(f"Failed to delete item: {e}")

        return f"Deleted [{item_key}] {title}"

    @mcp.tool(annotations=READ_ONLY)
    async def get_recent_items(
        limit: int = 10,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Get recently added items from your Zotero library.

        Args:
            limit: Maximum number of items to return (default 10)
        """
        try:
            limit = _validate_limit(limit, maximum=100)
            zot = _get_zot(library_id, library_type)
            results = await _recent_top_level(zot, limit)
        except Exception as e:
            return tool_error(f"Could not fetch recent items: {e}")

        if not results:
            return "No items."

        lines = [_fmt_item(item.get("data", {})) for item in results]

        return "\n".join(lines) if lines else "No items."

    @mcp.tool(annotations=READ_ONLY_OPEN_WORLD)
    async def verify_items(
        limit: int = 10,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Verify that recently added items have valid DOIs that match CrossRef metadata.

        Re-resolves each item's DOI via CrossRef and compares the title. Reports
        items that have no DOI, DOIs that don't resolve, or title mismatches.

        Args:
            limit: Number of recent items to check (default 10)
        """
        try:
            limit = _validate_limit(limit, maximum=50)
            zot = _get_zot(library_id, library_type)
            items = await _recent_top_level(zot, limit)
        except Exception as e:
            return tool_error(f"Could not fetch items: {e}")

        if not items:
            return "No items to verify."

        async def _verify_one(item: dict) -> tuple[str, str]:
            data = item.get("data", {})
            key = data.get("key", "?")
            title = data.get("title", "Untitled")
            raw_doi = data.get("DOI", "")

            if not raw_doi:
                return "skip", f"[{key}] SKIP — no DOI: {title}"
            try:
                doi = _normalize_doi(raw_doi)  # also accepts https://doi.org/... values
            except ValueError:
                return "fail", f"[{key}] FAIL — not a valid DOI: {raw_doi}"

            try:
                msg = (await _resolve_doi(doi)).get("message", {})
                main = (msg.get("title") or [""])[0]
                subtitle = (msg.get("subtitle") or [""])[0]
                candidates = [main, f"{main}: {subtitle}" if subtitle else main]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    return "fail", f"[{key}] FAIL — CrossRef error {exc.response.status_code}: {doi}"
                # Not a CrossRef DOI (e.g. DataCite: arXiv, Zenodo); ask doi.org.
                try:
                    candidates = [str((await _resolve_doi_csl(doi)).get("title", ""))]
                except Exception:
                    return "fail", f"[{key}] FAIL — DOI does not resolve: {doi}"
            except Exception:
                return "fail", f"[{key}] FAIL — DOI does not resolve: {doi}"

            def _norm(text: str) -> str:
                text = re.sub(r"<[^>]+>", "", text)  # CrossRef titles carry <i>, <sup>, ...
                return re.sub(r"[^\w]+", " ", text.casefold()).strip()

            zot_norm = _norm(title)
            similarity, cr_title = max(
                (difflib.SequenceMatcher(None, zot_norm, _norm(c)).ratio(), c) for c in candidates
            )

            if similarity >= 0.92:
                return "ok", f"[{key}] OK — {title}"
            return (
                "mismatch",
                f"[{key}] MISMATCH ({similarity:.0%}) — Zotero: {title}\n"
                f"        CrossRef: {cr_title}",
            )

        semaphore = asyncio.Semaphore(8)

        async def _bounded(item: dict) -> tuple[str, str]:
            async with semaphore:
                return await _verify_one(item)

        checked = await asyncio.gather(*(_bounded(item) for item in items))
        counts: defaultdict[str, int] = defaultdict(int)
        lines = []
        for status, line in checked:
            counts[status] += 1
            lines.append(line)

        header = (
            f"Verified {len(items)} items: {counts['ok']} OK, "
            f"{counts['mismatch']} mismatch, {counts['fail']} failed, "
            f"{counts['skip']} skipped"
        )
        return header + "\n" + "\n".join(lines)

    @mcp.tool(annotations=READ_ONLY)
    async def health_check(
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> dict[str, object]:
        """Check Zotero credentials, library access, and optional file storage setup.
        """

        try:
            zot = _get_zot(library_id, library_type)
            sample = await _zot_call(zot.items, limit=1)
        except Exception as exc:
            return tool_error(f"Zotero health check failed: {exc}")

        try:
            key_info = await _zot_call(zot.key_info)
            write_access = _key_write_access(
                key_info,
                str(zot.library_type),
                str(zot.library_id),
            )
            write_access_note = (
                "Derived non-destructively from the Zotero API key permissions."
                if write_access is not None
                else "The Zotero API did not report write permission for this library."
            )
        except Exception:
            write_access = None
            write_access_note = "Could not read API key permissions; no write was attempted."

        return {
            "ok": True,
            "library_id": str(zot.library_id),
            "library_type": str(zot.library_type).rstrip("s"),
            "read_access": True,
            "write_access": write_access if write_access is not None else "unknown",
            "write_access_note": write_access_note,
            "webdav_configured": _use_webdav(zot),
            "open_access_pdf_lookup": bool(_unpaywall_email()),
            "sample_item_available": bool(sample),
        }

    @mcp.tool(annotations=READ_ONLY)
    async def list_attachments(
        item_key: str,
        limit: int = 100,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> dict[str, object]:
        """List attachment keys, filenames, MIME types, links, and sizes for an item.
        """

        try:
            limit = _validate_limit(limit, maximum=500)
            zot = _get_zot(library_id, library_type)
            await _zot_call(zot.item, item_key)
            children = await _all_children(zot, item_key)
        except Exception as exc:
            return tool_error(f"Could not list attachments for {item_key}: {exc}")

        attachments = []
        for child in children:
            data = child.get("data", {})
            if data.get("itemType") != "attachment":
                continue
            attachments.append(
                {
                    "key": data.get("key", ""),
                    "title": data.get("title", ""),
                    "filename": data.get("filename", ""),
                    "mime_type": data.get("contentType", ""),
                    "link_mode": data.get("linkMode", ""),
                    "url": data.get("url", ""),
                    "md5": data.get("md5", ""),
                    "mtime": data.get("mtime"),
                }
            )
            if len(attachments) >= limit:
                break
        return {
            "item_key": item_key,
            "count": len(attachments),
            "limit": limit,
            "attachments": attachments,
        }

    @mcp.tool(annotations=WRITE)
    async def update_item_metadata(
        item_key: str,
        updates: dict[str, str],
        creators: list[dict[str, str]] | None = None,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Update selected bibliographic fields and optionally replace creators.

        Immutable/internal fields such as item type, key, version, parent item,
        collections, tags, and deletion state cannot be changed through this tool.
        """

        if not updates and creators is None:
            return tool_error("Provide at least one metadata update or a creators list")
        if creators is not None:
            for creator in creators:
                if not creator.get("creatorType"):
                    return tool_error("Every creator must include creatorType")
                if not creator.get("name") and not creator.get("lastName"):
                    return tool_error("Every creator must include name or lastName")

        try:
            zot = _get_zot(library_id, library_type)
            item = await _zot_call(zot.item, item_key)
            data = item.get("data", {})
            item_type = data.get("itemType", "")
            template = await _zot_call(zot.item_template, item_type)
            protected = {
                "key",
                "version",
                "itemType",
                "parentItem",
                "deleted",
                "collections",
                "tags",
                "relations",
            }
            unknown = sorted(
                field for field in updates if field not in template or field in protected
            )
            if unknown:
                return tool_error(
                    f"Unsupported field(s) for Zotero {item_type}: {', '.join(unknown)}"
                )
            changes: dict[str, object] = dict(updates)
            if creators is not None:
                changes["creators"] = creators
            version = int(data.get("version", item.get("version")))
            key = str(data.get("key", item_key))
            await _zot_call(_patch_item, zot, key, version, changes)
            title = str(updates.get("title", data.get("title", item_key)))
            return f"Updated [{item_key}] {title}"
        except Exception as exc:
            return tool_error(f"Failed to update item metadata: {exc}")

    @mcp.tool(annotations=WRITE)
    async def trash_item(
        item_key: str,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Move an item to Zotero's trash so it can be restored later.
        """

        try:
            zot = _get_zot(library_id, library_type)
            item = await _zot_call(zot.item, item_key)
            data = item.get("data", {})
            title = data.get("title", item_key)
            version = int(data.get("version", item.get("version")))
            key = str(data.get("key", item_key))
            await _zot_call(_patch_item, zot, key, version, {"deleted": True})
            return f"Moved [{item_key}] {title} to trash"
        except Exception as exc:
            return tool_error(f"Failed to trash item: {exc}")

    @mcp.tool(annotations=READ_ONLY)
    async def list_trash(
        limit: int = 25,
        start: int = 0,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """List items in Zotero's trash, e.g. to find a key for restore_item.

        Args:
            limit: Maximum number of items per page (default 25, max 100)
            start: Offset of the first item; use the footer's suggestion for the next page
        """
        try:
            limit = _validate_limit(limit)
            start = _validate_start(start)
            zot = _get_zot(library_id, library_type)
            page = await _zot_call(zot.trash, limit=limit, start=start)
        except Exception as exc:
            return tool_error(f"Could not list the trash: {exc}")

        total = _total_results(zot, start + len(page))
        if not page:
            return "Trash is empty." if start == 0 else "No more items." + _page_footer(start, 0, total)
        lines = [_fmt_item(item.get("data", {})) for item in page]
        return "\n".join(lines) + _page_footer(start, len(page), total)

    @mcp.tool(annotations=WRITE)
    async def restore_item(
        item_key: str,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Restore an item from Zotero's trash.
        """

        try:
            zot = _get_zot(library_id, library_type)
            item = await _zot_call(zot.item, item_key)
            data = item.get("data", {})
            title = data.get("title", item_key)
            version = int(data.get("version", item.get("version")))
            key = str(data.get("key", item_key))
            await _zot_call(_patch_item, zot, key, version, {"deleted": False})
            return f"Restored [{item_key}] {title} from trash"
        except Exception as exc:
            return tool_error(f"Failed to restore item: {exc}")

    @mcp.tool(annotations=READ_ONLY)
    async def find_duplicates(
        field: str = "DOI",
        limit: int = 50,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> dict[str, object]:
        """Find duplicate top-level items by DOI, ISBN, or normalized title.
        """

        field = field.upper() if field.upper() in {"DOI", "ISBN"} else field.lower()
        if field not in {"DOI", "ISBN", "title"}:
            return tool_error("field must be DOI, ISBN, or title")
        try:
            limit = _validate_limit(limit, maximum=200)
            zot = _get_zot(library_id, library_type)
            items = await _cached_top_items(
                zot, lambda: _get_zot(library_id, library_type)
            )
        except Exception as exc:
            return tool_error(f"Could not scan for duplicates: {exc}")

        groups: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
        for item in _filter_top_level(items):
            data = item.get("data", {})
            value = str(data.get(field, ""))
            if field == "title":
                normalized = re.sub(r"[^\w]+", " ", value.casefold()).strip()
            elif field == "DOI":
                # Hyphens are significant in DOIs; URL prefixes are not.
                normalized = re.sub(
                    r"^(?:https?://(?:dx\.|www\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I
                ).casefold()
            else:
                normalized = _first_isbn13(value)
            if normalized:
                groups[normalized].append(
                    {"key": data.get("key", ""), "title": data.get("title", "")}
                )

        duplicates = [
            {"value": normalized, "items": grouped}
            for normalized, grouped in groups.items()
            if len(grouped) > 1
        ][:limit]
        return {
            "field": field,
            "count": len(duplicates),
            "duplicates": duplicates,
            "items_scanned": len(items),
            "scan_capped": len(items) >= MAX_CACHED_ITEMS,
        }

    @mcp.tool(annotations=READ_ONLY)
    async def search_fulltext(
        query: str,
        limit: int = 10,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> dict[str, object]:
        """Search Zotero metadata and indexed full text using qmode=everything.
        """

        if not query.strip():
            return tool_error("query must not be empty")
        try:
            limit = _validate_limit(limit, maximum=50)
            zot = _get_zot(library_id, library_type)
            # /items/top matches parents through their attachments' full text.
            # /items would return the matching attachments themselves (about
            # 10x slower here, and mostly discarded by the filter below).
            results = await _zot_call(
                zot.top,
                q=query.strip(),
                qmode="everything",
                limit=limit,
            )
        except Exception as exc:
            return tool_error(f"Full-text search failed: {exc}")

        items = []
        for item in _filter_top_level(results)[:limit]:
            data = item.get("data", {})
            items.append(
                {
                    "key": data.get("key", ""),
                    "title": data.get("title", ""),
                    "date": data.get("date", ""),
                    "doi": data.get("DOI", ""),
                    "url": data.get("url", ""),
                }
            )
        return {"query": query, "count": len(items), "items": items}

    @mcp.tool(annotations=READ_ONLY)
    async def search(
        query: str,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> dict[str, object]:
        """Company-knowledge compatible search over Zotero items and full text.
        """

        result = await search_fulltext(
            query=query, limit=10, library_id=library_id, library_type=library_type
        )
        if isinstance(result, CallToolResult):
            return result
        try:
            web_base = await _web_library_url(_get_zot(library_id, library_type))
        except Exception:
            web_base = ""
        return {
            "results": [
                {
                    "id": item["key"],
                    "title": item["title"],
                    "url": item.get("url")
                    or (f"{web_base}/items/{item['key']}" if web_base else ""),
                }
                for item in result["items"]
            ]
        }

    @mcp.tool(annotations=READ_ONLY)
    async def fetch(
        id: str,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> dict[str, object]:
        """Company-knowledge compatible fetch for one Zotero item key.
        """

        try:
            zot = _get_zot(library_id, library_type)
            item = await _zot_call(zot.item, id)
        except Exception as exc:
            return tool_error(f"Could not fetch item {id}: {exc}")
        data = item.get("data", {})
        fulltext = await get_item_fulltext(
            id, max_chars=30000, library_id=library_id, library_type=library_type
        )
        if isinstance(fulltext, CallToolResult) or fulltext.startswith(
            "No extractable full-text"
        ):
            text = data.get("abstractNote", "")
        else:
            text = fulltext
        return {
            "id": id,
            "title": data.get("title", ""),
            "text": text,
            "url": data.get("url", ""),
            "metadata": {
                "doi": data.get("DOI", ""),
                "date": data.get("date", ""),
                "item_type": data.get("itemType", ""),
            },
        }
