"""Regression tests for the 0.10.1 fixes."""

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pymupdf as fitz

import zotero_mcp.annotations as annotations_mod
import zotero_mcp.tags as tags_mod
from zotero_mcp._helpers import _fmt_item


def _register(module):
    registered = {}
    mcp = MagicMock()
    mcp.tool = lambda **_kwargs: lambda fn: registered.setdefault(fn.__name__, fn)
    module.register(mcp)
    return registered


def _annotate(monkeypatch, tmp_path, pages, **kwargs):
    pdf_path = tmp_path / "paper.pdf"
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        for index, line in enumerate(lines):
            page.insert_text((72, 100 + 15 * index), line)
    doc.save(pdf_path)

    zot = MagicMock()
    zot.children.return_value = []
    zot.create_items.return_value = {"successful": {"0": {"key": "ANN00001"}}}

    async def fake_download(*_args, **_kwargs):
        return str(pdf_path), "ATT00001"

    monkeypatch.setattr(annotations_mod, "_get_zot", lambda *_args: zot)
    monkeypatch.setattr(annotations_mod, "_download_pdf", fake_download)
    asyncio.run(_register(annotations_mod)["create_annotation"](item_key="ITEM0001", **kwargs))
    annotation = zot.create_items.call_args.args[0][0]
    return json.loads(annotation["annotationPosition"])


def test_highlight_covers_every_line_of_a_wrapped_quote(monkeypatch, tmp_path):
    position = _annotate(
        monkeypatch,
        tmp_path,
        [["alpha beta gamma delta", "epsilon zeta eta theta"]],
        quoted_text="gamma delta epsilon zeta",
    )
    # One rect per word across both lines, not just the first line.
    line_tops = {round(rect[3]) for rect in position["rects"]}
    assert len(position["rects"]) == 4
    assert len(line_tops) == 2


def test_occurrence_counts_across_pages(monkeypatch, tmp_path):
    position = _annotate(
        monkeypatch,
        tmp_path,
        [["repeated phrase here"], ["filler"], ["repeated phrase again"]],
        quoted_text="repeated phrase",
        occurrence=2,
    )
    assert position["pageIndex"] == 2


def test_year_is_parsed_from_non_iso_dates():
    assert "(Rousseeuw, 1993)" in _fmt_item(
        {"key": "K", "title": "T", "date": "12/1/1993", "creators": [{"lastName": "Rousseeuw"}]}
    )
    assert "(2026)" in _fmt_item({"key": "K", "title": "T", "date": "2026-03"})


def test_rename_tag_reports_per_item_failures(monkeypatch):
    zot = MagicMock()
    zot.items.return_value = [
        {"data": {"key": f"ITEM000{i}", "tags": [{"tag": "old"}]}} for i in range(2)
    ]
    zot.request.json.return_value = {"failed": {"1": {"message": "version conflict"}}}
    zot.settings.return_value = {}
    monkeypatch.setattr(tags_mod, "_get_zot", lambda *_args: zot)

    result = asyncio.run(_register(tags_mod)["rename_tag"](old_name="old", new_name="new"))
    zot.update_items.assert_called_once()
    assert "on 1 item(s)" in result
    assert "ITEM0001: version conflict" in result


def _register_library():
    import zotero_mcp.library as library_mod

    return library_mod, _register(library_mod)


def test_top_items_pages_use_separate_clients():
    import zotero_mcp.library as library_mod

    first = MagicMock()
    first.top.return_value = [{"data": {"key": f"A{i}"}} for i in range(100)]
    first.request.headers = {"Total-Results": "250"}
    clones = []

    def new_zot():
        clone = MagicMock()
        clone.top.side_effect = lambda limit, start: [
            {"data": {"key": f"B{start + i}"}} for i in range(limit)
        ]
        clones.append(clone)
        return clone

    items = asyncio.run(library_mod._top_items_bounded(first, 1000, new_zot))
    assert len(items) == 250
    assert [clone.top.call_args.kwargs for clone in clones] == [
        {"limit": 100, "start": 100},
        {"limit": 50, "start": 200},
    ]


def test_bibtex_export_stops_once_max_chars_is_exceeded(monkeypatch):
    import bibtexparser

    library_mod, tools = _register_library()
    page = bibtexparser.loads(
        "".join(f"@article{{k{i}, title={{{'x' * 50}}}}}\n" for i in range(100))
    )
    zot = MagicMock()
    zot.top.return_value = page
    zot.request.headers = {"Total-Results": "5000"}
    monkeypatch.setattr(library_mod, "_get_zot", lambda *_args: zot)

    result = asyncio.run(tools["get_bibtex"](max_chars=1000))
    assert result.is_error
    zot.top.assert_called_once()


def test_fulltext_prefers_pdf_index_over_snapshot(monkeypatch):
    library_mod, tools = _register_library()
    zot = MagicMock()
    zot.children.return_value = [
        {"data": {"key": "SNAP0001", "itemType": "attachment", "contentType": "text/html"}},
        {"data": {"key": "PDF00001", "itemType": "attachment", "contentType": "application/pdf"}},
    ]
    zot.fulltext_item.side_effect = lambda key: {"content": f"text of {key}"}
    monkeypatch.setattr(library_mod, "_get_zot", lambda *_args: zot)

    assert asyncio.run(tools["get_item_fulltext"](item_key="ITEM0001")) == "text of PDF00001"


def test_web_library_url_uses_username_slug(monkeypatch):
    library_mod, _ = _register_library()
    monkeypatch.setattr(library_mod, "_web_library_urls", {})
    zot = MagicMock(library_type="users", library_id="42")
    zot.key_info.return_value = {"userID": 42, "username": "Jane Doe"}
    assert asyncio.run(library_mod._web_library_url(zot)) == "https://www.zotero.org/jane_doe"
    group = MagicMock(library_type="groups", library_id="7")
    assert asyncio.run(library_mod._web_library_url(group)) == "https://www.zotero.org/groups/7"


def test_shared_http_client_survives_pyzotero_cleanup(monkeypatch):
    from zotero_mcp import _helpers

    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "1")
    monkeypatch.setenv("ZOTERO_API_KEY", "key")
    first = _helpers._get_zot()
    client = first.client
    del first  # Pyzotero.__del__ calls client.close()
    assert not client.is_closed
    assert _helpers._get_zot().client is client


def test_rate_limited_calls_wait_and_retry(monkeypatch):
    from types import SimpleNamespace

    from zotero_mcp import _helpers

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(_helpers.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(_helpers, "_backoff_until", 0.0)

    class FakeZotero:
        def __init__(self):
            self.calls = 0
            self.request = None

        def top(self):
            self.calls += 1
            if self.calls == 1:
                # Pyzotero returns the 429 body as data instead of raising.
                self.request = SimpleNamespace(status_code=429, headers={"retry-after": "2"})
                return "Too many requests"
            self.request = SimpleNamespace(status_code=200, headers={})
            return ["item"]

    zot = FakeZotero()
    assert asyncio.run(_helpers._zot_call(zot.top)) == ["item"]
    assert zot.calls == 2
    assert len(sleeps) == 1 and 1.5 < sleeps[0] <= 2


def test_unfiled_scan_keeps_order_and_stops_at_short_page(monkeypatch):
    library_mod, tools = _register_library()

    def page(limit, start):
        if start >= 250:
            return []
        filed = start < 100  # first page is all filed
        count = min(limit, 250 - start)
        return [
            {"data": {"key": f"K{start + i:04d}", "title": "t", "collections": ["C"] if filed else []}}
            for i in range(count)
        ]

    zot = MagicMock()
    zot.top.side_effect = page
    monkeypatch.setattr(library_mod, "_get_zot", lambda *_args: zot)

    result = asyncio.run(tools["get_unfiled_items"](limit=3))
    assert "[K0100]" in result and "[K0102]" in result and "[K0099]" not in result
    assert zot.top.call_count == 5  # one batch, no second round


def test_doi_import_detects_duplicate_by_title_then_doi(monkeypatch):
    import zotero_mcp.papers as papers_mod

    existing = {"key": "KTFFXHL5", "title": "Alternatives to the MAD", "DOI": "10.1080/ABC"}
    zot = MagicMock()
    zot.top.return_value = [{"data": {"key": "OTHER001", "DOI": "10.9/x"}}, {"data": existing}]

    async def fake_resolve(doi):
        return {"message": {"title": ["Alternatives to the <i>MAD</i>"], "DOI": doi}}

    monkeypatch.setattr(papers_mod, "_get_zot", lambda *_args: zot)
    monkeypatch.setattr(papers_mod, "_resolve_doi", fake_resolve)
    tools = _register(papers_mod)

    result = asyncio.run(tools["add_paper_by_doi"](doi="https://doi.org/10.1080/abc"))
    assert result.startswith("Duplicate: [KTFFXHL5]")
    # Searched by the resolved, tag-stripped title (DOIs are not indexed).
    assert zot.top.call_args.kwargs["q"] == "Alternatives the MAD"
    zot.create_items.assert_not_called()

    batch = asyncio.run(tools["add_papers_by_dois"](dois=["10.1080/ABC"]))
    assert "No new DOI items were added." in batch and "KTFFXHL5" in batch


def test_doi_in_extra_counts_as_duplicate():
    from zotero_mcp.papers import _has_doi

    assert _has_doi({"extra": "arXiv:2301.1\nDOI: 10.5/XyZ"}, "10.5/xyz")
    assert not _has_doi({"extra": "Note: DOI 10.5/xyz"}, "10.5/xyz")


def test_cached_top_items_syncs_changes_and_deletions():
    from types import SimpleNamespace

    import zotero_mcp.library as library_mod

    library_mod._library_items.clear()
    zot = MagicMock(library_type="users", library_id="1")

    def respond(version, payload):
        def call(**_kwargs):
            zot.request = SimpleNamespace(headers={"Last-Modified-Version": str(version), "Total-Results": "2"})
            return payload
        return call

    item = lambda key, **data: {"data": {"key": key, **data}}
    zot.top.side_effect = respond(10, [item("A"), item("B")])
    first = asyncio.run(library_mod._cached_top_items(zot, lambda: zot))
    assert [i["data"]["key"] for i in first] == ["A", "B"]

    # Since v10: B edited, C added, A trashed, E moved under a parent (it was a
    # standalone note); D permanently deleted elsewhere.
    zot.top.side_effect = respond(10, [item("A"), item("B"), item("E")])
    library_mod._library_items.clear()
    asyncio.run(library_mod._cached_top_items(zot, lambda: zot))
    zot.items.side_effect = respond(
        12, [item("B", title="new"), item("C"), item("A", deleted=1), item("E", parentItem="C")]
    )
    zot.deleted.return_value = {"items": ["D"]}
    second = asyncio.run(library_mod._cached_top_items(zot, lambda: zot))
    assert {i["data"]["key"]: i["data"].get("title") for i in second} == {"B": "new", "C": None}
    assert zot.items.call_args.kwargs == {
        "since": 10, "includeTrashed": 1, "sort": "dateModified", "direction": "asc", "limit": 100, "start": 0,
    }
    assert library_mod._library_items[("users", "1")][0] == 12
    library_mod._library_items.clear()


def _rate_limit_env(monkeypatch):
    from zotero_mcp import _helpers

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(_helpers.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(_helpers, "_backoff_until", 0.0)
    return _helpers


class _Limited:
    """Fake Pyzotero instance whose first N responses are 429s."""

    def __init__(self, limited_calls):
        from types import SimpleNamespace

        self._ns = SimpleNamespace
        self.limited_calls = limited_calls
        self.calls = 0
        self.request = None

    def _respond(self):
        self.calls += 1
        status = 429 if self.calls <= self.limited_calls else 200
        self.request = self._ns(status_code=status, headers={"retry-after": "1"} if status == 429 else {})

    def collection_items_top(self, key, **_kwargs):
        self._respond()
        return ["item"]

    def create_items(self, payload, parentid=None):
        self._respond()
        return {"successful": {"0": {"key": "NEW"}}}


def test_rate_limited_write_raises_without_retrying(monkeypatch):
    import pytest

    helpers = _rate_limit_env(monkeypatch)
    zot = _Limited(limited_calls=1)
    with pytest.raises(helpers.zotero_errors.TooManyRetriesError):
        asyncio.run(helpers._zot_call(zot.create_items, [{}], parentid="P"))
    assert zot.calls == 1  # a retry could duplicate the created item


def test_partial_reads_are_retried(monkeypatch):
    from functools import partial

    helpers = _rate_limit_env(monkeypatch)
    zot = _Limited(limited_calls=2)
    fetch = partial(zot.collection_items_top, "COLL1")
    assert asyncio.run(helpers._zot_call(fetch, limit=100)) == ["item"]
    assert zot.calls == 3


def test_stale_429_from_earlier_call_is_ignored(monkeypatch):
    from types import SimpleNamespace

    helpers = _rate_limit_env(monkeypatch)

    class Cached:
        request = SimpleNamespace(status_code=429, headers={})  # from an earlier call
        calls = 0

        def top(self):
            self.calls += 1  # makes no request, so `request` stays the old 429
            return ["ok"]

    zot = Cached()
    assert asyncio.run(helpers._zot_call(zot.top)) == ["ok"]
    assert zot.calls == 1


def test_batch_pdf_notes_line_up_with_items(monkeypatch):
    import zotero_mcp.papers as papers_mod

    zot = MagicMock()
    zot.top.return_value = []  # no duplicates
    zot.create_items.return_value = {
        "successful": {
            "0": {"key": "KEYA0001", "data": {"title": "A"}},
            "1": {"key": "KEYB0001", "data": {"title": "B"}},
        }
    }

    async def fake_resolve(doi):
        return {"message": {"title": [doi], "DOI": doi}}

    async def fake_pdf_url(doi):
        return f"https://example.org/{doi}.pdf"

    async def fake_note(_zot, key, _title, url):
        # Finish out of order: the first item's PDF is slower.
        await asyncio.sleep(0.02 if key == "KEYA0001" else 0)
        return f" (pdf {url.rsplit('/', 1)[1]})"

    monkeypatch.setattr(papers_mod, "_get_zot", lambda *_args: zot)
    monkeypatch.setattr(papers_mod, "_resolve_doi", fake_resolve)
    monkeypatch.setattr(papers_mod, "_find_open_access_pdf", fake_pdf_url)
    monkeypatch.setattr(papers_mod, "_attach_pdf_note", fake_note)

    result = asyncio.run(
        _register(papers_mod)["add_papers_by_dois"](dois=["10.1234/a", "10.1234/b"], attach_pdfs=True)
    )
    assert "[KEYA0001] A (pdf a.pdf)" in result
    assert "[KEYB0001] B (pdf b.pdf)" in result


def test_isbn_duplicate_matches_hyphenated_and_isbn10_forms():
    from zotero_mcp.papers import _has_isbn, _isbn13

    assert _isbn13("0306406152") == "9780306406157"
    assert _has_isbn({"ISBN": "978-0-306-40615-7 978-1-118-98702-5"}, "9781118987025")
    assert _has_isbn({"ISBN": "0-306-40615-2"}, "9780306406157")
    assert not _has_isbn({"ISBN": "978-1-118-98702-5"}, "9780306406157")


def _annotate_pdf(monkeypatch, tmp_path, build, existing=(), **kwargs):
    """Run create_annotation on a PDF made by ``build(doc)``; return (zot, result)."""

    pdf_path = tmp_path / "paper.pdf"
    doc = fitz.open()
    build(doc)
    doc.save(pdf_path)
    doc.save(tmp_path / "truth.pdf")  # create_annotation deletes its download

    zot = MagicMock()
    zot.children.return_value = [{"data": d} for d in existing]
    zot.create_items.return_value = {"successful": {"0": {"key": "ANN00001"}}}

    async def fake_download(*_args, **_kwargs):
        return str(pdf_path), "ATT00001"

    monkeypatch.setattr(annotations_mod, "_get_zot", lambda *_args: zot)
    monkeypatch.setattr(annotations_mod, "_download_pdf", fake_download)
    patched = []
    monkeypatch.setattr(annotations_mod, "_patch_item", lambda *args: patched.append(args))
    result = asyncio.run(_register(annotations_mod)["create_annotation"](item_key="ITEM0001", **kwargs))
    return zot, result, patched


def test_highlight_geometry_matches_pdf_space_on_rotated_cropped_pages(monkeypatch, tmp_path):
    cases = [
        ([0, 0, 612, 792], [50, 60, 562, 742], 0),
        ([0, 0, 612, 792], [50, 60, 562, 742], 90),
        ([100, 200, 712, 992], None, 90),
        ([100, 200, 712, 992], [150, 260, 662, 942], 270),
        ([0, 0, 612, 792], None, 180),
    ]
    for mediabox, cropbox, rotation in cases:
        def build(doc):
            page = doc.new_page(width=mediabox[2] - mediabox[0], height=mediabox[3] - mediabox[1])
            page.insert_text((150, 300), "hello world")
            doc.xref_set_key(page.xref, "MediaBox", "[%g %g %g %g]" % tuple(mediabox))
            if cropbox:
                doc.xref_set_key(page.xref, "CropBox", "[%g %g %g %g]" % tuple(cropbox))
            page.set_rotation(rotation)

        zot, _, _ = _annotate_pdf(monkeypatch, tmp_path, build, quoted_text="world")
        rect = json.loads(zot.create_items.call_args.args[0][0]["annotationPosition"])["rects"][0]

        # Ground truth from the raw PDF boxes: x + CropBox.x0, CropBox.y1 - y.
        check = fitz.open(tmp_path / "truth.pdf")
        page = check[0]
        word = next(w for w in page.get_text("words") if w[4] == "world")
        raw = check.xref_get_key(page.xref, "CropBox")
        box = [float(v) for v in (raw[1] if raw[0] != "null" else check.xref_get_key(page.xref, "MediaBox")[1]).strip("[]").split()]
        expected = [word[0] + box[0], box[3] - word[3], word[2] + box[0], box[3] - word[1]]
        assert all(abs(a - b) < 0.5 for a, b in zip(rect, expected)), (mediabox, cropbox, rotation, rect, expected)


def _two_pages(doc):
    for _ in range(2):
        doc.new_page().insert_text((72, 100), "alpha beta gamma delta")


def test_same_text_on_another_page_is_a_new_highlight(monkeypatch, tmp_path):
    page_one = {"key": "OLD00001", "version": 3, "itemType": "annotation", "annotationType": "highlight",
                "annotationText": "beta gamma",
                "annotationPosition": json.dumps({"pageIndex": 0, "rects": [[100, 680, 200, 700]]})}
    zot, result, patched = _annotate_pdf(
        monkeypatch, tmp_path, _two_pages, existing=[page_one],
        quoted_text="beta gamma", comment="c", page_number=2,
    )
    assert zot.create_items.called and not patched
    assert json.loads(zot.create_items.call_args.args[0][0]["annotationPosition"])["pageIndex"] == 1


def test_matching_existing_highlight_gets_the_comment(monkeypatch, tmp_path):
    zot, _, _ = _annotate_pdf(monkeypatch, tmp_path, _two_pages, quoted_text="beta gamma")
    position = zot.create_items.call_args.args[0][0]["annotationPosition"]
    same_spot = {"key": "OLD00001", "version": 3, "itemType": "annotation", "annotationType": "highlight",
                 "annotationText": "beta gamma", "annotationPosition": position}
    zot, result, patched = _annotate_pdf(
        monkeypatch, tmp_path, _two_pages, existing=[same_spot], quoted_text="beta gamma", comment="c",
    )
    assert "appended comment" in str(result) and not zot.create_items.called
    assert patched[0][1:3] == ("OLD00001", 3)


def test_whole_words_win_and_missing_occurrence_is_an_error(monkeypatch, tmp_path):
    def build(doc):
        doc.new_page().insert_text((72, 100), "the concatenate cat sat")

    zot, _, _ = _annotate_pdf(monkeypatch, tmp_path, build, quoted_text="cat")
    assert len(json.loads(zot.create_items.call_args.args[0][0]["annotationPosition"])["rects"]) == 1
    rect = json.loads(zot.create_items.call_args.args[0][0]["annotationPosition"])["rects"][0]
    word = next(w for w in fitz.open(tmp_path / "truth.pdf")[0].get_text("words") if w[4] == "cat")
    assert abs(rect[0] - word[0]) < 0.5  # the word "cat", not "concatenate"

    zot, result, _ = _annotate_pdf(monkeypatch, tmp_path, _two_pages, quoted_text="beta gamma", occurrence=3)
    assert result.is_error and "appears 2 time(s)" in result.content[0].text
    assert not zot.create_items.called


def test_unknown_jwt_kids_do_not_hammer_the_jwks_endpoint():
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    from zotero_mcp.auth import JWTTokenVerifier, OAuthConfig

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "k1"
    fetches = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            fetches.append(self.path)
            body = json.dumps({"keys": [jwk]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/jwks"
        verifier = JWTTokenVerifier(OAuthConfig("https://iss", "https://res/mcp", url, ["zotero:read"]))
        claims = {"iss": "https://iss", "aud": "https://res/mcp", "exp": int(time.time()) + 300, "sub": "u"}
        valid = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})
        assert asyncio.run(verifier.verify_token(valid)) is not None
        for i in range(5):
            bogus = jwt.encode(claims, key, algorithm="RS256", headers={"kid": f"bogus{i}"})
            assert asyncio.run(verifier.verify_token(bogus)) is None
        assert asyncio.run(verifier.verify_token(valid)) is not None
        # One initial fetch, at most one forced refresh for all unknown kids.
        assert len(fetches) <= 2, fetches
    finally:
        server.shutdown()


def test_crossref_types_organizations_editors_and_empty_dates():
    from zotero_mcp._helpers import _crossref_to_zotero

    item = _crossref_to_zotero({"message": {
        "type": "edited-book", "title": ["Proceedings"],
        "editor": [{"given": "Ada", "family": "Lovelace"}],
        "author": [{"name": "LIGO Scientific Collaboration"}],
        "published-print": {"date-parts": [[None]]}, "issued": {"date-parts": [[2020, 5]]},
    }})
    assert item["itemType"] == "book"
    assert {"creatorType": "author", "name": "LIGO Scientific Collaboration"} in item["creators"]
    assert {"creatorType": "editor", "firstName": "Ada", "lastName": "Lovelace"} in item["creators"]
    assert item["date"] == "2020-5"
    thesis = _crossref_to_zotero({"message": {"type": "dissertation", "title": ["T"],
                                              "institution": [{"name": "FAU"}], "publisher": "P"}})
    assert thesis["itemType"] == "thesis" and thesis["university"] == "FAU"


def test_fmt_item_shows_organization_creators():
    assert "(LIGO, 2016)" in _fmt_item({"key": "K", "title": "T", "date": "2016", "creators": [{"name": "LIGO"}]})


def test_arxiv_duplicate_ignores_versions_but_not_prefixes(monkeypatch):
    import xml.etree.ElementTree as ET

    import zotero_mcp.papers as papers_mod

    async def fake_arxiv(arxiv_id):
        return ET.fromstring(
            '<entry xmlns="http://www.w3.org/2005/Atom"><title>ReAct: Reasoning and Acting</title></entry>'
        )

    zot = MagicMock()
    zot.create_items.return_value = {"failed": {"0": "stop"}}
    monkeypatch.setattr(papers_mod, "_get_zot", lambda *_args: zot)
    monkeypatch.setattr(papers_mod, "_resolve_arxiv", fake_arxiv)
    tools = _register(papers_mod)

    # Found by title search (Zotero's ID search ignores url/extra), any version.
    saved = {"key": "OLD00001", "title": "ReAct", "url": "https://arxiv.org/abs/2210.03629v3"}
    zot.top.side_effect = lambda **kw: [{"data": saved}] if kw.get("qmode") != "everything" else []
    assert asyncio.run(tools["add_paper_by_arxiv_id"](arxiv_id="2210.03629")).startswith("Duplicate: [OLD00001]")
    title_query = next(c.kwargs["q"] for c in zot.top.call_args_list if c.kwargs.get("qmode") != "everything")
    assert title_query == "ReAct"

    # A different paper whose ID merely starts the same is not a duplicate.
    other = {"key": "OTHER001", "title": "ReAct", "url": "http://arxiv.org/abs/2210.036291"}
    zot.top.side_effect = lambda **kw: [{"data": other}]
    result = asyncio.run(tools["add_paper_by_arxiv_id"](arxiv_id="2210.03629"))
    assert "Duplicate" not in result.content[0].text


def test_metadata_isbn_field_with_several_isbns(monkeypatch):
    import zotero_mcp.papers as papers_mod

    zot = MagicMock()
    zot.item_template.return_value = {"itemType": "book", "title": "", "ISBN": "", "creators": []}
    zot.top.return_value = []
    zot.create_items.return_value = {"successful": {"0": {"key": "NEW00001"}}}
    monkeypatch.setattr(papers_mod, "_get_zot", lambda *_args: zot)
    result = asyncio.run(_register(papers_mod)["add_item_from_metadata"](
        item_type="book", title="Learning Python", fields={"ISBN": "978-0-596-51774-8 9780596517748"},
    ))
    assert result == "Added [NEW00001] Learning Python"
    assert zot.create_items.call_args.args[0][0]["ISBN"] == "978-0-596-51774-8 9780596517748"


def test_items_whose_url_mentions_tags_are_not_parsed_as_tag_lists():
    import httpx

    from zotero_mcp import _helpers

    def handler(request):
        return httpx.Response(200, json=[{"key": "AAAA1111", "data": {"key": "AAAA1111"}}],
                              headers={"Content-Type": "application/json"})

    zot = _helpers._Zotero("1", "user", "k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert zot.top(q="hashtags")[0]["key"] == "AAAA1111"


def test_trashed_items_can_be_tagged_and_filed(monkeypatch):
    import zotero_mcp.collections as collections_mod

    trashed = {"key": "TRSH1111", "version": 5, "deleted": 1, "title": "T",
               "tags": [{"tag": "keep"}], "collections": []}
    for module, tool, kwargs, change in [
        (tags_mod, "add_tags", {"tags": ["new"]}, {"tags": [{"tag": "keep"}, {"tag": "new"}]}),
        (collections_mod, "add_to_collection", {"collection_id": "COLL0001"}, {"collections": ["COLL0001"]}),
    ]:
        zot = MagicMock()
        zot.item.return_value = {"version": 5, "data": dict(trashed)}
        patched = []
        monkeypatch.setattr(module, "_get_zot", lambda *_args, z=zot: z)
        monkeypatch.setattr(module, "_patch_item", lambda *args: patched.append(args))
        asyncio.run(_register(module)[tool](item_key="TRSH1111", **kwargs))
        assert patched == [(zot, "TRSH1111", 5, change)], tool
        zot.update_item.assert_not_called()


def test_remove_tags_prefers_exact_case_and_delete_rejects_or_operator(monkeypatch):
    zot = MagicMock()
    zot.item.return_value = {"version": 1, "data": {"key": "K", "version": 1, "tags": [{"tag": "ML"}, {"tag": "ml"}]}}
    patched = []
    monkeypatch.setattr(tags_mod, "_get_zot", lambda *_args: zot)
    monkeypatch.setattr(tags_mod, "_patch_item", lambda *args: patched.append(args))
    tools = _register(tags_mod)
    asyncio.run(tools["remove_tags"](item_key="K", tags=["ml"]))
    assert patched[0][3] == {"tags": [{"tag": "ML"}]}

    result = asyncio.run(tools["delete_tags"](tags=["a || b"]))
    assert result.is_error
    zot.delete_tags.assert_not_called()


def test_unsetting_the_last_tag_color_deletes_the_setting(monkeypatch):
    zot = MagicMock(endpoint="https://api.zotero.org", library_type="users", library_id="1")
    zot.settings.return_value = {"tagColors": {"value": [{"name": "todo", "color": "#FF0000"}], "version": 9}}
    zot.client.delete.return_value.raise_for_status.return_value = None
    monkeypatch.setattr(tags_mod, "_get_zot", lambda *_args: zot)
    asyncio.run(_register(tags_mod)["unset_tag_color"](tag="todo"))
    zot.client.put.assert_not_called()
    assert zot.client.delete.call_args.kwargs["headers"] == {"If-Unmodified-Since-Version": "9"}


def test_rename_tag_keeps_the_color_when_no_item_was_renamed(monkeypatch):
    zot = MagicMock()
    zot.items.return_value = [{"data": {"key": "K", "tags": [{"tag": "ML"}]}}]  # no exact "ml"
    monkeypatch.setattr(tags_mod, "_get_zot", lambda *_args: zot)
    asyncio.run(_register(tags_mod)["rename_tag"](old_name="ml", new_name="x"))
    zot.settings.assert_not_called()
    zot.client.put.assert_not_called()


def test_verify_items_handles_markup_subtitles_and_datacite(monkeypatch):
    import httpx

    library_mod, tools = _register_library()
    items = [
        {"data": {"key": "MARKUP01", "title": "CRISPR screens in E. coli", "DOI": "https://doi.org/10.1234/a"}},
        {"data": {"key": "SUBTTL01", "title": "Main title: the subtitle", "DOI": "10.1234/b"}},
        {"data": {"key": "ARXIV001", "title": "Pythia", "DOI": "10.48550/arXiv.2304.01373"}},
    ]
    zot = MagicMock()
    zot.top.return_value = items

    async def fake_crossref(doi):
        if doi == "10.1234/a":
            return {"message": {"title": ["<scp>CRISPR</scp> screens in <i>E. coli</i>"]}}
        if doi == "10.1234/b":
            return {"message": {"title": ["Main title"], "subtitle": ["The subtitle"]}}
        raise httpx.HTTPStatusError("404", request=httpx.Request("GET", "x"), response=httpx.Response(404))

    async def fake_csl(doi):
        return {"title": "Pythia"}

    monkeypatch.setattr(library_mod, "_get_zot", lambda *_args: zot)
    monkeypatch.setattr(library_mod, "_resolve_doi", fake_crossref)
    monkeypatch.setattr(library_mod, "_resolve_doi_csl", fake_csl)
    result = asyncio.run(tools["verify_items"](limit=3))
    assert "3 OK" in result, result


def test_short_fuzzy_queries_need_exact_matches(monkeypatch):
    library_mod, tools = _register_library()
    zot = MagicMock()
    zot.top.side_effect = lambda **kw: [] if "q" in kw else [
        {"data": {"key": "A", "title": "egg salad", "itemType": "book"}},
        {"data": {"key": "B", "title": "emg decoding", "itemType": "book"}},
    ]
    monkeypatch.setattr(library_mod, "_get_zot", lambda *_args: zot)
    result = asyncio.run(tools["search_library"]("emg"))
    assert "[B]" in result and "[A]" not in result  # "egg" is one edit from "emg"


def test_bibtex_pages_keep_order_and_unique_citekeys(monkeypatch):
    import bibtexparser

    library_mod, tools = _register_library()
    page = lambda: bibtexparser.loads("@article{zeta, title={Z}}\n@article{smith2020, title={A}}\n")
    zot = MagicMock()
    zot.top.side_effect = [page(), page()]
    zot.request.headers = {"Total-Results": "200"}
    monkeypatch.setattr(library_mod, "_get_zot", lambda *_args: zot)
    result = asyncio.run(tools["get_bibtex"]())
    ids = re.findall(r"@article\{([^,]+),", result)
    assert ids == ["zeta", "smith2020", "zeta-2", "smith2020-2"]


def test_standalone_pdf_fulltext_and_read_only_key(monkeypatch):
    from zotero_mcp.library import _key_write_access

    library_mod, tools = _register_library()
    zot = MagicMock()
    zot.item.return_value = {"data": {"key": "PDF00001", "itemType": "attachment", "contentType": "application/pdf"}}
    zot.fulltext_item.return_value = {"content": "standalone text"}
    monkeypatch.setattr(library_mod, "_get_zot", lambda *_args: zot)
    assert asyncio.run(tools["get_item_fulltext"](item_key="PDF00001")) == "standalone text"
    zot.children.assert_not_called()

    assert _key_write_access({"access": {"user": {"library": True}}}, "users", "1") is False


def test_server_file_reads_reject_symlink_swaps_after_validation(monkeypatch, tmp_path):
    import os

    import pytest

    from zotero_mcp import runtime

    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (root / "ok.txt").write_text("ok")
    runtime.configure_runtime(transport="streamable-http", allow_server_files=True, file_roots=[str(root)])
    try:
        fd, real = runtime.open_server_file(str(root / "ok.txt"))
        os.close(fd)
        assert real.name == "ok.txt"

        # Simulate the race: validation saw safe paths, then they were swapped.
        monkeypatch.setattr(runtime, "validate_server_path", lambda path, **_kw: Path(path))
        (root / "dir").symlink_to(outside)
        with pytest.raises(PermissionError):
            runtime.open_server_file(str(root / "dir" / "secret.txt"))  # parent swapped
        (root / "file.txt").symlink_to(outside / "secret.txt")
        with pytest.raises(OSError):
            runtime.open_server_file(str(root / "file.txt"))  # final component swapped
    finally:
        runtime.configure_runtime(transport="stdio")


def test_unconfirmed_zotero_storage_upload_fails_and_cleans_up(monkeypatch, tmp_path):
    import pytest

    from zotero_mcp import _helpers

    upload = tmp_path / "paper.pdf"
    upload.write_bytes(b"%PDF-1.7\n")
    for registered in (False, True):
        children = []
        deleted = []
        zot = MagicMock(backoff_until=0)

        zot.item_template.return_value = {"itemType": "attachment", "linkMode": "imported_file"}

        def upload_attachments(payload, _parent, basedir, registered=registered):
            # Zotero rejects stored filenames containing a directory path.
            assert payload[0]["filename"] == "paper.pdf" and basedir == str(tmp_path)
            # Pyzotero's result for a 429 on the upload steps looks like success.
            children.append({"key": "NEWATT01", "data": {"filename": "paper.pdf", "md5": "abc" if registered else None}})
            return {"success": [{"key": "NEWATT01"}], "failure": [], "unchanged": []}

        zot.upload_attachments.side_effect = upload_attachments
        zot.children.side_effect = lambda *_a, **_k: list(children)

        async def fake_delete(_zot, key):
            deleted.append(key)

        monkeypatch.setattr(_helpers, "_delete_orphan_attachment", fake_delete)
        if registered:
            assert asyncio.run(_helpers._attach_file_local(zot, "PARENT01", str(upload))) == "PARENT01"
            assert deleted == []
        else:
            with pytest.raises(ValueError, match="did not confirm"):
                asyncio.run(_helpers._attach_file_local(zot, "PARENT01", str(upload)))
            assert deleted == ["NEWATT01"]


def test_fuzzy_highlight_sort_index_has_real_offset(monkeypatch, tmp_path):
    def build(doc):
        doc.new_page().insert_text((72, 100), "alpha beta gamma delta epsilon")

    zot, result, _ = _annotate_pdf(monkeypatch, tmp_path, build, quoted_text="gamma delte epsilon")
    annotation = zot.create_items.call_args.args[0][0]
    assert "fuzzy" in str(result.content[0].text if hasattr(result, "content") else result)
    assert annotation["annotationSortIndex"].split("|")[1] == f"{len('alpha beta '):06d}"


def test_query_parameters_do_not_leak_between_calls():
    import httpx

    from zotero_mcp import _helpers

    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        body = {"itemType": "book", "title": ""} if "items/new" in request.url.path else []
        return httpx.Response(200, json=body, headers={"Content-Type": "application/json"})

    zot = _helpers._Zotero("1", "user", "k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    asyncio.run(_helpers._zot_call(zot.item_template, "book"))
    asyncio.run(_helpers._zot_call(zot.children, "ITEM0001"))
    assert "itemType" not in seen[-1], seen


def test_list_tags_pages_past_zoteros_missing_next_links(monkeypatch):
    import httpx

    from zotero_mcp import _helpers

    def handler(request):
        start = int(request.url.params.get("start", 0))
        count = max(0, min(100, 256 - start))
        # Zotero reports Total-Results=110 and drops the next link after page 2.
        return httpx.Response(200, json=[{"tag": f"t{start + i:03d}"} for i in range(count)],
                              headers={"Content-Type": "application/json", "Total-Results": "110"})

    zot = _helpers._Zotero("1", "user", "k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(tags_mod, "_get_zot", lambda *_args: zot)
    result = asyncio.run(_register(tags_mod)["list_tags"](limit=5))
    assert result.startswith("Tags (256 total")


def test_plugin_pins_the_released_package_version():
    root = Path(__file__).resolve().parents[1]
    version = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M).group(1)
    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    server = json.loads((root / "server.json").read_text())
    assert plugin["version"] == version
    assert f"zotero-library-mcp=={version}" in plugin["mcpServers"]["zotero"]["args"]
    assert server["version"] == version
    # Every ${user_config.X} the server uses must be declared.
    env = json.dumps(plugin["mcpServers"]["zotero"]["env"])
    assert set(re.findall(r"\$\{user_config\.(\w+)\}", env)) <= set(plugin["userConfig"])
