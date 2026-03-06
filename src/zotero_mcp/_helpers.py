"""Shared helpers, configuration, and metadata resolvers."""

import hashlib
import ipaddress
import os
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from pyzotero import zotero

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
ZOTERO_LIBRARY_ID = os.environ.get("ZOTERO_LIBRARY_ID", "")
ZOTERO_API_KEY = os.environ.get("ZOTERO_API_KEY", "")
ZOTERO_LIBRARY_TYPE = os.environ.get("ZOTERO_LIBRARY_TYPE", "user")

CROSSREF_API = "https://api.crossref.org/works"
CROSSREF_MAILTO = os.environ.get("CROSSREF_MAILTO", "")

# WebDAV config (optional — if set, file attachments go to WebDAV instead of Zotero storage)
_raw_webdav_url = os.environ.get("ZOTERO_WEBDAV_URL", "").rstrip("/")
ZOTERO_WEBDAV_URL = f"{_raw_webdav_url}/zotero" if _raw_webdav_url else ""
ZOTERO_WEBDAV_USER = os.environ.get("ZOTERO_WEBDAV_USER", "")
ZOTERO_WEBDAV_PASSWORD = os.environ.get("ZOTERO_WEBDAV_PASSWORD", "")

MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB limit for PDF downloads

UNPAYWALL_API = "https://api.unpaywall.org/v2"

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}

OPENLIBRARY_ISBN_API = "https://openlibrary.org/isbn"
OPENLIBRARY_API = "https://openlibrary.org"


def _is_safe_url(url: str) -> bool:
    """Check that a URL is safe to fetch (no SSRF to internal networks)."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = p.hostname or ""
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        if host in ("localhost", "") or host.endswith(".local"):
            return False
    return True


def _get_zot() -> zotero.Zotero:
    """Create a Pyzotero client from environment config."""
    if not ZOTERO_LIBRARY_ID or not ZOTERO_API_KEY:
        raise ValueError(
            "ZOTERO_LIBRARY_ID and ZOTERO_API_KEY environment variables must be set. "
            "Get your API key at https://www.zotero.org/settings/keys"
        )
    return zotero.Zotero(ZOTERO_LIBRARY_ID, ZOTERO_LIBRARY_TYPE, ZOTERO_API_KEY)


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


# ---------------------------------------------------------------------------
# CrossRef
# ---------------------------------------------------------------------------

def _crossref_to_zotero(cr: dict[str, Any]) -> dict[str, Any]:
    """Convert a CrossRef work record to a Zotero item dict."""
    msg = cr.get("message", cr)

    creators = []
    for author in msg.get("author", []):
        creators.append({
            "creatorType": "author",
            "firstName": author.get("given", ""),
            "lastName": author.get("family", ""),
        })

    date_parts = None
    for field in ("published-print", "published-online", "issued", "created"):
        if field in msg and "date-parts" in msg[field]:
            date_parts = msg[field]["date-parts"][0]
            break

    date_str = ""
    if date_parts:
        parts = [str(p) for p in date_parts if p]
        date_str = "-".join(parts)

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

    titles = msg.get("title", [])
    title = titles[0] if titles else "Unknown Title"

    abstract = msg.get("abstract", "")
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


async def _resolve_doi(doi: str) -> dict[str, Any]:
    """Fetch metadata for a DOI from the CrossRef API."""
    headers = {"Accept": "application/json"}
    if CROSSREF_MAILTO:
        mailto = CROSSREF_MAILTO.replace("\r", "").replace("\n", "")
        headers["User-Agent"] = f"ZoteroMCP/0.1 (mailto:{mailto})"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{CROSSREF_API}/{quote(doi, safe='')}", headers=headers)
        resp.raise_for_status()
        return resp.json()


async def _find_open_access_pdf(doi: str) -> str | None:
    """Query Unpaywall for an open-access PDF URL. Returns URL or None."""
    email = CROSSREF_MAILTO or "zotero-mcp@example.com"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{UNPAYWALL_API}/{doi}", params={"email": email})
            resp.raise_for_status()
            data = resp.json()
        best = data.get("best_oa_location") or {}
        return best.get("url_for_pdf") or best.get("url")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# File attachment helpers (WebDAV / local / URL)
# ---------------------------------------------------------------------------

def _use_webdav() -> bool:
    """Check if WebDAV is configured for file storage."""
    return bool(ZOTERO_WEBDAV_URL and ZOTERO_WEBDAV_USER and ZOTERO_WEBDAV_PASSWORD)


async def _attach_file_webdav(zot: zotero.Zotero, parent_key: str, file_path: str) -> str | None:
    """Create an attachment item and upload the file to WebDAV. Returns attachment key or None."""
    try:
        template = zot.item_template("attachment", "imported_file")
        template["title"] = os.path.basename(file_path)
        template["filename"] = os.path.basename(file_path)
        template["contentType"] = "application/pdf"
        result = zot.create_items([template], parentid=parent_key)
        if not result.get("successful"):
            return None
        created = list(result["successful"].values())[0]
        att_key = created["key"]

        file_data = open(file_path, "rb").read()
        md5 = hashlib.md5(file_data).hexdigest()
        mtime = int(os.path.getmtime(file_path) * 1000)

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            zip_path = tmp.name
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(file_path, os.path.basename(file_path))
            with open(zip_path, "rb") as f:
                zip_data = f.read()
        finally:
            os.unlink(zip_path)

        prop_xml = (
            f'<properties version="1">'
            f'<mtime>{mtime}</mtime>'
            f'<hash>{md5}</hash>'
            f'</properties>'
        )

        auth = (ZOTERO_WEBDAV_USER, ZOTERO_WEBDAV_PASSWORD)
        async with httpx.AsyncClient(timeout=60, auth=auth) as client:
            r1 = await client.put(
                f"{ZOTERO_WEBDAV_URL}/{att_key}.zip",
                content=zip_data,
                headers={"Content-Type": "application/zip"},
            )
            r1.raise_for_status()
            r2 = await client.put(
                f"{ZOTERO_WEBDAV_URL}/{att_key}.prop",
                content=prop_xml.encode(),
                headers={"Content-Type": "text/xml"},
            )
            r2.raise_for_status()

        att_item = zot.item(att_key)
        att_data = att_item["data"]
        att_data["md5"] = md5
        att_data["mtime"] = mtime
        zot.update_item(att_data)

        return att_key
    except Exception:
        return None


async def _attach_file_local(zot: zotero.Zotero, parent_key: str, file_path: str) -> str | None:
    """Attach a file using Zotero's built-in file storage. Returns parent key or None."""
    try:
        zot.attachment_simple([file_path], parent_key)
        return parent_key
    except Exception:
        return None


async def _attach_pdf_from_url(zot: zotero.Zotero, parent_key: str, url: str) -> str | None:
    """Download a PDF from a URL and attach it to a Zotero item. Returns attachment key or None."""
    if not _is_safe_url(url):
        return None
    tmp_path = None
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                    tmp_path = f.name
                    size = 0
                    async for chunk in resp.aiter_bytes(8192):
                        size += len(chunk)
                        if size > MAX_PDF_BYTES:
                            return None
                        f.write(chunk)
                with open(tmp_path, "rb") as f:
                    header = f.read(5)
                if "pdf" not in content_type and header != b"%PDF-":
                    return None
        if _use_webdav():
            result = await _attach_file_webdav(zot, parent_key, tmp_path)
        else:
            result = await _attach_file_local(zot, parent_key, tmp_path)
        return result
    except Exception:
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def _download_pdf(zot: zotero.Zotero, item_key: str) -> tuple[str, str]:
    """Download the PDF attachment for an item to a temp file.

    Returns (tmp_path, attachment_key). Caller must delete tmp_path.
    Raises ValueError if no PDF attachment found.
    """
    children = zot.children(item_key)
    att_key = None
    for child in children:
        d = child.get("data", {})
        if d.get("itemType") == "attachment" and d.get("contentType") == "application/pdf":
            att_key = d.get("key")
            break
    if not att_key:
        raise ValueError(f"No PDF attachment found for item {item_key}")

    if _use_webdav():
        auth = (ZOTERO_WEBDAV_USER, ZOTERO_WEBDAV_PASSWORD)
        async with httpx.AsyncClient(timeout=60, auth=auth) as client:
            resp = await client.get(f"{ZOTERO_WEBDAV_URL}/{att_key}.zip")
            resp.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(resp.content)
            zip_path = tmp.name
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                pdf_names = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
                if not pdf_names:
                    raise ValueError("No PDF found in WebDAV zip")
                pdf_data = zf.read(pdf_names[0])
        finally:
            os.unlink(zip_path)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_data)
            return tmp.name, att_key
    else:
        file_data = zot.file(att_key)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(file_data)
            return tmp.name, att_key


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

async def _resolve_arxiv(arxiv_id: str) -> ET.Element:
    """Fetch the Atom entry for an arXiv paper."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(ARXIV_API, params={"id_list": arxiv_id})
        resp.raise_for_status()
    root = ET.fromstring(resp.text)
    entry = root.find("atom:entry", ARXIV_NS)
    if entry is None:
        raise ValueError(f"No entry found for arXiv ID: {arxiv_id}")
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
    date_str = published[:10] if published else ""

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
# Open Library (ISBN)
# ---------------------------------------------------------------------------

async def _resolve_isbn(isbn: str) -> dict[str, Any]:
    """Fetch book metadata from Open Library by ISBN."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(f"{OPENLIBRARY_ISBN_API}/{quote(isbn, safe='')}.json")
        resp.raise_for_status()
        data = resp.json()

        authors = []
        for author_ref in data.get("authors", []):
            key = author_ref.get("key", "")
            if key and re.fullmatch(r"/authors/OL\d+A", key):
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
