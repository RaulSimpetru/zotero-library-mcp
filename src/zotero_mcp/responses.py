"""Helpers for MCP-native errors and downloadable resource results."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.types import CallToolResult, ResourceLink, TextContent


def sanitize_error_message(message: str) -> str:
    """Remove credentials, URL paths/query strings, and the local home path."""

    sanitized = str(message)
    for variable in (
        "ZOTERO_API_KEY",
        "ZOTERO_WEBDAV_PASSWORD",
        "ZOTERO_WEBDAV_USER",
    ):
        value = os.getenv(variable, "")
        if value:
            sanitized = sanitized.replace(value, "[redacted]")

    def _redact_url(match: re.Match[str]) -> str:
        raw = match.group(0).rstrip(".,;:)")
        suffix = match.group(0)[len(raw):]
        try:
            parsed = urlsplit(raw)
            host = parsed.hostname or "redacted"
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://{host}{port}/[redacted]{suffix}"
        except Exception:
            return "[redacted URL]"

    sanitized = re.sub(r"https?://[^\s]+", _redact_url, sanitized)
    try:
        home = str(Path.home())
        if home and home != "/":
            sanitized = sanitized.replace(home, "~")
    except Exception:
        pass
    return sanitized


def tool_error(message: str) -> CallToolResult:
    """Return a failure that MCP clients see as ``isError=true``."""

    message = sanitize_error_message(message)
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        # ``result`` keeps this compatible with FastMCP's auto-generated
        # wrapper schema for functions annotated as returning ``str``.
        structuredContent={"result": message},
        isError=True,
    )


def resource_result(
    message: str,
    *,
    uri: str,
    name: str,
    mime_type: str,
    size: int,
    **data: Any,
) -> CallToolResult:
    """Return a friendly message plus an MCP ``resource_link`` download."""

    file_data = {
        "uri": uri,
        "file_name": name,
        "mime_type": mime_type,
        "size": size,
    }
    return CallToolResult(
        content=[
            TextContent(type="text", text=message),
            ResourceLink(
                type="resource_link",
                uri=uri,
                name=name,
                mimeType=mime_type,
                size=size,
            ),
        ],
        structuredContent={
            "result": message,
            "ok": True,
            "message": message,
            "file": file_data,
            **data,
        },
    )
