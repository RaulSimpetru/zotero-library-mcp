"""Zotero MCP Server — Add papers by DOI and manage your Zotero library."""

import argparse
import logging
import os
from collections.abc import Sequence
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import annotations, collections, libraries, library, papers, tags
from .auth import _csv_env, build_mcp_auth, oauth_tool_meta
from .file_resources import read_temp_resource
from .runtime import configure_runtime


# HTTPX logs complete URLs at INFO. Some upstream APIs put credentials or
# private identifiers in URLs, so keep transport request logs at warning only.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
_auth_settings, _token_verifier = build_mcp_auth()

mcp = MCPServer(
    "zotero",
    instructions=(
        "Search and manage Zotero. Resolve real item and collection keys before "
        "mutations; never invent keys. Prefer trash_item over deletion. For new "
        "citations prefer DOI, arXiv ID, then ISBN. Use list_attachments before "
        "choosing among multiple PDFs. Use list_libraries to discover shared libraries. "
        "Pass both library_id and library_type on every call for that library; "
        "omitting them uses the configured default. Item and collection keys are "
        "scoped to their library."
    ),
    website_url="https://github.com/RaulSimpetru/zotero-library-mcp",
    auth=_auth_settings,
    token_verifier=_token_verifier,
)

_register_tool = mcp.tool


def _tool(**kwargs: Any):
    """Register a tool with a human title and optional OAuth metadata.

    Wraps the public ``tool()`` decorator rather than editing the SDK's private
    tool registry after the fact, which breaks across SDK releases.
    """

    def decorator(fn):
        kwargs.setdefault("title", fn.__name__.replace("_", " ").title())
        kwargs["meta"] = oauth_tool_meta(kwargs.get("meta"))
        return _register_tool(**kwargs)(fn)

    return decorator


mcp.tool = _tool

# Register all tool groups
libraries.register(mcp)
papers.register(mcp)
library.register(mcp)
collections.register(mcp)
tags.register(mcp)
annotations.register(mcp)


@mcp.resource("zotero://pdf/{token}", mime_type="application/pdf")
def get_temporary_pdf(token: str) -> bytes:
    """Read a short-lived PDF returned by a Zotero MCP tool."""

    return read_temp_resource(token, expected_mime_type="application/pdf")


@mcp.resource("zotero://image/{token}", mime_type="image/png")
def get_temporary_image(token: str) -> bytes:
    """Read a short-lived PNG preview returned by a Zotero MCP tool."""

    return read_temp_resource(token, expected_mime_type="image/png")


@mcp.resource("zotero://file/{token}", mime_type="application/octet-stream")
def get_temporary_file(token: str) -> bytes:
    """Read another short-lived file returned by a Zotero MCP tool."""

    return read_temp_resource(token)


# ---------------------------------------------------------------------------
# Entrypoint and transport configuration
# ---------------------------------------------------------------------------

def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for stdio and Streamable HTTP clients."""
    parser = argparse.ArgumentParser(description="Run the Zotero MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.getenv("ZOTERO_MCP_TRANSPORT", "stdio"),
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("ZOTERO_MCP_HOST", "127.0.0.1"),
        help="HTTP bind host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        # A string default goes through type=int, so a bad PORT gives a usage
        # error instead of a traceback at import time.
        default=os.getenv("ZOTERO_MCP_PORT", os.getenv("PORT", "8000")),
        help="HTTP bind port (default: 8000, or PORT)",
    )
    parser.add_argument(
        "--http-path",
        default=os.getenv("ZOTERO_MCP_HTTP_PATH", "/mcp"),
        help="Streamable HTTP endpoint path (default: /mcp)",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=_csv_env("ZOTERO_MCP_ALLOWED_HOSTS"),
        help=(
            "Allowed HTTP Host header; repeat for multiple hosts. "
            "Also accepts ZOTERO_MCP_ALLOWED_HOSTS as a comma-separated list."
        ),
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=_csv_env("ZOTERO_MCP_ALLOWED_ORIGINS"),
        help=(
            "Allowed HTTP Origin; repeat for multiple origins. "
            "Also accepts ZOTERO_MCP_ALLOWED_ORIGINS as a comma-separated list."
        ),
    )
    parser.add_argument(
        "--stateless-http",
        action="store_true",
        default=_env_flag("ZOTERO_MCP_STATELESS_HTTP"),
        help="Run Streamable HTTP without persistent MCP sessions",
    )
    parser.add_argument(
        "--allow-unauthenticated-http",
        action="store_true",
        default=_env_flag("ZOTERO_MCP_ALLOW_UNAUTHENTICATED_HTTP"),
        help=(
            "Explicitly permit an unauthenticated non-loopback HTTP listener. "
            "Unsafe unless another trusted access-control layer protects it."
        ),
    )
    parser.add_argument(
        "--allow-server-files",
        action="store_true",
        default=_env_flag("ZOTERO_MCP_ALLOW_SERVER_FILES"),
        help="Permit path-based file reads/writes in HTTP mode (requires --file-root)",
    )
    parser.add_argument(
        "--file-root",
        action="append",
        default=_csv_env("ZOTERO_MCP_FILE_ROOTS"),
        help=(
            "Allowed root for path-based file access; repeat for multiple roots. "
            "Also accepts ZOTERO_MCP_FILE_ROOTS as a comma-separated list."
        ),
    )
    return parser


def configure_server(args: argparse.Namespace) -> dict[str, Any]:
    """Validate transport settings and build the keyword arguments for ``mcp.run``.

    MCP 2.0 takes host/port/path options as ``run()`` arguments rather than
    server-level settings, so these are returned instead of assigned.
    """
    if not args.http_path.startswith("/"):
        raise ValueError("--http-path must start with '/'")

    configure_runtime(
        transport=args.transport,
        allow_server_files=args.allow_server_files,
        file_roots=args.file_root,
    )

    if args.transport != "streamable-http":
        return {}

    local_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    local_origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]
    allowed_hosts = list(dict.fromkeys([*local_hosts, *args.allowed_host]))
    allowed_origins = list(dict.fromkeys([*local_origins, *args.allowed_origin]))

    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allowed_host:
        raise ValueError(
            "A non-loopback HTTP bind requires at least one --allowed-host "
            "(or ZOTERO_MCP_ALLOWED_HOSTS)."
        )
    if (
        args.host not in {"127.0.0.1", "localhost", "::1"}
        and _token_verifier is None
        and not args.allow_unauthenticated_http
    ):
        raise ValueError(
            "A non-loopback HTTP bind requires OAuth configuration. Set the "
            "ZOTERO_MCP_OAUTH_* variables, or explicitly acknowledge the risk with "
            "--allow-unauthenticated-http when an external access-control layer is used."
        )

    return {
        "host": args.host,
        "port": args.port,
        "streamable_http_path": args.http_path,
        "stateless_http": args.stateless_http,
        "transport_security": TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
    }


def main(argv: Sequence[str] | None = None) -> None:
    """Run the Zotero MCP server over stdio or Streamable HTTP."""
    args = build_parser().parse_args(argv)
    try:
        run_options = configure_server(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    mcp.run(transport=args.transport, **run_options)


if __name__ == "__main__":
    main()
