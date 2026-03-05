# zotero-library-mcp

An MCP server that lets AI assistants add papers and books to your Zotero library by DOI, arXiv ID, or ISBN — and manage your collections, tags, and items.

## Tools

### Adding papers

- **`add_paper_by_doi`** — Resolve a DOI via CrossRef and add the paper to Zotero (with duplicate detection)
- **`add_papers_by_dois`** — Batch-add up to 50 papers at once
- **`add_paper_by_arxiv_id`** — Add a preprint by arXiv ID (uses DOI when available, falls back to arXiv metadata)

### Adding books

- **`add_book_by_isbn`** — Resolve an ISBN via Open Library and add the book to Zotero (with duplicate detection)

### Searching & browsing

- **`search_library`** — Search your Zotero library by title, author, tag, etc.
- **`get_item_details`** — View full metadata for any item
- **`get_recent_items`** — List recently added items
- **`get_unfiled_items`** — Get items not in any collection

### Reading & annotating

- **`get_item_fulltext`** — Get the full text of an indexed PDF
- **`get_bibtex`** — Export BibTeX for one or more items, a collection, or your full library (with optional `save_path` to write a `.bib` file directly)
- **`get_annotations`** — List all highlights and annotations on a paper's PDF
- **`create_annotation`** — Highlight a text passage in a PDF (searches for the exact text, creates a visible highlight in Zotero's reader, and returns a preview image for verification)
- **`add_note`** — Add a note to an item

### File attachments

- **`attach_file`** — Attach a local file to an item
- **`download_pdf`** — Download a PDF attachment to a local file (useful when Zotero's fulltext index is incomplete)

### Collections

- **`list_collections`** — List all collections (with nesting)
- **`create_collection`** — Create a new collection (optionally nested under a parent)
- **`get_collection_items`** — Browse items in a collection
- **`add_to_collection`** — Add an existing item to a collection
- **`remove_from_collection`** — Remove an item from a collection (keeps it in your library)

### Tags

- **`add_tags`** — Add one or more tags to an item
- **`remove_tags`** — Remove tags from an item

### Verification

- **`verify_items`** — Re-check recent items against CrossRef to catch bad DOIs or title mismatches

### Deleting

- **`delete_item`** — Permanently delete an item from your library
- **`delete_collection`** — Permanently delete a collection

## Prerequisites

1. A [Zotero account](https://www.zotero.org/user/register)
2. A Zotero API key with **write** permissions: https://www.zotero.org/settings/keys
3. Your Zotero **library ID** (shown on the same page, or in your profile URL)
4. [uv](https://docs.astral.sh/uv/) installed

## Quick Start

### Claude Code

```bash
claude mcp add zotero \
  -e ZOTERO_LIBRARY_ID=your_library_id \
  -e ZOTERO_API_KEY=your_api_key \
  -- uvx --from git+https://github.com/RaulSimpetru/zotero-library-mcp zotero-mcp
```

#### WebDAV setup

To use WebDAV file storage (e.g. Synology, Nextcloud), include the WebDAV variables:

```bash
claude mcp add zotero \
  -e ZOTERO_LIBRARY_ID=your_library_id \
  -e ZOTERO_API_KEY=your_api_key \
  -e ZOTERO_WEBDAV_URL=https://your-webdav-server.com \
  -e ZOTERO_WEBDAV_USER=your_username \
  -e ZOTERO_WEBDAV_PASSWORD=your_password \
  -- uvx --from git+https://github.com/RaulSimpetru/zotero-library-mcp zotero-mcp
```

### Claude Desktop

Add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "zotero": {
      "command": "/full/path/to/uvx",
      "args": ["--from", "git+https://github.com/RaulSimpetru/zotero-library-mcp", "zotero-mcp"],
      "env": {
        "ZOTERO_LIBRARY_ID": "your_library_id",
        "ZOTERO_API_KEY": "your_api_key",
        "ZOTERO_WEBDAV_URL": "https://your-webdav-server.com",
        "ZOTERO_WEBDAV_USER": "your_username",
        "ZOTERO_WEBDAV_PASSWORD": "your_password"
      }
    }
  }
}
```

> **Note:** Claude Desktop doesn't inherit your shell's PATH, so you need the full path to `uvx`. Find it with `which uvx` in your terminal.

### Run standalone

```bash
ZOTERO_LIBRARY_ID=your_id ZOTERO_API_KEY=your_key \
  uvx --from git+https://github.com/RaulSimpetru/zotero-library-mcp zotero-mcp
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ZOTERO_LIBRARY_ID` | Yes | Your Zotero user or group library ID |
| `ZOTERO_API_KEY` | Yes | API key with read/write permissions |
| `ZOTERO_LIBRARY_TYPE` | No | `user` (default) or `group` |
| `CROSSREF_MAILTO` | No | Your email for CrossRef polite pool (faster API access) |
| `ZOTERO_WEBDAV_URL` | No | WebDAV URL for file storage (e.g. `https://dav.example.com`) |
| `ZOTERO_WEBDAV_USER` | No | WebDAV username |
| `ZOTERO_WEBDAV_PASSWORD` | No | WebDAV password |

> **Note:** If all three `ZOTERO_WEBDAV_*` variables are set, file attachments are uploaded to your WebDAV server instead of Zotero's built-in storage. The server automatically appends `/zotero` to the base URL, matching Zotero Desktop's behavior.

## How it works

1. You provide a DOI, arXiv ID, or ISBN
2. The server queries the appropriate API to get full metadata:
   - **DOI** → [CrossRef API](https://api.crossref.org)
   - **arXiv ID** → [arXiv API](https://info.arxiv.org/help/api/) (with CrossRef fallback when a DOI exists)
   - **ISBN** → [Open Library API](https://openlibrary.org/developers/api)
3. Metadata is mapped to Zotero's item format (title, authors, journal/publisher, date, etc.)
4. The item is created in your Zotero library via the [Zotero Web API](https://www.zotero.org/support/dev/web_api/v3/start)

## License

MIT

mcp-name: io.github.RaulSimpetru/zotero-library-mcp
