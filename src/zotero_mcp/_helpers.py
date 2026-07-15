"""Shared helpers, configuration, and metadata resolvers."""

import asyncio
import hashlib
import ipaddress
import mimetypes
import os
import re
import socket
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

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
UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", CROSSREF_MAILTO)

# WebDAV config (optional — if set, file attachments go to WebDAV instead of Zotero storage)
_raw_webdav_url = os.environ.get("ZOTERO_WEBDAV_URL", "").rstrip("/")
ZOTERO_WEBDAV_URL = f"{_raw_webdav_url}/zotero" if _raw_webdav_url else ""
ZOTERO_WEBDAV_USER = os.environ.get("ZOTERO_WEBDAV_USER", "")
ZOTERO_WEBDAV_PASSWORD = os.environ.get("ZOTERO_WEBDAV_PASSWORD", "")

MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB limit for PDF downloads
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_WEBDAV_ZIP_BYTES = 110 * 1024 * 1024
MAX_REDIRECTS = 5

UNPAYWALL_API = "https://api.unpaywall.org/v2"

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

OPENLIBRARY_ISBN_API = "https://openlibrary.org/isbn"
OPENLIBRARY_API = "https://openlibrary.org"


def _is_safe_url(url: str) -> bool:
    """Perform the non-network portion of public URL validation."""
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


async def _validate_public_url(url: str) -> None:
    """Reject URLs whose hostname resolves to a private or special-use address."""

    if not _is_safe_url(url):
        raise ValueError("URL is not a safe public HTTP(S) URL")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve download host: {host}") from exc
    resolved = {entry[4][0].split("%", 1)[0] for entry in addresses}
    if not resolved or any(not ipaddress.ip_address(address).is_global for address in resolved):
        raise ValueError("Download host resolves to a private or special-use address")


async def _zot_call(func, /, *args, **kwargs):
    """Run a synchronous Pyzotero operation outside the async event loop."""

    return await asyncio.to_thread(partial(func, *args, **kwargs))


def _validate_limit(limit: int, *, maximum: int = 100) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return limit


async def _download_file_from_url(
    url: str,
    *,
    suffix: str = "",
    max_bytes: int = MAX_ATTACHMENT_BYTES,
    require_pdf: bool = False,
) -> tuple[str, str]:
    """Download a public URL with DNS/redirect SSRF checks and a hard size limit."""

    tmp_path: str | None = None
    current = url
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            for redirect_count in range(MAX_REDIRECTS + 1):
                await _validate_public_url(current)
                async with client.stream("GET", current) as resp:
                    if resp.status_code in {301, 302, 303, 307, 308}:
                        location = resp.headers.get("location")
                        if not location:
                            raise ValueError("Download redirect omitted a Location header")
                        if redirect_count >= MAX_REDIRECTS:
                            raise ValueError("Download exceeded the redirect limit")
                        current = urljoin(current, location)
                        continue

                    resp.raise_for_status()
                    content_length = resp.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise ValueError(f"Download exceeds the {max_bytes} byte limit")

                    content_type = resp.headers.get("content-type", "").split(";", 1)[0]
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as output:
                        tmp_path = output.name
                        size = 0
                        async for chunk in resp.aiter_bytes(64 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                raise ValueError(f"Download exceeds the {max_bytes} byte limit")
                            output.write(chunk)

                    with open(tmp_path, "rb") as input_file:
                        header = input_file.read(5)
                    if require_pdf and header != b"%PDF-":
                        raise ValueError("Downloaded content is not a valid PDF")
                    return tmp_path, content_type
        raise ValueError("Download did not produce a response")
    except Exception:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        raise


def _get_zot() -> zotero.Zotero:
    """Create a Pyzotero client from environment config."""
    library_id = os.environ.get("ZOTERO_LIBRARY_ID", ZOTERO_LIBRARY_ID)
    api_key = os.environ.get("ZOTERO_API_KEY", ZOTERO_API_KEY)
    library_type = os.environ.get("ZOTERO_LIBRARY_TYPE", ZOTERO_LIBRARY_TYPE)
    if not library_id or not api_key:
        raise ValueError(
            "ZOTERO_LIBRARY_ID and ZOTERO_API_KEY environment variables must be set. "
            "Get your API key at https://www.zotero.org/settings/keys"
        )
    if library_type not in {"user", "group"}:
        raise ValueError("ZOTERO_LIBRARY_TYPE must be 'user' or 'group'")
    return zotero.Zotero(library_id, library_type, api_key)


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

    item: dict[str, Any] = {
        "itemType": item_type,
        "title": title,
        "creators": creators,
        "abstractNote": abstract,
        "date": date_str,
        "DOI": msg.get("DOI", ""),
        "url": msg.get("URL", ""),
        "language": msg.get("language", ""),
    }

    container_title = (msg.get("container-title") or [""])[0]
    issn = (msg.get("ISSN") or [""])[0]
    isbn = (msg.get("ISBN") or [""])[0]
    publisher = msg.get("publisher", "")
    pages = msg.get("page", "")

    # Zotero item types accept different field names. Sending journal-only
    # fields on books, reports, or theses causes rejected or silently dropped
    # metadata, so map only fields valid for the selected type.
    if item_type == "journalArticle":
        item.update(
            publicationTitle=container_title,
            volume=msg.get("volume", ""),
            issue=msg.get("issue", ""),
            pages=pages,
            ISSN=issn,
        )
    elif item_type == "conferencePaper":
        item.update(
            proceedingsTitle=container_title,
            pages=pages,
            publisher=publisher,
            ISBN=isbn,
        )
    elif item_type == "bookSection":
        item.update(bookTitle=container_title, pages=pages, publisher=publisher, ISBN=isbn)
    elif item_type == "book":
        item.update(publisher=publisher, ISBN=isbn)
    elif item_type == "report":
        item.update(institution=publisher, reportNumber=msg.get("number", ""))
    elif item_type == "thesis":
        item.update(university=publisher)
    elif item_type == "preprint":
        item.update(repository=container_title or publisher)

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
    email = os.environ.get("UNPAYWALL_EMAIL", UNPAYWALL_EMAIL).strip()
    if not email:
        return None
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
    return all(_webdav_config())


def _webdav_config() -> tuple[str, str, str]:
    raw_url = os.environ.get("ZOTERO_WEBDAV_URL", "").rstrip("/")
    url = f"{raw_url}/zotero" if raw_url else ZOTERO_WEBDAV_URL
    user = os.environ.get("ZOTERO_WEBDAV_USER", ZOTERO_WEBDAV_USER)
    password = os.environ.get("ZOTERO_WEBDAV_PASSWORD", ZOTERO_WEBDAV_PASSWORD)
    return url, user, password


def _hash_file(path: str) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as file_obj:
        while chunk := file_obj.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def _file_chunks(path: str, chunk_size: int = 64 * 1024):
    file_obj = await asyncio.to_thread(open, path, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(file_obj.read, chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        await asyncio.to_thread(file_obj.close)


async def _delete_orphan_attachment(zot: zotero.Zotero, attachment_key: str) -> None:
    try:
        attachment = await _zot_call(zot.item, attachment_key)
        await _zot_call(zot.delete_item, attachment)
    except Exception:
        pass


async def _attach_file_webdav(zot: zotero.Zotero, parent_key: str, file_path: str) -> str | None:
    """Create an attachment item and upload the file to WebDAV. Returns attachment key or None."""
    att_key: str | None = None
    zip_path: str | None = None
    try:
        if os.path.getsize(file_path) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachment exceeds the configured size limit")
        template = await _zot_call(zot.item_template, "attachment", "imported_file")
        template["title"] = os.path.basename(file_path)
        template["filename"] = os.path.basename(file_path)
        template["contentType"] = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        result = await _zot_call(zot.create_items, [template], parentid=parent_key)
        if not result.get("successful"):
            return None
        created = list(result["successful"].values())[0]
        att_key = created["key"]

        md5 = await asyncio.to_thread(_hash_file, file_path)
        mtime = int(os.path.getmtime(file_path) * 1000)

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            zip_path = tmp.name
        await asyncio.to_thread(
            _write_webdav_zip,
            zip_path,
            file_path,
            os.path.basename(file_path),
        )
        if os.path.getsize(zip_path) > MAX_WEBDAV_ZIP_BYTES:
            raise ValueError("Compressed attachment exceeds the WebDAV upload limit")

        prop_xml = (
            f'<properties version="1">'
            f'<mtime>{mtime}</mtime>'
            f'<hash>{md5}</hash>'
            f'</properties>'
        )

        webdav_url, webdav_user, webdav_password = _webdav_config()
        auth = (webdav_user, webdav_password)
        async with httpx.AsyncClient(timeout=60, auth=auth) as client:
            r1 = await client.put(
                f"{webdav_url}/{att_key}.zip",
                content=_file_chunks(zip_path),
                headers={
                    "Content-Type": "application/zip",
                    "Content-Length": str(os.path.getsize(zip_path)),
                },
            )
            r1.raise_for_status()
            r2 = await client.put(
                f"{webdav_url}/{att_key}.prop",
                content=prop_xml.encode(),
                headers={"Content-Type": "text/xml"},
            )
            r2.raise_for_status()

        att_item = await _zot_call(zot.item, att_key)
        att_data = att_item["data"]
        att_data["md5"] = md5
        att_data["mtime"] = mtime
        await _zot_call(zot.update_item, att_data)

        return att_key
    except Exception:
        if att_key:
            await _delete_orphan_attachment(zot, att_key)
        return None
    finally:
        if zip_path:
            Path(zip_path).unlink(missing_ok=True)


def _write_webdav_zip(zip_path: str, file_path: str, archive_name: str) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.write(file_path, archive_name)


async def _attach_file_local(zot: zotero.Zotero, parent_key: str, file_path: str) -> str | None:
    """Attach a file using Zotero's built-in file storage. Returns parent key or None."""
    try:
        if os.path.getsize(file_path) > MAX_ATTACHMENT_BYTES:
            return None
        await _zot_call(zot.attachment_simple, [file_path], parent_key)
        return parent_key
    except Exception:
        return None


async def _attach_pdf_from_url(zot: zotero.Zotero, parent_key: str, url: str) -> str | None:
    """Download a PDF from a URL and attach it to a Zotero item. Returns attachment key or None."""
    tmp_path = None
    try:
        tmp_path, _ = await _download_file_from_url(
            url,
            suffix=".pdf",
            max_bytes=MAX_PDF_BYTES,
            require_pdf=True,
        )
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


async def _download_pdf(
    zot: zotero.Zotero,
    item_key: str,
    attachment_key: str | None = None,
) -> tuple[str, str]:
    """Download the PDF attachment for an item to a temp file.

    Returns (tmp_path, attachment_key). Caller must delete tmp_path.
    Raises ValueError if no PDF attachment found.
    """
    children = await _zot_call(zot.children, item_key)
    att_key = attachment_key
    matching_keys = []
    for child in children:
        data = child.get("data", {})
        if data.get("itemType") == "attachment" and data.get("contentType") == "application/pdf":
            key = data.get("key")
            if key:
                matching_keys.append(key)
    if att_key and att_key not in matching_keys:
        raise ValueError(f"PDF attachment {att_key} is not a child of item {item_key}")
    if not att_key and matching_keys:
        att_key = matching_keys[0]
    if not att_key:
        raise ValueError(f"No PDF attachment found for item {item_key}")

    if _use_webdav():
        webdav_url, webdav_user, webdav_password = _webdav_config()
        auth = (webdav_user, webdav_password)
        zip_path = None
        pdf_path = None
        try:
            async with httpx.AsyncClient(timeout=60, auth=auth) as client:
                async with client.stream("GET", f"{webdav_url}/{att_key}.zip") as resp:
                    resp.raise_for_status()
                    content_length = resp.headers.get("content-length")
                    if content_length and int(content_length) > MAX_WEBDAV_ZIP_BYTES:
                        raise ValueError("WebDAV ZIP exceeds the download limit")
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        zip_path = tmp.name
                        downloaded = 0
                        async for chunk in resp.aiter_bytes(64 * 1024):
                            downloaded += len(chunk)
                            if downloaded > MAX_WEBDAV_ZIP_BYTES:
                                raise ValueError("WebDAV ZIP exceeds the download limit")
                            tmp.write(chunk)
            with zipfile.ZipFile(zip_path, "r") as zf:
                pdf_infos = [info for info in zf.infolist() if info.filename.lower().endswith(".pdf")]
                if not pdf_infos:
                    raise ValueError("No PDF found in WebDAV zip")
                info = pdf_infos[0]
                if info.file_size > MAX_PDF_BYTES:
                    raise ValueError("PDF in WebDAV ZIP exceeds the size limit")
                if info.compress_size == 0 and info.file_size:
                    raise ValueError("Suspicious WebDAV ZIP compression ratio")
                if info.compress_size and info.file_size / info.compress_size > 200:
                    raise ValueError("Suspicious WebDAV ZIP compression ratio")
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as output:
                    pdf_path = output.name
                    with zf.open(info) as source:
                        copied = 0
                        while chunk := source.read(64 * 1024):
                            copied += len(chunk)
                            if copied > MAX_PDF_BYTES:
                                raise ValueError("Extracted PDF exceeds the size limit")
                            output.write(chunk)
            with open(pdf_path, "rb") as input_file:
                if input_file.read(5) != b"%PDF-":
                    raise ValueError("WebDAV attachment is not a valid PDF")
            return pdf_path, att_key
        except Exception:
            if pdf_path:
                Path(pdf_path).unlink(missing_ok=True)
            raise
        finally:
            if zip_path:
                Path(zip_path).unlink(missing_ok=True)
    else:
        file_data = await _zot_call(zot.file, att_key)
        if len(file_data) > MAX_PDF_BYTES:
            raise ValueError("PDF attachment exceeds the size limit")
        if file_data[:5] != b"%PDF-":
            raise ValueError("Attachment content is not a valid PDF")
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

        async def _resolve_author(author_ref: dict[str, Any]) -> dict[str, Any] | None:
            key = author_ref.get("key", "")
            if key and re.fullmatch(r"/authors/OL\d+A", key):
                try:
                    author_resp = await client.get(f"{OPENLIBRARY_API}{key}.json")
                    author_resp.raise_for_status()
                    return author_resp.json()
                except Exception:
                    return None
            return None

        resolved = await asyncio.gather(
            *(_resolve_author(author_ref) for author_ref in data.get("authors", []))
        )
        data["_resolved_authors"] = [author for author in resolved if author]

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
