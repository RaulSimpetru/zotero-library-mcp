"""Short-lived, opaque MCP resources for generated and downloaded files."""

from __future__ import annotations

import atexit
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path


RESOURCE_TTL_SECONDS = 15 * 60
MAX_RESOURCES = 32


@dataclass
class StoredResource:
    path: Path
    name: str
    mime_type: str
    expires_at: float


_resources: dict[str, StoredResource] = {}


def clear_temp_resources() -> None:
    """Delete all files owned by the resource store, including on shutdown."""

    for resource in list(_resources.values()):
        try:
            resource.path.unlink(missing_ok=True)
        except OSError:
            pass
    _resources.clear()


atexit.register(clear_temp_resources)


def _cleanup() -> None:
    now = time.monotonic()
    expired = [token for token, item in _resources.items() if item.expires_at <= now]
    for token in expired:
        item = _resources.pop(token)
        try:
            item.path.unlink(missing_ok=True)
        except OSError:
            pass

    while len(_resources) >= MAX_RESOURCES:
        token = min(_resources, key=lambda key: _resources[key].expires_at)
        item = _resources.pop(token)
        try:
            item.path.unlink(missing_ok=True)
        except OSError:
            pass


def register_temp_resource(path: str, *, name: str, mime_type: str) -> str:
    """Own a temp file and return an unguessable MCP resource URI."""

    _cleanup()
    resource_path = Path(path).resolve(strict=True)
    token = secrets.token_urlsafe(24)
    _resources[token] = StoredResource(
        path=resource_path,
        name=os.path.basename(name) or resource_path.name,
        mime_type=mime_type,
        expires_at=time.monotonic() + RESOURCE_TTL_SECONDS,
    )
    return f"zotero://file/{token}"


def read_temp_resource(token: str) -> bytes:
    """Read a registered resource while enforcing expiry and size limits upstream."""

    _cleanup()
    resource = _resources.get(token)
    if resource is None:
        raise ValueError("File resource is missing or has expired")
    return resource.path.read_bytes()
