"""Tools for adding papers and books to Zotero."""

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
)


def register(mcp):
    @mcp.tool()
    async def add_paper_by_doi(doi: str, collection_id: str | None = None) -> str:
        """Add a paper to your Zotero library by its DOI.

        Resolves metadata automatically via CrossRef and creates the item in Zotero.
        Optionally add it to a specific collection.

        Args:
            doi: The DOI of the paper (e.g. "10.1038/nature12373")
            collection_id: Optional Zotero collection key to add the paper to
        """
        try:
            zot = _get_zot()
            doi_lower = doi.lower().strip()
            for item in zot.everything(zot.top()):
                item_doi = item.get("data", {}).get("DOI", "")
                if item_doi and item_doi.lower().strip() == doi_lower:
                    key = item["data"].get("key", "")
                    title = item["data"].get("title", "")
                    return f"Duplicate: [{key}] {title} already in library"
        except Exception:
            pass

        try:
            cr_data = await _resolve_doi(doi)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"DOI not found: {doi}. Please check the DOI is correct."
            return f"CrossRef API error: {e}"
        except Exception as e:
            return f"Failed to resolve DOI: {e}"

        item = _crossref_to_zotero(cr_data)

        if collection_id:
            item["collections"] = [collection_id]

        try:
            result = zot.create_items([item])
        except Exception as e:
            return f"Failed to create item in Zotero: {e}"

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
            return f"Rejected: {list(result['failed'].values())}"
        else:
            return f"Unexpected: {result}"

    @mcp.tool()
    async def add_papers_by_dois(dois: list[str], collection_id: str | None = None) -> str:
        """Add multiple papers to Zotero by their DOIs (batch, up to 50).

        Args:
            dois: List of DOIs to add
            collection_id: Optional Zotero collection key to add all papers to
        """
        if len(dois) > 50:
            return "Zotero API supports a maximum of 50 items per batch. Please split into smaller batches."

        items = []
        failed_dois = []

        for doi in dois:
            try:
                cr_data = await _resolve_doi(doi)
                item = _crossref_to_zotero(cr_data)
                if collection_id:
                    item["collections"] = [collection_id]
                items.append(item)
            except Exception as e:
                failed_dois.append(f"{doi}: {e}")

        if not items:
            return f"Could not resolve any DOIs.\nErrors:\n" + "\n".join(failed_dois)

        try:
            zot = _get_zot()
            result = zot.create_items(items)
        except Exception as e:
            return f"Failed to create items in Zotero: {e}"

        successful = result.get("successful", {})
        zot_failed = len(result.get("failed", {}))

        lines = [f"Added {len(successful)}/{len(dois)}:"]
        for it in successful.values():
            lines.append(f"  [{it.get('key','?')}] {it.get('data',{}).get('title','?')}")
        if zot_failed:
            lines.append(f"Rejected: {zot_failed}")
        if failed_dois:
            lines.extend(f"  Failed: {f}" for f in failed_dois)

        return "\n".join(lines)

    @mcp.tool()
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

        try:
            zot = _get_zot()
            arxiv_url = f"https://arxiv.org/abs/{arxiv_id}"
            for existing in zot.everything(zot.top()):
                data = existing.get("data", {})
                if arxiv_url in data.get("url", "") or f"arXiv:{arxiv_id}" in data.get("extra", ""):
                    key = data.get("key", "")
                    title = data.get("title", "")
                    return f"Duplicate: [{key}] {title} already in library"
        except Exception:
            pass

        try:
            entry = await _resolve_arxiv(arxiv_id)
        except ValueError as e:
            return str(e)
        except Exception as e:
            return f"Failed to fetch arXiv metadata: {e}"

        doi = None
        for link in entry.findall("atom:link", ARXIV_NS):
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
            result = zot.create_items([item])
        except Exception as e:
            return f"Failed to create item in Zotero: {e}"

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
            return f"Rejected: {list(result['failed'].values())}"
        else:
            return f"Unexpected: {result}"

    @mcp.tool()
    async def add_book_by_isbn(isbn: str, collection_id: str | None = None) -> str:
        """Add a book to your Zotero library by its ISBN.

        Resolves metadata automatically via Open Library and creates the item in Zotero.
        Optionally add it to a specific collection.

        Args:
            isbn: The ISBN of the book (e.g. "9780262046824")
            collection_id: Optional Zotero collection key to add the book to
        """
        isbn = re.sub(r"[- ]", "", isbn.strip())

        try:
            zot = _get_zot()
            for existing in zot.everything(zot.top()):
                data = existing.get("data", {})
                existing_isbn = re.sub(r"[- ]", "", data.get("ISBN", ""))
                if existing_isbn and existing_isbn == isbn:
                    key = data.get("key", "")
                    title = data.get("title", "")
                    return f"Duplicate: [{key}] {title} already in library"
        except Exception:
            pass

        try:
            ol_data = await _resolve_isbn(isbn)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"ISBN not found: {isbn}. Please check the ISBN is correct."
            return f"Open Library API error: {e}"
        except Exception as e:
            return f"Failed to resolve ISBN: {e}"

        item = _openlibrary_to_zotero(ol_data, isbn)

        if collection_id:
            item["collections"] = [collection_id]

        try:
            result = zot.create_items([item])
        except Exception as e:
            return f"Failed to create item in Zotero: {e}"

        if result.get("successful"):
            created = list(result["successful"].values())[0]
            key = created.get("key", "unknown")
            title = created.get("data", {}).get("title", item["title"])
            return f"Added [{key}] {title}"
        elif result.get("failed"):
            return f"Rejected: {list(result['failed'].values())}"
        else:
            return f"Unexpected: {result}"
