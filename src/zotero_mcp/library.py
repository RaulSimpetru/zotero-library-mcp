"""Tools for browsing, searching, and managing library items."""

import asyncio
import difflib
import os
import re
from collections import defaultdict
from pathlib import Path

import bibtexparser
import fitz
from fuzzysearch import find_near_matches
from mcp.types import CallToolResult

from ._helpers import (
    _download_pdf,
    _fmt_item,
    _get_zot,
    _page_footer,
    _resolve_doi,
    _total_results,
    _use_webdav,
    _validate_limit,
    _validate_start,
    _zot_call,
)
from .responses import tool_error
from .runtime import validate_server_path
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
        if not isinstance(user_access, dict) or "write" not in user_access:
            return None
        return bool(user_access["write"])

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
    results: list[dict] = []
    start = 0
    page_size = min(100, max(limit * 2, 20))
    while len(results) < limit and start < 1000:
        page = await _zot_call(
            zot.items,
            sort="dateAdded",
            direction="desc",
            limit=page_size,
            start=start,
        )
        if not page:
            break
        results.extend(_filter_top_level(page))
        if len(page) < page_size:
            break
        start += page_size
    return results[:limit]


async def _top_items_bounded(zot, maximum: int) -> list[dict]:
    items: list[dict] = []
    start = 0
    while len(items) < maximum:
        page_size = min(100, maximum - len(items))
        page = await _zot_call(zot.top, limit=page_size, start=start)
        if not page:
            break
        items.extend(page)
        if len(page) < page_size:
            break
        start += page_size
    return items


def _patch_item_partial(
    zot,
    item_key: str,
    version: int,
    changes: dict[str, object],
):
    """Apply an optimistic-concurrency-protected partial item update."""

    url = (
        f"{zot.endpoint.rstrip('/')}/{zot.library_type}/"
        f"{zot.library_id}/items/{item_key.upper()}"
    )
    response = zot.client.patch(
        url,
        headers={"If-Unmodified-Since-Version": str(version)},
        json=changes,
    )
    response.raise_for_status()
    return response


def register(mcp):
    @mcp.tool(annotations=READ_ONLY)
    async def get_unfiled_items(limit: int = 25) -> str:
        """Get items that are not in any collection (unfiled items).

        Args:
            limit: Maximum number of items to return (default 25)
        """
        try:
            limit = _validate_limit(limit)
            zot = _get_zot()
        except Exception as e:
            return tool_error(str(e))

        unfiled = []
        start = 0
        page_size = 100
        # Stay bounded even for very large libraries while avoiding a full
        # ``everything(top())`` download for the common case.
        while len(unfiled) < limit and start < 5000:
            try:
                page = await _zot_call(zot.top, limit=page_size, start=start)
            except Exception as e:
                return tool_error(f"Could not fetch items: {e}")
            if not page:
                break
            for item in page:
                data = item.get("data", {})
                if data.get("itemType") in ("attachment", "note", "annotation"):
                    continue
                if not data.get("collections"):
                    unfiled.append(data)
                    if len(unfiled) >= limit:
                        break
            if len(page) < page_size:
                break
            start += page_size

        if not unfiled:
            return "No unfiled items."

        lines = [_fmt_item(item) for item in unfiled]
        suffix = " (scan capped at 5,000 items)" if start >= 5000 else ""
        return f"Unfiled items (showing {len(lines)}){suffix}:\n" + "\n".join(lines)

    def _rank_fuzzy_items(all_items: list[dict], query: str) -> list[dict]:
        """Fuzzy-match query against a bounded set of item titles, authors, and tags."""
        query_norm = re.sub(r"\s+", " ", query.strip().lower())
        if not query_norm:
            return []

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
            tag_str = " ".join(tag.get("tag", "") for tag in data.get("tags", []))
            searchable = f"{title} {author_str} {tag_str}".lower()

            matches = find_near_matches(query_norm, searchable, max_l_dist=max_dist)
            if matches:
                best_dist = min(m.dist for m in matches)
                scored.append((best_dist, data))

        scored.sort(key=lambda x: x[0])
        return [data for _, data in scored]

    @mcp.tool(annotations=READ_ONLY)
    async def search_library(query: str, limit: int = 10, start: int = 0) -> str:
        """Search your Zotero library. Falls back to fuzzy matching if the
        exact search returns no results.

        Args:
            query: Search query (searches titles, authors, tags, etc.)
            limit: Maximum number of results per page (default 10, max 100)
            start: Offset of the first result; pass the value suggested by the
                previous call's footer to fetch the next page (default 0)
        """
        try:
            limit = _validate_limit(limit, maximum=100)
            start = _validate_start(start)
            if not query.strip():
                return tool_error("query must not be empty")
            zot = _get_zot()
            results = await _zot_call(zot.items, q=query.strip(), limit=limit, start=start)
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
            top_query = await _top_items_bounded(zot, 1000)
            ranked = _rank_fuzzy_items(top_query, query)
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
    async def get_item_details(item_key: str) -> str:
        """Get full details of a Zotero item by its key.

        Args:
            item_key: The Zotero item key
        """
        zot = _get_zot()

        try:
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
            children = await _zot_call(zot.children, item_key)
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
            zot = _get_zot()
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

        def _bib_to_str(result: object) -> str:
            if isinstance(result, bibtexparser.bibdatabase.BibDatabase):
                if not include_abstract:
                    for entry in result.entries:
                        entry.pop("abstract", None)
                if biblatex:
                    for entry in result.entries:
                        _to_biblatex(entry)
                return bibtexparser.dumps(result)
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
                    return bibtexparser.dumps(parsed)
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
                parts = []
                for key in item_keys:
                    result = await _zot_call(zot.item, key, format="bibtex")
                    if result:
                        parts.append(_bib_to_str(result).strip())
                if not parts:
                    return "No BibTeX data available for the specified items."
                bib = "\n\n".join(parts)
            elif collection_id:
                result = await _zot_call(
                    lambda: zot.everything(
                        zot.collection_items(collection_id, format="bibtex")
                    )
                )
                bib = _bib_to_str(result)
            else:
                result = await _zot_call(lambda: zot.everything(zot.items(format="bibtex")))
                bib = _bib_to_str(result)
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
        )
        if isinstance(exported, CallToolResult):
            return exported
        if exported.startswith("No BibTeX data"):
            return exported

        temp_path: Path | None = None
        try:
            destination = validate_server_path(save_path, for_write=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp_path = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
            temp_path.write_text(exported, encoding="utf-8")
            os.replace(temp_path, destination)
            parsed = bibtexparser.loads(exported)
            count = len(parsed.entries)
            fmt = "BibLaTeX" if biblatex else "BibTeX"
            return f"Saved {count} {fmt} entries to {destination}"
        except Exception as exc:
            return tool_error(f"Failed to save BibTeX: {exc}")
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)

    @mcp.tool(annotations=READ_ONLY_OPEN_WORLD)
    async def get_item_fulltext(
        item_key: str,
        attachment_key: str | None = None,
        max_chars: int = 50000,
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
            zot = _get_zot()
            children = await _zot_call(zot.children, item_key)
        except Exception as e:
            return tool_error(f"Could not find item {item_key}: {e}")

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
    async def delete_item(item_key: str) -> str:
        """Permanently delete an item from your Zotero library.

        Args:
            item_key: The Zotero item key to delete
        """
        zot = _get_zot()

        try:
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
    async def get_recent_items(limit: int = 10) -> str:
        """Get recently added items from your Zotero library.

        Args:
            limit: Maximum number of items to return (default 10)
        """
        try:
            limit = _validate_limit(limit, maximum=100)
            zot = _get_zot()
            results = await _recent_top_level(zot, limit)
        except Exception as e:
            return tool_error(f"Could not fetch recent items: {e}")

        if not results:
            return "No items."

        lines = [_fmt_item(item.get("data", {})) for item in results]

        return "\n".join(lines) if lines else "No items."

    @mcp.tool(annotations=READ_ONLY_OPEN_WORLD)
    async def verify_items(limit: int = 10) -> str:
        """Verify that recently added items have valid DOIs that match CrossRef metadata.

        Re-resolves each item's DOI via CrossRef and compares the title. Reports
        items that have no DOI, DOIs that don't resolve, or title mismatches.

        Args:
            limit: Number of recent items to check (default 10)
        """
        try:
            limit = _validate_limit(limit, maximum=50)
            zot = _get_zot()
            items = await _recent_top_level(zot, limit)
        except Exception as e:
            return tool_error(f"Could not fetch items: {e}")

        if not items:
            return "No items to verify."

        async def _verify_one(item: dict) -> tuple[str, str]:
            data = item.get("data", {})
            key = data.get("key", "?")
            title = data.get("title", "Untitled")
            doi = data.get("DOI", "")

            if not doi:
                return "skip", f"[{key}] SKIP — no DOI: {title}"

            try:
                cr_data = await _resolve_doi(doi)
            except Exception:
                return "fail", f"[{key}] FAIL — DOI does not resolve: {doi}"

            msg = cr_data.get("message", cr_data)
            cr_titles = msg.get("title", [])
            cr_title = cr_titles[0] if cr_titles else ""

            zot_norm = re.sub(r"[^\w]+", " ", title.casefold()).strip()
            cr_norm = re.sub(r"[^\w]+", " ", cr_title.casefold()).strip()
            similarity = difflib.SequenceMatcher(None, zot_norm, cr_norm).ratio()

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
    async def health_check() -> dict[str, object]:
        """Check Zotero credentials, library access, and optional file storage setup."""

        try:
            zot = _get_zot()
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
            "library_type": zot.library_type,
            "read_access": True,
            "write_access": write_access if write_access is not None else "unknown",
            "write_access_note": write_access_note,
            "webdav_configured": _use_webdav(),
            "sample_item_available": bool(sample),
        }

    @mcp.tool(annotations=READ_ONLY)
    async def list_attachments(item_key: str, limit: int = 100) -> dict[str, object]:
        """List attachment keys, filenames, MIME types, links, and sizes for an item."""

        try:
            limit = _validate_limit(limit, maximum=500)
            zot = _get_zot()
            await _zot_call(zot.item, item_key)
            children = await _zot_call(zot.children, item_key)
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
            zot = _get_zot()
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
            await _zot_call(_patch_item_partial, zot, key, version, changes)
            title = str(updates.get("title", data.get("title", item_key)))
            return f"Updated [{item_key}] {title}"
        except Exception as exc:
            return tool_error(f"Failed to update item metadata: {exc}")

    @mcp.tool(annotations=WRITE)
    async def trash_item(item_key: str) -> str:
        """Move an item to Zotero's trash so it can be restored later."""

        try:
            zot = _get_zot()
            item = await _zot_call(zot.item, item_key)
            data = item.get("data", {})
            title = data.get("title", item_key)
            version = int(data.get("version", item.get("version")))
            key = str(data.get("key", item_key))
            await _zot_call(_patch_item_partial, zot, key, version, {"deleted": True})
            return f"Moved [{item_key}] {title} to trash"
        except Exception as exc:
            return tool_error(f"Failed to trash item: {exc}")

    @mcp.tool(annotations=WRITE)
    async def restore_item(item_key: str) -> str:
        """Restore an item from Zotero's trash."""

        try:
            zot = _get_zot()
            item = await _zot_call(zot.item, item_key)
            data = item.get("data", {})
            title = data.get("title", item_key)
            version = int(data.get("version", item.get("version")))
            key = str(data.get("key", item_key))
            await _zot_call(_patch_item_partial, zot, key, version, {"deleted": False})
            return f"Restored [{item_key}] {title} from trash"
        except Exception as exc:
            return tool_error(f"Failed to restore item: {exc}")

    @mcp.tool(annotations=READ_ONLY)
    async def find_duplicates(field: str = "DOI", limit: int = 50) -> dict[str, object]:
        """Find duplicate top-level items by DOI, ISBN, or normalized title."""

        field = field.upper() if field.upper() in {"DOI", "ISBN"} else field.lower()
        if field not in {"DOI", "ISBN", "title"}:
            return tool_error("field must be DOI, ISBN, or title")
        try:
            limit = _validate_limit(limit, maximum=200)
            zot = _get_zot()
            items = await _top_items_bounded(zot, 5000)
        except Exception as exc:
            return tool_error(f"Could not scan for duplicates: {exc}")

        groups: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
        for item in _filter_top_level(items):
            data = item.get("data", {})
            value = str(data.get(field, ""))
            if field == "title":
                normalized = re.sub(r"[^\w]+", " ", value.casefold()).strip()
            else:
                normalized = re.sub(r"[-\s]", "", value).casefold()
            if normalized:
                groups[normalized].append(
                    {"key": data.get("key", ""), "title": data.get("title", "")}
                )

        duplicates = [
            {"value": normalized, "items": grouped}
            for normalized, grouped in groups.items()
            if len(grouped) > 1
        ][:limit]
        return {"field": field, "count": len(duplicates), "duplicates": duplicates}

    @mcp.tool(annotations=READ_ONLY)
    async def search_fulltext(query: str, limit: int = 10) -> dict[str, object]:
        """Search Zotero metadata and indexed full text using qmode=everything."""

        if not query.strip():
            return tool_error("query must not be empty")
        try:
            limit = _validate_limit(limit, maximum=50)
            zot = _get_zot()
            results = await _zot_call(
                zot.items,
                q=query.strip(),
                qmode="everything",
                limit=min(100, limit * 3),
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
    async def search(query: str) -> dict[str, object]:
        """Company-knowledge compatible search over Zotero items and full text."""

        result = await search_fulltext(query=query, limit=10)
        if isinstance(result, CallToolResult):
            return result
        return {
            "results": [
                {
                    "id": item["key"],
                    "title": item["title"],
                    "url": item.get("url", ""),
                }
                for item in result["items"]
            ]
        }

    @mcp.tool(annotations=READ_ONLY)
    async def fetch(id: str) -> dict[str, object]:
        """Company-knowledge compatible fetch for one Zotero item key."""

        try:
            zot = _get_zot()
            item = await _zot_call(zot.item, id)
        except Exception as exc:
            return tool_error(f"Could not fetch item {id}: {exc}")
        data = item.get("data", {})
        fulltext = await get_item_fulltext(id, max_chars=30000)
        if isinstance(fulltext, CallToolResult):
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
