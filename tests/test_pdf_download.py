"""PDF download progress and timeout behavior."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

import zotero_mcp._helpers as helpers


def _mock_zotero() -> MagicMock:
    zot = MagicMock()
    zot.children.return_value = [
        {
            "data": {
                "itemType": "attachment",
                "contentType": "application/pdf",
                "key": "PDF12345",
            }
        }
    ]
    zot.file.return_value = b"%PDF-1.7\n%%EOF\n"
    return zot


def test_zotero_pdf_download_reports_progress(monkeypatch):
    zot = _mock_zotero()
    updates = []

    async def progress(value, message):
        updates.append((value, message))

    monkeypatch.setattr(helpers, "_use_webdav", lambda: False)
    path, attachment_key = asyncio.run(
        helpers._download_pdf(zot, "ITEM1234", progress=progress)
    )

    try:
        assert attachment_key == "PDF12345"
        assert Path(path).read_bytes().startswith(b"%PDF-")
        assert [value for value, _ in updates] == [2, 10, 98]
    finally:
        Path(path).unlink(missing_ok=True)


def test_webdav_timeout_has_actionable_message(monkeypatch):
    zot = _mock_zotero()

    class TimeoutStream:
        async def __aenter__(self):
            raise httpx.ReadTimeout("slow WebDAV")

        async def __aexit__(self, *_args):
            return False

    class TimeoutClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return TimeoutStream()

    monkeypatch.setattr(helpers, "_use_webdav", lambda: True)
    monkeypatch.setattr(
        helpers,
        "_webdav_config",
        lambda: ("https://dav.example/zotero", "user", "password"),
    )
    monkeypatch.setattr(helpers.httpx, "AsyncClient", TimeoutClient)

    with pytest.raises(TimeoutError, match="timed out after 60 seconds"):
        asyncio.run(helpers._download_pdf(zot, "ITEM1234"))
