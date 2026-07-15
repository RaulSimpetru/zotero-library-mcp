"""Tools for adding papers and books to Zotero."""

import asyncio
import re

import httpx

from ._helpers import (
    ARXIV_NS,
    _arxiv_to_zotero,
    _attach_pdf_from_url,
    _crossref_to_zotero,
    _find_open_access_pdf,
    _get_zot,
    _openlibrary_to_zotero,
    _resolve_arxiv,
    _resolve_doi,
    _resolve_isbn,
    _zot_call,
)
from .responses import tool_error
from .tool_annotations import WRITE, WRITE_OPEN_WORLD


def _normalize_doi(doi: str) -> str:
    normalized = doi.strip()
    normalized = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", normalized, flags=re.I)
    normalized = re.sub(r"^doi:\s*", "", normalized, flags=re.I).strip()
    if not re.fullmatch(r"10\.\d{4,9}/\S+", normalized, flags=re.I):
        raise ValueError(f"Invalid DOI: {doi}")
    return normalized


def _normalize_isbn(isbn: str) -> str:
    """Normalize and checksum-validate an ISBN-10 or ISBN-13."""

    normalized = re.sub(r"[-\s]", "", isbn).upper()
    if re.fullmatch(r"\d{9}[\dX]", normalized):
        values = [10 if char == "X" else int(char) for char in normalized]
        if sum((10 - index) * value for index, value in enumerate(values)) % 11:
            raise ValueError(f"Invalid ISBN checksum: {isbn}")
        return normalized
    if re.fullmatch(r"\d{13}", normalized):
        if sum((1 if index % 2 == 0 else 3) * int(char) for index, char in enumerate(normalized)) % 10:
            raise ValueError(f"Invalid ISBN checksum: {isbn}")
        return normalized
    raise ValueError(f"Invalid ISBN: {isbn}")


async def _find_duplicate(zot, *, field: str, value: str) -> dict | None:
    """Use Zotero's server-side search, then confirm an exact field match."""

    candidates = await _zot_call(zot.items, q=value, qmode="everything", limit=100)
    normalized = re.sub(r"[-\s]", "", value).casefold()
    for candidate in candidates:
        data = candidate.get("data", {})
        candidate_value = str(data.get(field, ""))
        if re.sub(r"[-\s]", "", candidate_value).casefold() == normalized:
            return data
    return None


async def _validate_collection(zot, collection_id: str | None) -> None:
    if collection_id:
        await _zot_call(zot.collection, collection_id)


def register(mcp):
    @mcp.tool(annotations=WRITE_OPEN_WORLD)
    async def add_paper_by_doi(doi: str, collection_id: str | None = None) -> str:
        """Add a paper to your Zotero library by its DOI.

        Resolves metadata automatically via CrossRef and creates the item in Zotero.
        Optionally add it to a specific collection.

        Args:
            doi: The DOI of the paper (e.g. "10.1038/nature12373")
            collection_id: Optional Zotero collection key to add the paper to
        """
        try:
            doi = _normalize_doi(doi)
            zot = _get_zot()
            await _validate_collection(zot, collection_id)
            duplicate = await _find_duplicate(zot, field="DOI", value=doi)
            if duplicate:
                return (
                    f"Duplicate: [{duplicate.get('key', '')}] "
                    f"{duplicate.get('title', '')} already in library"
                )
        except Exception as exc:
            return tool_error(f"Could not validate DOI import: {exc}")

        try:
            cr_data = await _resolve_doi(doi)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return tool_error(f"DOI not found: {doi}. Please check the DOI is correct.")
            return tool_error(f"CrossRef API error: {e}")
        except Exception as e:
            return tool_error(f"Failed to resolve DOI: {e}")

        item = _crossref_to_zotero(cr_data)

        if collection_id:
            item["collections"] = [collection_id]

        try:
            result = await _zot_call(zot.create_items, [item])
        except Exception as e:
            return tool_error(f"Failed to create item in Zotero: {e}")

        if result.get("successful"):
            created = list(result["successful"].values())[0]
            key = created.get("key", "unknown")
            title = created.get("data", {}).get("title", item["title"])
            pdf_url = await _find_open_access_pdf(doi)
            if pdf_url:
                attached = await _attach_pdf_from_url(zot, key, pdf_url)
                if attached:
                    return f"Added [{key}] {title} (with PDF)"
            return f"Added [{key}] {title}"
        elif result.get("failed"):
            return tool_error(f"Rejected: {list(result['failed'].values())}")
        else:
            return tool_error(f"Unexpected response from Zotero: {result}")

    @mcp.tool(annotations=WRITE_OPEN_WORLD)
    async def add_papers_by_dois(
        dois: list[str],
        collection_id: str | None = None,
        attach_pdfs: bool = False,
    ) -> str:
        """Add multiple papers to Zotero by their DOIs (batch, up to 50).

        Args:
            dois: List of DOIs to add
            collection_id: Optional Zotero collection key to add all papers to
        """
        if not dois:
            return tool_error("At least one DOI is required.")
        if len(dois) > 50:
            return tool_error(
                "Zotero API supports a maximum of 50 items per batch. "
                "Please split into smaller batches."
            )

        normalized_dois: list[str] = []
        seen = set()
        invalid_dois = []
        for raw_doi in dois:
            try:
                doi = _normalize_doi(raw_doi)
            except ValueError as exc:
                invalid_dois.append(str(exc))
                continue
            folded = doi.casefold()
            if folded not in seen:
                seen.add(folded)
                normalized_dois.append(doi)

        try:
            zot = _get_zot()
            await _validate_collection(zot, collection_id)
        except Exception as exc:
            return tool_error(f"Could not validate batch import: {exc}")

        new_dois = []
        duplicate_lines = []
        for doi in normalized_dois:
            try:
                duplicate = await _find_duplicate(zot, field="DOI", value=doi)
            except Exception as exc:
                return tool_error(f"Duplicate check failed for {doi}: {exc}")
            if duplicate:
                duplicate_lines.append(
                    f"{doi}: [{duplicate.get('key', '?')}] {duplicate.get('title', '?')}"
                )
            else:
                new_dois.append(doi)

        items = []
        failed_dois = []

        semaphore = asyncio.Semaphore(8)

        async def _resolve_one(doi: str):
            try:
                async with semaphore:
                    cr_data = await _resolve_doi(doi)
                item = _crossref_to_zotero(cr_data)
                if collection_id:
                    item["collections"] = [collection_id]
                return doi, item, None
            except Exception as e:
                return doi, None, str(e)

        resolved = await asyncio.gather(*(_resolve_one(doi) for doi in new_dois))
        resolved_dois = []
        for doi, item, error in resolved:
            if item is not None:
                items.append(item)
                resolved_dois.append(doi)
            else:
                failed_dois.append(f"{doi}: {error}")

        if not items:
            lines = ["No new DOI items were added."]
            if duplicate_lines:
                lines.append("Duplicates:\n  " + "\n  ".join(duplicate_lines))
            if invalid_dois or failed_dois:
                lines.append("Errors:\n  " + "\n  ".join([*invalid_dois, *failed_dois]))
            return "\n".join(lines)

        try:
            result = await _zot_call(zot.create_items, items)
        except Exception as e:
            return tool_error(f"Failed to create items in Zotero: {e}")

        successful = result.get("successful", {})
        zot_failed = len(result.get("failed", {}))

        lines = [f"Added {len(successful)}/{len(normalized_dois)} unique valid DOI(s):"]
        for index, it in successful.items():
            lines.append(f"  [{it.get('key','?')}] {it.get('data',{}).get('title','?')}")
            if attach_pdfs:
                try:
                    doi = resolved_dois[int(index)]
                    pdf_url = await _find_open_access_pdf(doi)
                    if pdf_url and await _attach_pdf_from_url(zot, it.get("key", ""), pdf_url):
                        lines[-1] += " (with PDF)"
                except Exception:
                    lines[-1] += " (PDF attachment failed)"
        if zot_failed:
            lines.append(f"Rejected: {zot_failed}")
        if duplicate_lines:
            lines.append("Duplicates skipped:\n  " + "\n  ".join(duplicate_lines))
        if invalid_dois:
            lines.extend(f"  Invalid: {error}" for error in invalid_dois)
        if failed_dois:
            lines.extend(f"  Failed: {f}" for f in failed_dois)

        return "\n".join(lines)

    @mcp.tool(annotations=WRITE_OPEN_WORLD)
    async def add_paper_by_arxiv_id(arxiv_id: str, collection_id: str | None = None) -> str:
        """Add a paper to your Zotero library by its arXiv ID.

        Fetches metadata from the arXiv API. If the paper has a DOI, resolves it
        via CrossRef for richer metadata; otherwise creates a preprint entry directly.
        Optionally add it to a specific collection.

        Args:
            arxiv_id: The arXiv ID of the paper (e.g. "2301.07041")
            collection_id: Optional Zotero collection key to add the paper to
        """
        arxiv_id = arxiv_id.strip()
        arxiv_id = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", arxiv_id, flags=re.I)
        arxiv_id = re.sub(r"\.pdf$", "", arxiv_id, flags=re.I)
        arxiv_id = re.sub(r"^arxiv:\s*", "", arxiv_id, flags=re.I)
        if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?", arxiv_id, re.I):
            return tool_error(f"Invalid arXiv ID: {arxiv_id}")

        try:
            zot = _get_zot()
            await _validate_collection(zot, collection_id)
            arxiv_url = f"https://arxiv.org/abs/{arxiv_id}"
            existing_items = await _zot_call(
                zot.items, q=arxiv_id, qmode="everything", limit=100
            )
            for existing in existing_items:
                data = existing.get("data", {})
                if arxiv_url in data.get("url", "") or f"arXiv:{arxiv_id}" in data.get("extra", ""):
                    key = data.get("key", "")
                    title = data.get("title", "")
                    return f"Duplicate: [{key}] {title} already in library"
        except Exception as exc:
            return tool_error(f"Could not validate arXiv import: {exc}")

        try:
            entry = await _resolve_arxiv(arxiv_id)
        except ValueError as e:
            return tool_error(str(e))
        except Exception as e:
            return tool_error(f"Failed to fetch arXiv metadata: {e}")

        doi = entry.findtext("arxiv:doi", "", ARXIV_NS).strip() or None
        for link in entry.findall("atom:link", ARXIV_NS):
            if doi:
                break
            href = link.get("href", "")
            if "doi.org/" in href:
                doi = href.split("doi.org/", 1)[1]
                break

        if doi:
            try:
                cr_data = await _resolve_doi(doi)
                item = _crossref_to_zotero(cr_data)
                if not item.get("url"):
                    item["url"] = arxiv_url
                extra = item.get("extra", "")
                if f"arXiv:{arxiv_id}" not in extra:
                    item["extra"] = f"arXiv:{arxiv_id}\n{extra}".strip()
            except Exception:
                item = _arxiv_to_zotero(entry, arxiv_id)
        else:
            item = _arxiv_to_zotero(entry, arxiv_id)

        if collection_id:
            item["collections"] = [collection_id]

        try:
            result = await _zot_call(zot.create_items, [item])
        except Exception as e:
            return tool_error(f"Failed to create item in Zotero: {e}")

        if result.get("successful"):
            created = list(result["successful"].values())[0]
            key = created.get("key", "unknown")
            title = created.get("data", {}).get("title", item["title"])
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
            attached = await _attach_pdf_from_url(zot, key, pdf_url)
            if attached:
                return f"Added [{key}] {title} (with PDF)"
            return f"Added [{key}] {title}"
        elif result.get("failed"):
            return tool_error(f"Rejected: {list(result['failed'].values())}")
        else:
            return tool_error(f"Unexpected response from Zotero: {result}")

    @mcp.tool(annotations=WRITE_OPEN_WORLD)
    async def add_book_by_isbn(isbn: str, collection_id: str | None = None) -> str:
        """Add a book to your Zotero library by its ISBN.

        Resolves metadata automatically via Open Library and creates the item in Zotero.
        Optionally add it to a specific collection.

        Args:
            isbn: The ISBN of the book (e.g. "9780262046824")
            collection_id: Optional Zotero collection key to add the book to
        """
        try:
            isbn = _normalize_isbn(isbn)
        except ValueError as exc:
            return tool_error(str(exc))

        try:
            zot = _get_zot()
            await _validate_collection(zot, collection_id)
            duplicate = await _find_duplicate(zot, field="ISBN", value=isbn)
            if duplicate:
                return (
                    f"Duplicate: [{duplicate.get('key', '')}] "
                    f"{duplicate.get('title', '')} already in library"
                )
        except Exception as exc:
            return tool_error(f"Could not validate ISBN import: {exc}")

        try:
            ol_data = await _resolve_isbn(isbn)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return tool_error(f"ISBN not found: {isbn}. Please check the ISBN is correct.")
            return tool_error(f"Open Library API error: {e}")
        except Exception as e:
            return tool_error(f"Failed to resolve ISBN: {e}")

        item = _openlibrary_to_zotero(ol_data, isbn)

        if collection_id:
            item["collections"] = [collection_id]

        try:
            result = await _zot_call(zot.create_items, [item])
        except Exception as e:
            return tool_error(f"Failed to create item in Zotero: {e}")

        if result.get("successful"):
            created = list(result["successful"].values())[0]
            key = created.get("key", "unknown")
            title = created.get("data", {}).get("title", item["title"])
            return f"Added [{key}] {title}"
        elif result.get("failed"):
            return tool_error(f"Rejected: {list(result['failed'].values())}")
        else:
            return tool_error(f"Unexpected response from Zotero: {result}")

    @mcp.tool(annotations=WRITE)
    async def add_item_from_metadata(
        item_type: str,
        title: str,
        fields: dict[str, object] | None = None,
        creators: list[dict[str, str]] | None = None,
        collection_id: str | None = None,
    ) -> str:
        """Add an item from manual, CSL-like, or previously parsed metadata.

        The Zotero item template determines which fields are accepted for the
        requested item type, preventing invalid cross-type metadata.
        """

        title = title.strip()
        if not title:
            return tool_error("title must not be empty")
        if creators and len(creators) > 200:
            return tool_error("A maximum of 200 creators is supported")
        try:
            zot = _get_zot()
            await _validate_collection(zot, collection_id)
            template = await _zot_call(zot.item_template, item_type)
        except Exception as exc:
            return tool_error(f"Invalid item type or collection: {exc}")

        supplied = fields or {}
        protected = {
            "key",
            "version",
            "itemType",
            "title",
            "creators",
            "parentItem",
            "deleted",
            "collections",
            "tags",
            "relations",
        }
        unknown = sorted(key for key in supplied if key not in template or key in protected)
        if unknown:
            return tool_error(
                f"Unsupported field(s) for Zotero {item_type}: {', '.join(unknown)}"
            )

        for creator in creators or []:
            if not creator.get("creatorType"):
                return tool_error("Every creator must include creatorType")
            if not creator.get("name") and not creator.get("lastName"):
                return tool_error("Every creator must include name or lastName")

        doi = str(supplied.get("DOI", "")).strip()
        isbn = str(supplied.get("ISBN", "")).strip()
        try:
            if doi:
                doi = _normalize_doi(doi)
                duplicate = await _find_duplicate(zot, field="DOI", value=doi)
                if duplicate:
                    return (
                        f"Duplicate: [{duplicate.get('key', '')}] "
                        f"{duplicate.get('title', '')} already in library"
                    )
            elif isbn:
                isbn = _normalize_isbn(isbn)
                duplicate = await _find_duplicate(zot, field="ISBN", value=isbn)
                if duplicate:
                    return (
                        f"Duplicate: [{duplicate.get('key', '')}] "
                        f"{duplicate.get('title', '')} already in library"
                    )
        except Exception as exc:
            return tool_error(f"Duplicate check failed: {exc}")

        template["title"] = title
        template.update(supplied)
        if doi:
            template["DOI"] = doi
        if isbn:
            template["ISBN"] = isbn
        if creators is not None:
            template["creators"] = creators
        if collection_id:
            template["collections"] = [collection_id]

        try:
            result = await _zot_call(zot.create_items, [template])
        except Exception as exc:
            return tool_error(f"Failed to create item: {exc}")
        if result.get("successful"):
            created = next(iter(result["successful"].values()))
            return f"Added [{created.get('key', '?')}] {title}"
        return tool_error(f"Zotero rejected the item: {result.get('failed', result)}")
