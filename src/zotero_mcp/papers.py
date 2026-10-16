"""Tools for adding papers and books to Zotero."""

import asyncio
import re
from typing import Literal
from urllib.parse import unquote

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
    _unpaywall_email,
    _zot_call,
)
from .responses import sanitize_error_message, tool_error
from .tool_annotations import WRITE, WRITE_OPEN_WORLD


def _normalize_doi(doi: str) -> str:
    normalized = str(doi).strip()
    url_form = re.match(r"^(?:https?://)?(?:dx\.|www\.)?doi\.org/", normalized, flags=re.I)
    if url_form:
        # A pasted link: drop tracking parameters and decode %28-style escapes.
        normalized = unquote(re.split(r"[?#]", normalized[url_form.end():], maxsplit=1)[0])
    normalized = re.sub(r"^doi:\s*", "", normalized, flags=re.I).strip()
    # Punctuation picked up from surrounding prose; brackets only when
    # unbalanced, since SICI-style DOIs contain parentheses.
    while normalized and (
        normalized[-1] in ".,;"
        or (normalized[-1] == ")" and normalized.count(")") > normalized.count("("))
        or (normalized[-1] == "]" and normalized.count("]") > normalized.count("["))
    ):
        normalized = normalized[:-1]
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


def _isbn13(isbn: str) -> str:
    """ISBN-13 form of a normalized ISBN, so ISBN-10 and ISBN-13 compare equal."""

    if len(isbn) == 13:
        return isbn
    core = "978" + isbn[:9]
    check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(core)) % 10) % 10
    return core + str(check)


def _has_isbn(data: dict, isbn13: str) -> bool:
    # The ISBN field often holds several hyphenated ISBNs ("978-1-... 978-0-...").
    for token in re.split(r"[\s,;]+", str(data.get("ISBN", ""))):
        try:
            if token and _isbn13(_normalize_isbn(token)) == isbn13:
                return True
        except ValueError:
            continue
    return False


def _first_isbn13(value: str) -> str:
    """ISBN-13 of the first valid ISBN in a field that may hold several, or ""."""

    for token in re.split(r"[\s,;]+", str(value)):
        try:
            if token:
                return _isbn13(_normalize_isbn(token))
        except ValueError:
            continue
    return ""


async def _title_candidates(zot, title: str) -> list[dict]:
    title = re.sub(r"<[^>]+>", "", title).strip()
    if not title or title == "Unknown Title":
        return []
    # Zotero matches the query as a phrase, so "A: B" misses a stored "A : B".
    # Bare words are ANDed instead; use the main title (subtitles are often
    # dropped or reworded) and skip short words.
    main_title = re.split(r"\s*[:?]\s", title, maxsplit=1)[0]
    words = [word for word in re.findall(r"\w[\w'-]*", main_title) if len(word) >= 3][:6]
    query = " ".join(words) or title
    candidates = await _zot_call(zot.top, q=query, limit=50)
    return [candidate.get("data", {}) for candidate in candidates]


async def _find_isbn_duplicate(zot, isbn: str, title: str) -> dict | None:
    """Find an existing item with this ISBN.

    Zotero's search only matches ISBNs exactly as stored (usually hyphenated),
    so search by the resolved title and compare normalized ISBNs.
    """

    target = _isbn13(isbn)
    return next((data for data in await _title_candidates(zot, title) if _has_isbn(data, target)), None)


def _has_doi(data: dict, folded_doi: str) -> bool:
    if str(data.get("DOI", "")).strip().casefold() == folded_doi:
        return True
    # Item types without a DOI field (e.g. older preprints) keep it in Extra.
    return any(
        line.split(":", 1)[1].strip().casefold() == folded_doi
        for line in str(data.get("extra", "")).splitlines()
        if line.casefold().startswith("doi:")
    )


async def _find_doi_duplicate(zot, doi: str, title: str) -> dict | None:
    """Find an existing item with this DOI.

    Zotero's quick search does not index the DOI field (even in "everything"
    mode), so search by the resolved title and compare DOIs exactly.
    """

    folded = doi.casefold()
    return next((data for data in await _title_candidates(zot, title) if _has_doi(data, folded)), None)


def _duplicate_message(duplicate: dict) -> str:
    return (
        f"Duplicate: [{duplicate.get('key', '')}] "
        f"{duplicate.get('title', '')} already in library"
    )


async def _validate_collection(zot, collection_id: str | None) -> None:
    if collection_id:
        await _zot_call(zot.collection, collection_id)


async def _attach_pdf_note(zot, key: str, title: str, pdf_url: str | None) -> str:
    """Attach a PDF if one was found and describe the outcome for the result line."""

    if not pdf_url:
        if not _unpaywall_email():
            return " (no PDF: set UNPAYWALL_EMAIL to fetch open-access PDFs)"
        return ""
    try:
        await _attach_pdf_from_url(zot, key, pdf_url, title)
        return " (with PDF)"
    except Exception as exc:
        return f" (PDF not attached: {sanitize_error_message(str(exc))})"


def register(mcp):
    @mcp.tool(annotations=WRITE_OPEN_WORLD)
    async def add_paper_by_doi(
        doi: str,
        collection_id: str | None = None,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Add a paper to your Zotero library by its DOI.

        Resolves metadata automatically via CrossRef and creates the item in Zotero.
        Optionally add it to a specific collection.

        Args:
            doi: The DOI of the paper (e.g. "10.1038/nature12373")
            collection_id: Optional Zotero collection key to add the paper to
        """
        try:
            doi = _normalize_doi(doi)
            zot = _get_zot(library_id, library_type)
            await _validate_collection(zot, collection_id)
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
        try:
            duplicate = await _find_doi_duplicate(zot, doi, item["title"])
        except Exception as exc:
            return tool_error(f"Duplicate check failed: {exc}")
        if duplicate:
            return _duplicate_message(duplicate)

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
            return f"Added [{key}] {title}" + await _attach_pdf_note(zot, key, title, pdf_url)
        elif result.get("failed"):
            return tool_error(f"Rejected: {list(result['failed'].values())}")
        else:
            return tool_error(f"Unexpected response from Zotero: {result}")

    @mcp.tool(annotations=WRITE_OPEN_WORLD)
    async def add_papers_by_dois(
        dois: list[str],
        collection_id: str | None = None,
        attach_pdfs: bool = False,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
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
            zot = _get_zot(library_id, library_type)
            await _validate_collection(zot, collection_id)
        except Exception as exc:
            return tool_error(f"Could not validate batch import: {exc}")

        duplicate_lines = []
        items = []
        failed_dois = []

        semaphore = asyncio.Semaphore(8)

        async def _resolve_one(doi: str):
            try:
                async with semaphore:
                    cr_data = await _resolve_doi(doi)
                    item = _crossref_to_zotero(cr_data)
                    # Own client per concurrent check (Pyzotero keeps query state).
                    duplicate = await _find_doi_duplicate(
                        _get_zot(library_id, library_type), doi, item["title"]
                    )
                if collection_id:
                    item["collections"] = [collection_id]
                return doi, item, duplicate, None
            except Exception as e:
                return doi, None, None, str(e)

        resolved = await asyncio.gather(*(_resolve_one(doi) for doi in normalized_dois))
        resolved_dois = []
        for doi, item, duplicate, error in resolved:
            if duplicate:
                duplicate_lines.append(
                    f"{doi}: [{duplicate.get('key', '?')}] {duplicate.get('title', '?')}"
                )
            elif item is not None:
                items.append(item)
                resolved_dois.append(doi)
            else:
                failed_dois.append(f"{doi}: {sanitize_error_message(error)}")

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

        pdf_semaphore = asyncio.Semaphore(4)

        async def _pdf_note(index: str, it: dict) -> str:
            if not attach_pdfs:
                return ""
            async with pdf_semaphore:
                pdf_url = await _find_open_access_pdf(resolved_dois[int(index)])
                # Own client per concurrent upload (Pyzotero keeps request state).
                return await _attach_pdf_note(
                    _get_zot(library_id, library_type),
                    it.get("key", ""),
                    it.get("data", {}).get("title", ""),
                    pdf_url,
                )

        notes = await asyncio.gather(
            *(_pdf_note(index, it) for index, it in successful.items())
        )
        lines = [f"Added {len(successful)}/{len(normalized_dois)} unique valid DOI(s):"]
        for it, note in zip(successful.values(), notes):
            lines.append(f"  [{it.get('key','?')}] {it.get('data',{}).get('title','?')}{note}")
        for index, failure in result.get("failed", {}).items():
            message = failure.get("message", failure) if isinstance(failure, dict) else failure
            lines.append(f"  Rejected {resolved_dois[int(index)]}: {message}")
        if duplicate_lines:
            lines.append("Duplicates skipped:\n  " + "\n  ".join(duplicate_lines))
        if invalid_dois:
            lines.extend(f"  Invalid: {error}" for error in invalid_dois)
        if failed_dois:
            lines.extend(f"  Failed: {f}" for f in failed_dois)

        return "\n".join(lines)

    @mcp.tool(annotations=WRITE_OPEN_WORLD)
    async def add_paper_by_arxiv_id(
        arxiv_id: str,
        collection_id: str | None = None,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
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
            zot = _get_zot(library_id, library_type)
            await _validate_collection(zot, collection_id)
            arxiv_url = f"https://arxiv.org/abs/{arxiv_id}"
        except Exception as exc:
            return tool_error(f"Could not validate arXiv import: {exc}")

        try:
            entry = await _resolve_arxiv(arxiv_id)
        except ValueError as e:
            return tool_error(str(e))
        except Exception as e:
            return tool_error(f"Failed to fetch arXiv metadata: {e}")

        # Any version of the paper counts: 2301.07041v2 duplicates an item
        # saved as 2301.07041 or 2301.07041v1, but 2301.0704 does not.
        base_id = re.sub(r"v\d+$", "", arxiv_id)
        id_pattern = re.escape(base_id) + r"(?:v\d+)?(?![\d.])"
        try:
            # Zotero's search ignores url/extra (the ID search only hits items
            # whose PDF text mentions it), so also search by the paper's title.
            title = re.sub(r"\s+", " ", entry.findtext("atom:title", "", ARXIV_NS)).strip()
            candidates = await _title_candidates(zot, title)
            by_id = await _zot_call(zot.top, q=base_id, qmode="everything", limit=100)
            candidates += [existing.get("data", {}) for existing in by_id]
        except Exception as exc:
            return tool_error(f"Duplicate check failed: {exc}")
        for data in candidates:
            if (
                # The Zotero connector stores http:// arXiv URLs.
                re.search(r"arxiv\.org/(?:abs|pdf)/" + id_pattern, data.get("url", ""), re.I)
                or re.search(r"arXiv:\s*" + id_pattern, data.get("extra", ""), re.I)
                or str(data.get("DOI", "")).casefold() == f"10.48550/arxiv.{base_id}".casefold()
            ):
                return _duplicate_message(data)

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
            try:
                duplicate = await _find_doi_duplicate(zot, doi, item["title"])
            except Exception as exc:
                return tool_error(f"Duplicate check failed: {exc}")
            if duplicate:
                return _duplicate_message(duplicate)
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
            return f"Added [{key}] {title}" + await _attach_pdf_note(zot, key, title, pdf_url)
        elif result.get("failed"):
            return tool_error(f"Rejected: {list(result['failed'].values())}")
        else:
            return tool_error(f"Unexpected response from Zotero: {result}")

    @mcp.tool(annotations=WRITE_OPEN_WORLD)
    async def add_book_by_isbn(
        isbn: str,
        collection_id: str | None = None,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
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
            zot = _get_zot(library_id, library_type)
            await _validate_collection(zot, collection_id)
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
        try:
            duplicate = await _find_isbn_duplicate(zot, isbn, item["title"])
        except Exception as exc:
            return tool_error(f"Duplicate check failed: {exc}")
        if duplicate:
            return _duplicate_message(duplicate)

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
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
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
            zot = _get_zot(library_id, library_type)
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
        raw_isbn = str(supplied.get("ISBN", "")).strip()
        try:
            doi = _normalize_doi(doi) if doi else ""
        except ValueError as exc:
            return tool_error(str(exc))
        isbn13 = _first_isbn13(raw_isbn) if raw_isbn else ""
        if raw_isbn and not isbn13:
            return tool_error(f"Invalid ISBN: {raw_isbn}")
        try:
            if doi:
                duplicate = await _find_doi_duplicate(zot, doi, title)
            elif isbn13:
                duplicate = await _find_isbn_duplicate(zot, isbn13, title)
            else:
                duplicate = None
        except Exception as exc:
            return tool_error(f"Duplicate check failed: {exc}")
        if duplicate:
            return _duplicate_message(duplicate)

        template["title"] = title
        template.update(supplied)
        if doi:
            template["DOI"] = doi
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
