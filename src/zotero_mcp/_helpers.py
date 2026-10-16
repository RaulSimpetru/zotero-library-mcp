"""Shared helpers, configuration, and metadata resolvers."""

import asyncio
import hashlib
import ipaddress
import mimetypes
import os
import re
import shutil
import socket
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Awaitable, Callable
from functools import lru_cache, partial
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse, urlsplit

import httpx
from pyzotero import _client as pyzotero_client
from pyzotero import _utils as pyzotero_utils
from pyzotero import zotero
from pyzotero import zotero_errors

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
CROSSREF_API = "https://api.crossref.org/works"
MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB limit for PDF downloads
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_WEBDAV_ZIP_BYTES = 110 * 1024 * 1024
MAX_REDIRECTS = 5
PDF_DOWNLOAD_TIMEOUT_SECONDS = 60.0

DownloadProgress = Callable[[float, str], Awaitable[None]]

UNPAYWALL_API = "https://api.unpaywall.org/v2"

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

OPENLIBRARY_ISBN_API = "https://openlibrary.org/isbn"
OPENLIBRARY_API = "https://openlibrary.org"


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


def _is_public_ip(address: str) -> bool:
    """True for globally routable addresses, including IPv6 forms that embed IPv4."""

    ip = ipaddress.ip_address(address)
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # ::ffff:a.b.c.d, 6to4, Teredo, NAT64 and IPv4-compatible addresses
        # can route to the embedded IPv4 address, which must be public too.
        embedded = [ip.ipv4_mapped, ip.sixtofour, ip.teredo[1] if ip.teredo else None]
        if ip in _NAT64 or ip in _IPV4_COMPATIBLE:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if any(inner is not None and not inner.is_global for inner in embedded):
            return False
    return True


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
        return _is_public_ip(host)
    except ValueError:
        if host in ("localhost", "") or host.endswith(".local"):
            return False
    return True


async def _validate_public_url(url: str) -> str:
    """Reject URLs whose hostname resolves to a private or special-use address.

    Returns one validated IP address. Callers must connect to that address:
    resolving the name again would allow DNS rebinding to a private address.
    """

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
    resolved = [entry[4][0].split("%", 1)[0] for entry in addresses]
    if not resolved or not all(_is_public_ip(address) for address in resolved):
        raise ValueError("Download host resolves to a private or special-use address")
    return resolved[0]


def _pinned_request(url: str, ip: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """URL aimed at ``ip`` plus the Host header and TLS name of the original URL."""

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    bracket = lambda value: f"[{value}]" if ":" in value else value
    pinned = parsed._replace(netloc=f"{bracket(ip)}{port}").geturl()
    return pinned, {"Host": f"{bracket(host)}{port}"}, {"sni_hostname": host}


MAX_RATE_LIMIT_RETRIES = 3
MAX_BACKOFF_SECONDS = 60.0
# Zotero's Backoff/Retry-After applies to the API key, but Pyzotero tracks it per
# client instance. Every call here creates its own instance, so share it here.
_backoff_until = 0.0


def _requested_delay(owner) -> float | None:
    """Seconds Zotero asked for in ``owner``'s last response headers, if any."""

    headers = getattr(getattr(owner, "request", None), "headers", None)
    if headers is None:
        return None
    value = headers.get("backoff") or headers.get("retry-after")
    try:
        return min(float(value), MAX_BACKOFF_SECONDS) if isinstance(value, str) else None
    except ValueError:
        return None


# Only reads are retried. Writes are not safe to repeat: create_items with a
# parent POSTs and then PATCHes each item, so a 429 on a PATCH would re-run the
# whole creation and duplicate it.
_RETRYABLE_READS = frozenset({
    "children", "collection", "collection_items", "collection_items_top",
    "collections", "deleted", "fulltext_item", "groups", "item", "item_template",
    "items", "key_info", "settings", "tags", "top", "trash",
})


async def _zot_call(func, /, *args, **kwargs):
    """Run a synchronous Pyzotero operation outside the event loop, honoring rate limits.

    On a 429 that carries a delay, Pyzotero does not raise (reads parse the
    error body as data; writes return True). Detect it from the instance's
    response for this call, retry reads, and raise for writes. Lambdas have no
    instance to inspect and pass through unchecked.
    """

    global _backoff_until
    target = func.func if isinstance(func, partial) else func
    owner = getattr(target, "__self__", None)
    attempts = MAX_RATE_LIMIT_RETRIES + 1 if getattr(target, "__name__", "") in _RETRYABLE_READS else 1
    error: Exception | None = None
    for attempt in range(attempts):
        wait = _backoff_until - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        before = getattr(owner, "request", None)
        if owner is not None and hasattr(owner, "url_params"):
            # Pyzotero merges leftover query parameters into the next request:
            # after item_template("book") a children() call silently filters to
            # itemType=book, and failed requests leave their parameters behind.
            owner.url_params = None
        error = None
        result = None
        try:
            result = await asyncio.to_thread(partial(func, *args, **kwargs))
        except Exception as exc:
            error = exc
        # Only trust a response this call produced, not one left from an earlier call.
        response = getattr(owner, "request", None)
        fresh = response is not None and response is not before
        delay = _requested_delay(owner) if fresh else None
        rate_limited = (
            fresh and getattr(response, "status_code", None) == 429
        ) or isinstance(error, zotero_errors.TooManyRetriesError)
        if rate_limited and delay is None:
            delay = float(2**attempt)
        if delay:
            _backoff_until = max(_backoff_until, time.monotonic() + delay)
        if not rate_limited:
            if error is not None:
                raise error
            return result
    raise error or zotero_errors.TooManyRetriesError(
        "Zotero is rate-limiting requests; try again shortly"
    )


def _validate_limit(limit: int, *, maximum: int = 100) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return limit


def _validate_start(start: int) -> int:
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        raise ValueError("start must be a non-negative integer")
    return start


def _total_results(zot, fallback: int) -> int:
    """Total-Results header from the client's last request, or fallback."""
    try:
        value = zot.request.headers.get("Total-Results")
    except AttributeError:
        return fallback
    return int(value) if isinstance(value, str) and value.isdigit() else fallback


def _page_footer(start: int, shown: int, total: int) -> str:
    if shown:
        footer = f"\n\nShowing items {start + 1}–{start + shown} of {total}."
    else:
        footer = f"\n\nNo items at start={start}; {total} total."
    if start + shown < total:
        footer += f" Pass start={start + shown} to get the next page."
    return footer


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
        # trust_env=False: a configured proxy would resolve names itself and
        # bypass the address check.
        async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as client:
            for redirect_count in range(MAX_REDIRECTS + 1):
                ip = await _validate_public_url(current)
                pinned_url, headers, extensions = _pinned_request(current, ip)
                async with client.stream(
                    "GET", pinned_url, headers=headers, extensions=extensions
                ) as resp:
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


# Pyzotero uses the httpx2 fork, not httpx. The shared client must be the same
# library, or Pyzotero's ``except httpx2.HTTPError`` handlers (error mapping,
# rate-limit backoff) never see its errors.
class _SharedHTTPClient(pyzotero_client.httpx2.Client):
    """Connection pool reused by every per-call Pyzotero client."""

    def close(self) -> None:
        # Pyzotero closes its client in ``__del__``; this pool lives for the
        # whole process, so ignore those per-instance closes.
        pass


@lru_cache(maxsize=4)
def _shared_http_client(api_key: str):
    # Same settings Pyzotero uses for the clients it creates itself; without
    # the timeout, the library default (5 s) cuts off slow full-text searches.
    return _SharedHTTPClient(
        headers={
            "User-Agent": "zotero-library-mcp",
            "Zotero-API-Version": "3",
            "Authorization": f"Bearer {api_key}",
        },
        follow_redirects=True,
        timeout=pyzotero_utils.DEFAULT_TIMEOUT,
    )


class _Zotero(zotero.Zotero):
    def _tags_data(self, retrieved):
        # Pyzotero treats any response whose URL contains "tags" (query string
        # included, e.g. ?q=tags or ?tag=hashtags) as a tag list and crashes on
        # item results. Only real /tags endpoints return tag lists.
        if "/tags" not in self.request.url.path:
            return retrieved
        return super()._tags_data(retrieved)


def _get_zot(
    library_id: str | None = None,
    library_type: str | None = None,
) -> zotero.Zotero:
    """Create a client for this call without changing the configured default."""
    if (library_id is None) != (library_type is None):
        raise ValueError("Pass both library_id and library_type, or omit both")
    if library_id is None:
        library_id = os.environ.get("ZOTERO_LIBRARY_ID", "")
        # ``or``: plugin installs pass unset optional settings as "".
        library_type = os.environ.get("ZOTERO_LIBRARY_TYPE") or "user"
    api_key = os.environ.get("ZOTERO_API_KEY", "")
    if not library_id or not api_key:
        raise ValueError(
            "ZOTERO_LIBRARY_ID and ZOTERO_API_KEY environment variables must be set. "
            "Get your API key at https://www.zotero.org/settings/keys"
        )
    if library_type not in {"user", "group"}:
        raise ValueError("library_type must be 'user' or 'group'")
    if not isinstance(library_id, str) or not re.fullmatch(r"[1-9][0-9]*", library_id):
        raise ValueError("library_id must be a positive numeric ID")
    # A fresh Pyzotero instance per call keeps its per-request state (query
    # params, pagination links, last response) private, while the shared
    # HTTP client reuses TLS connections across calls.
    return _Zotero(
        library_id, library_type, api_key, client=_shared_http_client(api_key)
    )


async def _fetch_all(method, *args, **kwargs) -> list:
    """Every page of a list endpoint, each request rate-limit checked.

    Pyzotero's own ``everything()`` pages internally, out of reach of
    ``_zot_call``'s 429 detection and retries.
    """

    results: list = []
    while True:
        page = await _zot_call(method, *args, limit=100, start=len(results), **kwargs)
        results.extend(page)
        if len(page) < 100:
            return results


async def _all_children(zot, item_key: str) -> list[dict]:
    """Every child of an item; Pyzotero's default page is only 100."""

    return await _fetch_all(zot.children, item_key)


def _patch_item(zot, item_key: str, version: int, changes: dict[str, object]):
    """Optimistic-concurrency partial update of only the changed fields.

    Unlike update_item, this works for trashed items (Pyzotero rejects their
    ``deleted`` field) and skips Pyzotero's extra field-validation requests.
    """

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


def _fmt_item(data: dict[str, Any]) -> str:
    """Format a Zotero item as a compact one-liner."""
    key = data.get("key", "?")
    title = data.get("title", "Untitled")
    creators = data.get("creators", [])
    names = [c.get("lastName") or c.get("name", "") for c in creators[:3]]
    author = ", ".join(n for n in names if n)
    if len(creators) > 3:
        author += " et al."
    year_match = re.search(r"\b\d{4}\b", data.get("date", ""))
    year = year_match.group(0) if year_match else ""
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
    for role in ("author", "editor"):
        for person in msg.get(role, []):
            if person.get("family"):
                creators.append({
                    "creatorType": role,
                    "firstName": person.get("given", ""),
                    "lastName": person["family"],
                })
            elif person.get("name"):
                # Organizations ("LIGO Scientific Collaboration") have only a name.
                creators.append({"creatorType": role, "name": person["name"]})

    date_parts = None
    for field in ("published-print", "published-online", "issued", "created"):
        parts = (msg.get(field) or {}).get("date-parts") or [[]]
        # CrossRef sends [[null]] or [[]] for unknown dates; try the next field.
        if parts[0] and parts[0][0]:
            date_parts = parts[0]
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
        "book-section": "bookSection",
        "book-part": "bookSection",
        "reference-entry": "bookSection",
        "book": "book",
        "monograph": "book",
        "edited-book": "book",
        "reference-book": "book",
        "posted-content": "preprint",
        "report": "report",
        "dissertation": "thesis",
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
        institution = (msg.get("institution") or [{}])[0].get("name", "")
        item.update(university=institution or publisher)
    elif item_type == "preprint":
        item.update(repository=container_title or publisher)

    return item


async def _resolve_doi(doi: str) -> dict[str, Any]:
    """Fetch metadata for a DOI from the CrossRef API."""
    headers = {"Accept": "application/json"}
    # A contact address puts requests in CrossRef's "polite" pool, which has
    # far higher rate limits than anonymous traffic.
    mailto = (os.environ.get("CROSSREF_MAILTO") or _unpaywall_email()).replace("\r", "").replace("\n", "")
    if mailto:
        headers["User-Agent"] = f"zotero-library-mcp (mailto:{mailto})"

    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(3):
            resp = await client.get(f"{CROSSREF_API}/{quote(doi, safe='')}", headers=headers)
            if resp.status_code != 429 or attempt == 2:
                break
            retry_after = resp.headers.get("retry-after", "")
            await asyncio.sleep(min(float(retry_after), 10.0) if retry_after.isdigit() else 2.0 ** attempt)
        resp.raise_for_status()
        return resp.json()


def _unpaywall_email() -> str:
    return (os.environ.get("UNPAYWALL_EMAIL") or os.environ.get("CROSSREF_MAILTO", "")).strip()


async def _resolve_doi_csl(doi: str) -> dict[str, Any]:
    """CSL-JSON for any registered DOI (CrossRef, DataCite, ...) via doi.org."""

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(
            f"https://doi.org/{quote(doi, safe='/')}",
            headers={"Accept": "application/vnd.citationstyles.csl+json"},
        )
        resp.raise_for_status()
        return resp.json()


async def _find_open_access_pdf(doi: str) -> str | None:
    """Query Unpaywall for an open-access PDF URL. Returns URL or None."""
    email = _unpaywall_email()
    if not email:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{UNPAYWALL_API}/{quote(doi, safe='')}", params={"email": email}
            )
            resp.raise_for_status()
            data = resp.json()
        # ``url`` is often a landing page, which would only fail the PDF check.
        for location in [data.get("best_oa_location"), *(data.get("oa_locations") or [])]:
            if location and location.get("url_for_pdf"):
                return location["url_for_pdf"]
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# File attachment helpers (WebDAV / local / URL)
# ---------------------------------------------------------------------------

def _use_webdav(zot: zotero.Zotero) -> bool:
    """WebDAV belongs to the configured personal library, never a group."""
    if str(zot.library_type).rstrip("s") != "user":
        return False
    if str(zot.library_id) != os.environ.get("ZOTERO_LIBRARY_ID", ""):
        return False
    if (os.environ.get("ZOTERO_LIBRARY_TYPE") or "user") != "user":
        return False
    return all(_webdav_config())


def _webdav_config() -> tuple[str, str, str]:
    raw_url = os.environ.get("ZOTERO_WEBDAV_URL", "").rstrip("/")
    url = f"{raw_url}/zotero" if raw_url else ""
    user = os.environ.get("ZOTERO_WEBDAV_USER", "")
    password = os.environ.get("ZOTERO_WEBDAV_PASSWORD", "")
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


async def _attach_file_webdav(zot: zotero.Zotero, parent_key: str, file_path: str) -> str:
    """Create an attachment item and upload the file to WebDAV; return its key or raise."""
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
            raise ValueError(f"Zotero rejected the attachment: {result.get('failed', result)}")
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
        raise
    finally:
        if zip_path:
            Path(zip_path).unlink(missing_ok=True)


def _write_webdav_zip(zip_path: str, file_path: str, archive_name: str) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.write(file_path, archive_name)


async def _attach_file_local(zot: zotero.Zotero, parent_key: str, file_path: str) -> str:
    """Attach a file using Zotero's built-in file storage; return the parent key or raise."""
    if os.path.getsize(file_path) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Attachment exceeds the configured size limit")
    before = {child.get("key") for child in await _all_children(zot, parent_key)}
    filename = os.path.basename(file_path)

    async def _new_attachments() -> list[dict]:
        return [
            child for child in await _all_children(zot, parent_key)
            if child.get("key") not in before and child.get("data", {}).get("filename") == filename
        ]

    try:
        try:
            # Not attachment_simple: it sends the full local path as the
            # filename, which Zotero rejects ("cannot contain a directory path").
            template = await _zot_call(zot.item_template, "attachment", "imported_file")
            template.update(title=filename, filename=filename)
            result = await _zot_call(
                zot.upload_attachments, [template], parent_key, os.path.dirname(file_path)
            )
        except Exception as exc:
            if getattr(zot, "backoff_until", 0) > time.time():
                raise ValueError("Zotero rate-limited the upload; try again shortly") from exc
            raise
        if isinstance(result, dict) and result.get("failure"):
            raise ValueError(f"Zotero rejected the attachment: {result['failure']}")
        if not (isinstance(result, dict) and result.get("unchanged")):
            # Pyzotero ignores a 429 on the upload steps and still reports
            # success. Zotero sets md5 only once the file is registered.
            created = await _new_attachments()
            if not created or not all(c.get("data", {}).get("md5") for c in created):
                raise ValueError(
                    "Zotero did not confirm the file upload (possibly rate-limited); try again"
                )
    except Exception:
        # Pyzotero creates the attachment item before uploading the file, so a
        # failed upload (e.g. storage quota exceeded) leaves an empty item.
        for child in await _new_attachments():
            await _delete_orphan_attachment(zot, child["key"])
        raise
    return parent_key


def _pdf_filename(title: str) -> str:
    """Turn an item title into a portable attachment filename."""
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title)
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:120]
    return f"{stem or 'paper'}.pdf"


async def _attach_pdf_from_url(
    zot: zotero.Zotero, parent_key: str, url: str, title: str = ""
) -> str:
    """Download a PDF and attach it to an item under a title-based filename; raise on failure."""
    tmp_path, _ = await _download_file_from_url(
        url,
        suffix=".pdf",
        max_bytes=MAX_PDF_BYTES,
        require_pdf=True,
    )
    temp_dir = tempfile.mkdtemp(prefix="zotero_pdf_")
    try:
        # The attachment's title and filename come from the basename, so never
        # upload under the random temp name.
        named_path = os.path.join(temp_dir, _pdf_filename(title))
        os.replace(tmp_path, named_path)
        if _use_webdav(zot):
            return await _attach_file_webdav(zot, parent_key, named_path)
        return await _attach_file_local(zot, parent_key, named_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        shutil.rmtree(temp_dir, ignore_errors=True)


async def _download_pdf(
    zot: zotero.Zotero,
    item_key: str,
    attachment_key: str | None = None,
    progress: DownloadProgress | None = None,
) -> tuple[str, str]:
    """Download the PDF attachment for an item to a temp file.

    Returns (tmp_path, attachment_key). Caller must delete tmp_path.
    Raises ValueError if no PDF attachment found.
    """
    async def report(value: float, message: str) -> None:
        if progress is not None:
            await progress(value, message)

    await report(2, "Resolving the PDF attachment")
    item = await _zot_call(zot.item, item_key)
    if item.get("data", {}).get("itemType") == "attachment":
        # A standalone PDF (no parent item) is its own attachment.
        candidates = [item]
    else:
        candidates = await _all_children(zot, item_key)
    att_key = attachment_key
    matching_keys = []
    linked_keys = []
    for child in candidates:
        data = child.get("data", {})
        if data.get("itemType") == "attachment" and data.get("contentType") == "application/pdf":
            key = data.get("key")
            if not key:
                continue
            # Linked files/URLs live outside Zotero storage and WebDAV, so
            # neither can serve them.
            if data.get("linkMode") in ("linked_file", "linked_url"):
                linked_keys.append(key)
            else:
                matching_keys.append(key)
    if att_key and att_key in linked_keys:
        raise ValueError(
            f"PDF attachment {att_key} is a linked file, which is not stored in Zotero"
        )
    if att_key and att_key not in matching_keys:
        raise ValueError(f"PDF attachment {att_key} is not a child of item {item_key}")
    if not att_key and matching_keys:
        att_key = matching_keys[0]
    if not att_key:
        if linked_keys:
            raise ValueError(
                f"Item {item_key} only has linked PDF files, which are not stored in Zotero"
            )
        raise ValueError(f"No PDF attachment found for item {item_key}")

    if _use_webdav(zot):
        webdav_url, webdav_user, webdav_password = _webdav_config()
        auth = (webdav_user, webdav_password)
        zip_path = None
        pdf_path = None
        try:
            await report(5, "Connecting to WebDAV")
            timeout = httpx.Timeout(
                PDF_DOWNLOAD_TIMEOUT_SECONDS,
                connect=min(15.0, PDF_DOWNLOAD_TIMEOUT_SECONDS),
            )
            async with httpx.AsyncClient(timeout=timeout, auth=auth) as client:
                async with client.stream("GET", f"{webdav_url}/{att_key}.zip") as resp:
                    resp.raise_for_status()
                    content_length = resp.headers.get("content-length")
                    expected_bytes = int(content_length) if content_length else None
                    if expected_bytes and expected_bytes > MAX_WEBDAV_ZIP_BYTES:
                        raise ValueError("WebDAV ZIP exceeds the download limit")
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        zip_path = tmp.name
                        downloaded = 0
                        last_progress = 5.0
                        last_reported_bytes = 0
                        async for chunk in resp.aiter_bytes(64 * 1024):
                            downloaded += len(chunk)
                            if downloaded > MAX_WEBDAV_ZIP_BYTES:
                                raise ValueError("WebDAV ZIP exceeds the download limit")
                            tmp.write(chunk)
                            if expected_bytes:
                                current = min(75.0, 5.0 + (downloaded / expected_bytes) * 70.0)
                                if current - last_progress >= 5:
                                    last_progress = current
                                    await report(current, "Downloading the PDF from WebDAV")
                            elif downloaded - last_reported_bytes >= 5 * 1024 * 1024:
                                last_reported_bytes = downloaded
                                await report(
                                    40,
                                    f"Downloaded {downloaded / (1024 * 1024):.0f} MB from WebDAV",
                                )
            await report(78, "Validating the WebDAV archive")
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
                        last_progress = 78.0
                        while chunk := source.read(64 * 1024):
                            copied += len(chunk)
                            if copied > MAX_PDF_BYTES:
                                raise ValueError("Extracted PDF exceeds the size limit")
                            output.write(chunk)
                            if info.file_size:
                                current = min(96.0, 78.0 + (copied / info.file_size) * 18.0)
                                if current - last_progress >= 5:
                                    last_progress = current
                                    await report(current, "Extracting the PDF")
            with open(pdf_path, "rb") as input_file:
                if input_file.read(5) != b"%PDF-":
                    raise ValueError("WebDAV attachment is not a valid PDF")
            await report(98, "PDF downloaded and validated")
            return pdf_path, att_key
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"WebDAV PDF download timed out after "
                f"{PDF_DOWNLOAD_TIMEOUT_SECONDS:.0f} seconds; retry or check the WebDAV connection"
            ) from exc
        except Exception:
            if pdf_path:
                Path(pdf_path).unlink(missing_ok=True)
            raise
        finally:
            if zip_path:
                Path(zip_path).unlink(missing_ok=True)
    else:
        await report(10, "Downloading the PDF from Zotero storage")
        file_data = await _zot_call(zot.file, att_key)
        if len(file_data) > MAX_PDF_BYTES:
            raise ValueError("PDF attachment exceeds the size limit")
        if file_data[:5] != b"%PDF-":
            raise ValueError("Attachment content is not a valid PDF")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(file_data)
            await report(98, "PDF downloaded and validated")
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

        author_refs = data.get("authors") or []
        if not author_refs and data.get("works"):
            # Many edition records list authors only on the work record.
            work_key = data["works"][0].get("key", "")
            if re.fullmatch(r"/works/OL\d+W", work_key):
                try:
                    work_resp = await client.get(f"{OPENLIBRARY_API}{work_key}.json")
                    work_resp.raise_for_status()
                    author_refs = [
                        entry["author"]
                        for entry in work_resp.json().get("authors", [])
                        if isinstance(entry.get("author"), dict)
                    ]
                except Exception:
                    author_refs = []
        resolved = await asyncio.gather(
            *(_resolve_author(author_ref) for author_ref in author_refs)
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
