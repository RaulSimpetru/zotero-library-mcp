"""Zotero MCP Server — Add papers by DOI and manage your Zotero library."""

import os
import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from pyzotero import zotero

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
ZOTERO_LIBRARY_ID = os.environ.get("ZOTERO_LIBRARY_ID", "")
ZOTERO_API_KEY = os.environ.get("ZOTERO_API_KEY", "")
ZOTERO_LIBRARY_TYPE = os.environ.get("ZOTERO_LIBRARY_TYPE", "user")

CROSSREF_API = "https://api.crossref.org/works"
CROSSREF_MAILTO = os.environ.get("CROSSREF_MAILTO", "")  # optional, for polite pool

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "zotero",
    instructions="Add papers to Zotero by DOI and manage your library",
)


def _get_zot() -> zotero.Zotero:
    """Create a Pyzotero client from environment config."""
    if not ZOTERO_LIBRARY_ID or not ZOTERO_API_KEY:
        raise ValueError(
            "ZOTERO_LIBRARY_ID and ZOTERO_API_KEY environment variables must be set. "
            "Get your API key at https://www.zotero.org/settings/keys"
        )
    return zotero.Zotero(ZOTERO_LIBRARY_ID, ZOTERO_LIBRARY_TYPE, ZOTERO_API_KEY)


# ---------------------------------------------------------------------------
# CrossRef → Zotero metadata mapping
# ---------------------------------------------------------------------------

def _crossref_to_zotero(cr: dict[str, Any]) -> dict[str, Any]:
    """Convert a CrossRef work record to a Zotero item dict."""
    msg = cr.get("message", cr)  # handle both /works/{doi} and nested

    # Map creators
    creators = []
    for author in msg.get("author", []):
        creators.append({
            "creatorType": "author",
            "firstName": author.get("given", ""),
            "lastName": author.get("family", ""),
        })

    # Extract date
    date_parts = None
    for field in ("published-print", "published-online", "issued", "created"):
        if field in msg and "date-parts" in msg[field]:
            date_parts = msg[field]["date-parts"][0]
            break

    date_str = ""
    if date_parts:
        parts = [str(p) for p in date_parts if p]
        date_str = "-".join(parts)  # e.g. "2023-6-15" or "2023"

    # Determine item type
    cr_type = msg.get("type", "")
    type_map = {
        "journal-article": "journalArticle",
        "proceedings-article": "conferencePaper",
        "book-chapter": "bookSection",
        "book": "book",
        "posted-content": "preprint",
        "report": "report",
        "thesis": "thesis",
        "dataset": "document",
    }
    item_type = type_map.get(cr_type, "journalArticle")

    # Build title
    titles = msg.get("title", [])
    title = titles[0] if titles else "Unknown Title"

    # Build abstract
    abstract = msg.get("abstract", "")
    # CrossRef sometimes wraps in <jats:p> tags
    if abstract:
        abstract = re.sub(r"<[^>]+>", "", abstract)

    item = {
        "itemType": item_type,
        "title": title,
        "creators": creators,
        "abstractNote": abstract,
        "date": date_str,
        "DOI": msg.get("DOI", ""),
        "url": msg.get("URL", ""),
        "publicationTitle": (msg.get("container-title") or [""])[0],
        "volume": msg.get("volume", ""),
        "issue": msg.get("issue", ""),
        "pages": msg.get("page", ""),
        "ISSN": (msg.get("ISSN") or [""])[0],
        "language": msg.get("language", ""),
    }

    return item


def _fmt_item(data: dict[str, Any]) -> str:
    """Format a Zotero item as a compact one-liner."""
    key = data.get("key", "?")
    title = data.get("title", "Untitled")
    creators = data.get("creators", [])
    names = [c.get("lastName", "") for c in creators[:3]]
    author = ", ".join(n for n in names if n)
    if len(creators) > 3:
        author += " et al."
    date = data.get("date", "")
    year = date[:4] if date else ""
    doi = data.get("DOI", "")
    parts = [f"[{key}]", title]
    if author:
        parts.append(f"({author}, {year})" if year else f"({author})")
    elif year:
        parts.append(f"({year})")
    if doi:
        parts.append(f"DOI:{doi}")
    return " ".join(parts)


async def _resolve_doi(doi: str) -> dict[str, Any]:
    """Fetch metadata for a DOI from the CrossRef API."""
    headers = {"Accept": "application/json"}
    if CROSSREF_MAILTO:
        headers["User-Agent"] = f"ZoteroMCP/0.1 (mailto:{CROSSREF_MAILTO})"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{CROSSREF_API}/{doi}", headers=headers)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# arXiv → Zotero metadata mapping
# ---------------------------------------------------------------------------

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


async def _resolve_arxiv(arxiv_id: str) -> ET.Element:
    """Fetch the Atom entry for an arXiv paper."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(ARXIV_API, params={"id_list": arxiv_id})
        resp.raise_for_status()
    root = ET.fromstring(resp.text)
    entry = root.find("atom:entry", ARXIV_NS)
    if entry is None:
        raise ValueError(f"No entry found for arXiv ID: {arxiv_id}")
    # arXiv returns an entry even for invalid IDs — check for an <id> that matches
    entry_id = entry.findtext("atom:id", "", ARXIV_NS)
    if arxiv_id not in entry_id:
        raise ValueError(f"arXiv returned no matching entry for: {arxiv_id}")
    return entry


def _arxiv_to_zotero(entry: ET.Element, arxiv_id: str) -> dict[str, Any]:
    """Convert an arXiv Atom entry to a Zotero item dict."""
    title = entry.findtext("atom:title", "", ARXIV_NS).strip()
    title = re.sub(r"\s+", " ", title)

    abstract = entry.findtext("atom:summary", "", ARXIV_NS).strip()
    abstract = re.sub(r"\s+", " ", abstract)

    creators = []
    for author_el in entry.findall("atom:author", ARXIV_NS):
        name = author_el.findtext("atom:name", "", ARXIV_NS).strip()
        if name:
            parts = name.rsplit(" ", 1)
            if len(parts) == 2:
                creators.append({"creatorType": "author", "firstName": parts[0], "lastName": parts[1]})
            else:
                creators.append({"creatorType": "author", "lastName": name, "firstName": ""})

    published = entry.findtext("atom:published", "", ARXIV_NS)
    date_str = published[:10] if published else ""  # "2023-01-17T..."

    return {
        "itemType": "preprint",
        "title": title,
        "creators": creators,
        "abstractNote": abstract,
        "date": date_str,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "extra": f"arXiv:{arxiv_id}",
    }


# ---------------------------------------------------------------------------
# Open Library → Zotero metadata mapping (for ISBNs)
# ---------------------------------------------------------------------------

OPENLIBRARY_ISBN_API = "https://openlibrary.org/isbn"
OPENLIBRARY_API = "https://openlibrary.org"


async def _resolve_isbn(isbn: str) -> dict[str, Any]:
    """Fetch book metadata from Open Library by ISBN."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(f"{OPENLIBRARY_ISBN_API}/{isbn}.json")
        resp.raise_for_status()
        data = resp.json()

        # Resolve author names (the ISBN endpoint only has author keys)
        authors = []
        for author_ref in data.get("authors", []):
            key = author_ref.get("key", "")
            if key:
                try:
                    author_resp = await client.get(f"{OPENLIBRARY_API}{key}.json")
                    author_resp.raise_for_status()
                    authors.append(author_resp.json())
                except Exception:
                    pass
        data["_resolved_authors"] = authors

    return data


def _openlibrary_to_zotero(data: dict[str, Any], isbn: str) -> dict[str, Any]:
    """Convert Open Library book data to a Zotero item dict."""
    title = data.get("title", "Unknown Title")
    subtitle = data.get("subtitle", "")
    if subtitle:
        title = f"{title}: {subtitle}"

    creators = []
    for author in data.get("_resolved_authors", []):
        name = author.get("name", "")
        if name:
            parts = name.rsplit(" ", 1)
            if len(parts) == 2:
                creators.append({"creatorType": "author", "firstName": parts[0], "lastName": parts[1]})
            else:
                creators.append({"creatorType": "author", "lastName": name, "firstName": ""})

    publishers = data.get("publishers", [])
    publisher = publishers[0] if publishers else ""

    publish_date = data.get("publish_date", "")

    num_pages = data.get("number_of_pages", "")

    languages = data.get("languages", [])
    language = ""
    if languages:
        lang_key = languages[0].get("key", "")
        # e.g. "/languages/eng" → "eng"
        language = lang_key.rsplit("/", 1)[-1] if lang_key else ""

    return {
        "itemType": "book",
        "title": title,
        "creators": creators,
        "publisher": publisher,
        "date": publish_date,
        "numPages": str(num_pages) if num_pages else "",
        "ISBN": isbn,
        "language": language,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def add_paper_by_doi(doi: str, collection_id: str | None = None) -> str:
    """Add a paper to your Zotero library by its DOI.

    Resolves metadata automatically via CrossRef and creates the item in Zotero.
    Optionally add it to a specific collection.

    Args:
        doi: The DOI of the paper (e.g. "10.1038/nature12373")
        collection_id: Optional Zotero collection key to add the paper to
    """
    # 0. Check for duplicates by scanning DOI fields
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
        pass  # If duplicate check fails, proceed with adding

    # 1. Resolve DOI → metadata
    try:
        cr_data = await _resolve_doi(doi)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"DOI not found: {doi}. Please check the DOI is correct."
        return f"CrossRef API error: {e}"
    except Exception as e:
        return f"Failed to resolve DOI: {e}"

    # 2. Map to Zotero format
    item = _crossref_to_zotero(cr_data)

    # 3. Add collection if specified
    if collection_id:
        item["collections"] = [collection_id]

    # 4. Create in Zotero
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

    # 0. Check for duplicates by scanning URL/extra fields
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

    # 1. Fetch arXiv metadata
    try:
        entry = await _resolve_arxiv(arxiv_id)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Failed to fetch arXiv metadata: {e}"

    # 2. Check if arXiv entry has a DOI link — if so, use CrossRef for richer metadata
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
            # Ensure arXiv info is preserved
            if not item.get("url"):
                item["url"] = arxiv_url
            extra = item.get("extra", "")
            if f"arXiv:{arxiv_id}" not in extra:
                item["extra"] = f"arXiv:{arxiv_id}\n{extra}".strip()
        except Exception:
            # Fall back to arXiv-only metadata
            item = _arxiv_to_zotero(entry, arxiv_id)
    else:
        item = _arxiv_to_zotero(entry, arxiv_id)

    # 3. Add collection if specified
    if collection_id:
        item["collections"] = [collection_id]

    # 4. Create in Zotero
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

    # 0. Check for duplicates by scanning ISBN fields
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

    # 1. Resolve ISBN → metadata
    try:
        ol_data = await _resolve_isbn(isbn)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"ISBN not found: {isbn}. Please check the ISBN is correct."
        return f"Open Library API error: {e}"
    except Exception as e:
        return f"Failed to resolve ISBN: {e}"

    # 2. Map to Zotero format
    item = _openlibrary_to_zotero(ol_data, isbn)

    # 3. Add collection if specified
    if collection_id:
        item["collections"] = [collection_id]

    # 4. Create in Zotero
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


@mcp.tool()
async def search_library(query: str, limit: int = 10) -> str:
    """Search your Zotero library.

    Args:
        query: Search query (searches titles, authors, tags, etc.)
        limit: Maximum number of results (default 10)
    """
    zot = _get_zot()
    results = zot.items(q=query, limit=limit)

    if not results:
        return "No results."

    lines = [_fmt_item(item.get("data", {})) for item in results]
    return "\n".join(lines)


@mcp.tool()
async def list_collections() -> str:
    """List all collections in your Zotero library."""
    zot = _get_zot()
    collections = zot.collections()

    if not collections:
        return "No collections."

    parent_map = {}
    for col in collections:
        d = col.get("data", {})
        parent_map[d.get("key", "")] = d.get("parentCollection", None)

    def _depth(key: str) -> int:
        d, cur = 0, key
        while parent_map.get(cur):
            d += 1
            cur = parent_map[cur]
        return d

    lines = []
    for col in collections:
        d = col.get("data", {})
        key = d.get("key", "")
        name = d.get("name", "?")
        n = d.get("meta", {}).get("numItems", col.get("meta", {}).get("numItems", 0))
        lines.append(f"{'  ' * _depth(key)}[{key}] {name} ({n})")

    return "\n".join(lines)


@mcp.tool()
async def add_to_collection(item_key: str, collection_id: str) -> str:
    """Add an existing Zotero item to a collection.

    Args:
        item_key: The Zotero item key (from search results)
        collection_id: The collection key to add it to
    """
    zot = _get_zot()

    try:
        item = zot.item(item_key)
    except Exception as e:
        return f"Could not find item {item_key}: {e}"

    data = item.get("data", {})
    collections = data.get("collections", [])

    if collection_id in collections:
        return f"Item is already in collection {collection_id}."

    collections.append(collection_id)
    data["collections"] = collections

    try:
        zot.update_item(data)
    except Exception as e:
        return f"Failed to update item: {e}"

    return f"Added '{data.get('title', item_key)}' to {collection_id}."


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
async def remove_from_collection(item_key: str, collection_id: str) -> str:
    """Remove an item from a collection without deleting it from the library.

    Args:
        item_key: The Zotero item key
        collection_id: The collection key to remove it from
    """
    zot = _get_zot()

    try:
        item = zot.item(item_key)
    except Exception as e:
        return f"Could not find item {item_key}: {e}"

    data = item.get("data", {})
    collections = data.get("collections", [])

    if collection_id not in collections:
        return f"Item is not in collection {collection_id}."

    title = data.get("title", item_key)

    try:
        zot.deletefrom_collection(collection_id, item)
    except Exception as e:
        return f"Failed to remove from collection: {e}"

    return f"Removed '{title}' from {collection_id}."


@mcp.tool()
async def create_collection(name: str, parent_collection_id: str | None = None) -> str:
    """Create a new collection in your Zotero library.

    Args:
        name: Name for the new collection
        parent_collection_id: Optional parent collection key to nest under
    """
    zot = _get_zot()

    payload = [{"name": name}]
    if parent_collection_id:
        payload[0]["parentCollection"] = parent_collection_id

    try:
        result = zot.create_collections(payload)
    except Exception as e:
        return f"Failed to create collection: {e}"

    successful = result.get("successful", {})
    if successful:
        col = list(successful.values())[0]
        key = col.get("key", "unknown")
        return f"Created [{key}] {name}"

    failed = result.get("failed", {})
    if failed:
        errors = list(failed.values())
        return f"Zotero rejected the collection: {errors}"

    return f"Unexpected response from Zotero: {result}"


@mcp.tool()
async def get_collection_items(collection_id: str, limit: int = 25) -> str:
    """Get all items in a specific collection.

    Args:
        collection_id: The collection key to browse
        limit: Maximum number of items to return (default 25)
    """
    zot = _get_zot()

    try:
        results = zot.collection_items(collection_id, limit=limit)
    except Exception as e:
        return f"Could not fetch collection items: {e}"

    if not results:
        return "Empty collection."

    lines = []
    for item in results:
        data = item.get("data", {})
        if data.get("itemType") not in ("attachment", "note"):
            lines.append(_fmt_item(data))

    return "\n".join(lines) if lines else "Empty collection."


@mcp.tool()
async def add_tags(item_key: str, tags: list[str]) -> str:
    """Add one or more tags to a Zotero item.

    Args:
        item_key: The Zotero item key
        tags: List of tags to add
    """
    zot = _get_zot()

    try:
        item = zot.item(item_key)
    except Exception as e:
        return f"Could not find item {item_key}: {e}"

    title = item.get("data", {}).get("title", item_key)

    try:
        zot.add_tags(item, *tags)
    except Exception as e:
        return f"Failed to add tags: {e}"

    return f"Tagged '{title}': {', '.join(tags)}"


@mcp.tool()
async def remove_tags(item_key: str, tags: list[str]) -> str:
    """Remove one or more tags from a Zotero item.

    Args:
        item_key: The Zotero item key
        tags: List of tags to remove
    """
    zot = _get_zot()

    try:
        item = zot.item(item_key)
    except Exception as e:
        return f"Could not find item {item_key}: {e}"

    data = item.get("data", {})
    title = data.get("title", item_key)
    current_tags = data.get("tags", [])
    tags_lower = {t.lower() for t in tags}
    new_tags = [t for t in current_tags if t.get("tag", "").lower() not in tags_lower]

    removed_count = len(current_tags) - len(new_tags)
    if removed_count == 0:
        return f"None of the specified tags were found on '{title}'."

    data["tags"] = new_tags
    try:
        zot.update_item(data)
    except Exception as e:
        return f"Failed to update item: {e}"

    return f"Removed {removed_count} tag(s) from '{title}'."


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

        # Normalize for comparison
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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    """Run the Zotero MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
