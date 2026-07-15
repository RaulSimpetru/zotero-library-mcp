"""Runtime safety settings shared by transports and filesystem tools."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class OpenAIFile(BaseModel):
    """File object supplied by ChatGPT through ``openai/fileParams``."""

    model_config = ConfigDict(extra="forbid")

    download_url: str
    file_id: str
    mime_type: str | None = None
    file_name: str | None = None


@dataclass
class RuntimeSettings:
    transport: str = "stdio"
    allow_server_files: bool = True
    file_roots: tuple[Path, ...] = field(default_factory=tuple)


_settings = RuntimeSettings()


def configure_runtime(
    *,
    transport: str,
    allow_server_files: bool = False,
    file_roots: list[str] | None = None,
) -> None:
    """Configure path access for the selected MCP transport.

    Stdio is a local, user-controlled transport and retains the historical path
    behavior. HTTP denies server path reads and writes unless the operator opts
    in and confines access to one or more roots.
    """

    roots = tuple(Path(root).expanduser().resolve() for root in (file_roots or []))
    if transport == "stdio":
        _settings.transport = transport
        _settings.allow_server_files = True
        _settings.file_roots = roots
        return

    if allow_server_files and not roots:
        raise ValueError(
            "--allow-server-files requires at least one --file-root to confine access"
        )

    _settings.transport = transport
    _settings.allow_server_files = allow_server_files
    _settings.file_roots = roots


def is_http_transport() -> bool:
    return _settings.transport == "streamable-http"


def validate_server_path(path: str, *, for_write: bool = False) -> Path:
    """Resolve and authorize a local server path for reading or writing."""

    if not path or "\x00" in path:
        raise ValueError("A non-empty local file path is required")
    if not _settings.allow_server_files:
        raise PermissionError(
            "Server filesystem paths are disabled for HTTP transport. "
            "Use a ChatGPT file input or an MCP file resource instead."
        )

    candidate = Path(os.path.expanduser(path))
    # For a new output file, resolve the existing parent so a final symlink
    # cannot escape an allowed root unnoticed.
    resolved = candidate.resolve(strict=not for_write)
    if _settings.file_roots:
        if not any(resolved == root or resolved.is_relative_to(root) for root in _settings.file_roots):
            roots = ", ".join(str(root) for root in _settings.file_roots)
            raise PermissionError(f"Path is outside the configured file roots: {roots}")
    return resolved
