"""Zotero MCP Server — Add papers by DOI and manage your Zotero library."""

from mcp.server.fastmcp import FastMCP

from . import annotations, collections, library, papers, tags

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "zotero",
    instructions="Add papers to Zotero by DOI and manage your library",
)

# Register all tool groups
papers.register(mcp)
library.register(mcp)
collections.register(mcp)
tags.register(mcp)
annotations.register(mcp)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    """Run the Zotero MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
