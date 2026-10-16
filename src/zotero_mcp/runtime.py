"""Runtime safety settings shared by transports and filesystem tools."""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

import os
import stat
import tempfile
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


def atomic_write(destination: Path, write) -> None:
    """Write via a fresh temp file in the same directory, then rename into place.

    mkstemp creates a random name with O_EXCL, so a symlink planted at a
    predictable staging name cannot redirect the write outside the file root.
    """

    fd, staging = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(fd, 0o644)  # mkstemp's 0600 would make the saved file private
        with os.fdopen(fd, "wb") as output:
            write(output)
        os.replace(staging, destination)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise


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
    _require_within_roots(resolved)
    return resolved


def _require_within_roots(resolved: Path) -> None:
    if _settings.file_roots:
        if not any(resolved == root or resolved.is_relative_to(root) for root in _settings.file_roots):
            roots = ", ".join(str(root) for root in _settings.file_roots)
            raise PermissionError(f"Path is outside the configured file roots: {roots}")


def _opened_path(fd: int) -> Path | None:
    """Where an open file descriptor actually points, if the OS can tell us."""

    if fcntl is not None and hasattr(fcntl, "F_GETPATH"):  # macOS
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
        return Path(raw.split(b"\0", 1)[0].decode())
    proc = Path(f"/proc/self/fd/{fd}")  # Linux
    return Path(os.readlink(proc)) if proc.exists() else None


def open_server_file(path: str) -> tuple[int, Path]:
    """Open an authorized file for reading; returns (fd, real path).

    Checking a path and then opening it is racy: a file or parent directory
    swapped for a symlink in between would be followed out of the file roots.
    So refuse a final symlink when opening, and check the root against where
    the opened descriptor really points.
    """

    resolved = validate_server_path(path)
    fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Not a regular file: {resolved}")
        actual = _opened_path(fd)
        if actual is not None:
            _require_within_roots(actual.resolve())
        return fd, actual or resolved
    except BaseException:
        os.close(fd)
        raise
