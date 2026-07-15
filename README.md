# zotero-library-mcp

An MCP server that lets Claude, Codex, and ChatGPT add papers and books to your Zotero library by DOI, arXiv ID, or ISBN — and manage your collections, tags, and items.

The server supports both MCP transports used by these clients:

- **stdio** (default) for Claude Code, Claude Desktop, Codex CLI/IDE, and the ChatGPT desktop app
- **Streamable HTTP** for a hosted ChatGPT app or any remote MCP client

## Tools

### Adding papers

- **`add_paper_by_doi`** — Resolve a DOI via CrossRef and add the paper to Zotero (with duplicate detection)
- **`add_papers_by_dois`** — Batch-add up to 50 papers at once
- **`add_paper_by_arxiv_id`** — Add a preprint by arXiv ID (uses DOI when available, falls back to arXiv metadata)
- **`add_item_from_metadata`** — Create any supported Zotero item type from validated manual metadata

### Adding books

- **`add_book_by_isbn`** — Resolve an ISBN via Open Library and add the book to Zotero (with duplicate detection)

### Searching & browsing

- **`search_library`** — Search your Zotero library by title, author, tag, etc. (falls back to fuzzy matching when the exact search returns no results)
- **`get_item_details`** — View full metadata for any item
- **`get_recent_items`** — List recently added items
- **`get_unfiled_items`** — Get items not in any collection
- **`search_fulltext`** — Search Zotero metadata and indexed full text
- **`find_duplicates`** — Find duplicate items by DOI, ISBN, or normalized title
- **`list_attachments`** — List every attachment and choose a specific PDF key
- **`health_check`** — Verify library credentials, access, and storage configuration

### Reading & annotating

- **`get_item_fulltext`** — Return bounded plain text from Zotero's index or a PDF, without leaking temporary paths
- **`get_bibtex`** — Read-only BibTeX/BibLaTeX export for items, a collection, or the full library
- **`save_bibtex`** — Save an export to an authorized local path
- **`get_annotations`** — List all highlights and annotations on a paper's PDF
- **`create_annotation`** — Highlight a text passage in a PDF (searches for the exact text, creates a visible highlight in Zotero's reader, and returns a preview image for verification). Smart overlap handling: exact duplicates update the existing comment; sub-passages get a contrasting highlight color automatically.
- **`add_note`** — Add a note to an item
- **`list_notes`**, **`update_note`**, **`delete_note`** — Manage existing notes
- **`update_annotation`**, **`delete_annotation`** — Edit or remove annotations

### File attachments

- **`attach_file`** — Attach a local file over stdio or a ChatGPT file input over HTTP
- **`download_pdf`** — Return a remote-safe MCP file resource
- **`save_pdf`** — Save a PDF to an authorized local path

### Collections

- **`list_collections`** — List all collections (with nesting)
- **`create_collection`** — Create a new collection (optionally nested under a parent)
- **`get_collection_items`** — Browse items in a collection
- **`add_to_collection`** — Add an existing item to a collection
- **`remove_from_collection`** — Remove an item from a collection (keeps it in your library)
- **`rename_collection`**, **`move_collection`** — Reorganize collections

### Tags

- **`list_tags`** — List all tags in your library
- **`add_tags`** — Add one or more tags to an item (with optional color)
- **`remove_tags`** — Remove tags from an item
- **`delete_tags`** — Delete tags from the entire library
- **`set_tag_color`** — Assign a color to a tag (appears in Zotero's tag selector)
- **`rename_tag`** — Rename a tag across all items in your library
- **`unset_tag_color`** — Remove a tag color without deleting the tag

### Verification

- **`verify_items`** — Re-check recent items against CrossRef to catch bad DOIs or title mismatches

### Deleting

- **`delete_item`** — Permanently delete an item from your library
- **`delete_collection`** — Permanently delete a collection
- **`trash_item`**, **`restore_item`** — Prefer reversible trash operations for ordinary cleanup

The server also exposes the standard read-only **`search`** and **`fetch`** tool shapes used by ChatGPT company knowledge and deep research.

## Prerequisites

1. A [Zotero account](https://www.zotero.org/user/register)
2. A Zotero API key with **write** permissions: https://www.zotero.org/settings/keys
3. Your Zotero **library ID** (shown on the same page, or in your profile URL)
4. [uv](https://docs.astral.sh/uv/) installed

## Quick Start

### Codex and the ChatGPT desktop app

Codex and the ChatGPT desktop app share MCP configuration on the same Codex host. Add the server once:

```bash
codex mcp add zotero \
  --env ZOTERO_LIBRARY_ID=your_library_id \
  --env ZOTERO_API_KEY=your_api_key \
  -- uvx --from git+https://github.com/RaulSimpetru/zotero-library-mcp zotero-mcp
```

Then restart Codex or the ChatGPT desktop app. In Codex, use `/mcp` to confirm that `zotero` is connected. In ChatGPT desktop, open **Settings → MCP servers** to view the same server.

For WebDAV storage, add the three `ZOTERO_WEBDAV_*` values shown in the [WebDAV example](#webdav-setup). If the desktop app cannot find `uvx`, replace it with the full path returned by `which uvx`.

You can also configure the server directly in `~/.codex/config.toml`:

```toml
[mcp_servers.zotero]
command = "/full/path/to/uvx"
args = ["--from", "git+https://github.com/RaulSimpetru/zotero-library-mcp", "zotero-mcp"]
env_vars = ["ZOTERO_LIBRARY_ID", "ZOTERO_API_KEY", "ZOTERO_LIBRARY_TYPE", "CROSSREF_MAILTO", "ZOTERO_WEBDAV_URL", "ZOTERO_WEBDAV_USER", "ZOTERO_WEBDAV_PASSWORD"]
startup_timeout_sec = 30
tool_timeout_sec = 120
```

With `env_vars`, start Codex/ChatGPT from an environment that contains those variables. Use `[mcp_servers.zotero.env]` instead if you intentionally want to store their values in the config file.

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

### ChatGPT on the web (Apps SDK / developer mode)

ChatGPT web connects to an HTTPS Streamable HTTP endpoint. Start the server locally with the HTTP transport, then make it reachable through [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) or another authenticated HTTPS deployment:

```bash
ZOTERO_LIBRARY_ID=your_id ZOTERO_API_KEY=your_key \
  uvx --from git+https://github.com/RaulSimpetru/zotero-library-mcp zotero-mcp \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8000 \
  --allowed-host your-tunnel.example.com
```

The MCP endpoint is `https://your-tunnel.example.com/mcp`. Enable developer mode in ChatGPT, create a developer-mode app, and enter that URL as the MCP server URL. See OpenAI's [Connect from ChatGPT](https://developers.openai.com/apps-sdk/deploy/connect-chatgpt) guide for the current UI flow.

> **Security:** The safest personal setup is OpenAI Secure MCP Tunnel with the MCP server bound to loopback. HTTP mode disables all server-path reads and writes by default. `attach_file` accepts ChatGPT's authorized file object, while `download_pdf` returns an opaque MCP resource link. Safety annotations are approval hints, not an authorization boundary.

For a public single-library deployment, configure an external OAuth 2.1 identity provider. The server validates JWT access tokens against its JWKS endpoint:

```bash
export ZOTERO_MCP_OAUTH_ISSUER=https://auth.example.com
export ZOTERO_MCP_OAUTH_RESOURCE=https://zotero.example.com
export ZOTERO_MCP_OAUTH_JWKS_URL=https://auth.example.com/.well-known/jwks.json
export ZOTERO_MCP_OAUTH_SCOPES=zotero:read,zotero:write
```

The authorization server must publish OAuth/OIDC discovery metadata, support the MCP OAuth 2.1 flow with PKCE, issue tokens for `ZOTERO_MCP_OAUTH_RESOURCE`, and include the configured scopes. See OpenAI's [authentication guide](https://developers.openai.com/apps-sdk/build/auth). For testing behind an already authenticated gateway only, `--allow-unauthenticated-http` explicitly acknowledges an unauthenticated non-loopback listener.

This process still uses one server-side Zotero library key. A true multi-user service must map the verified OAuth identity to separate Zotero credentials and enforce per-user authorization; that deployment architecture is intentionally outside this personal-server package.

If an HTTP deployment genuinely needs server paths, enable them only inside confined roots:

```bash
zotero-mcp --transport streamable-http \
  --allow-server-files \
  --file-root /srv/zotero-mcp/exports
```

HTTP launch settings can also be supplied as environment variables:

| CLI option | Environment variable | Default |
|------------|----------------------|---------|
| `--transport` | `ZOTERO_MCP_TRANSPORT` | `stdio` |
| `--host` | `ZOTERO_MCP_HOST` | `127.0.0.1` |
| `--port` | `ZOTERO_MCP_PORT` or `PORT` | `8000` |
| `--http-path` | `ZOTERO_MCP_HTTP_PATH` | `/mcp` |
| `--allowed-host` | `ZOTERO_MCP_ALLOWED_HOSTS` (comma-separated) | local hosts |
| `--allowed-origin` | `ZOTERO_MCP_ALLOWED_ORIGINS` (comma-separated) | local origins |
| `--stateless-http` | `ZOTERO_MCP_STATELESS_HTTP` | `false` |
| `--allow-unauthenticated-http` | `ZOTERO_MCP_ALLOW_UNAUTHENTICATED_HTTP` | `false` |
| `--allow-server-files` | `ZOTERO_MCP_ALLOW_SERVER_FILES` | `false` in HTTP mode |
| `--file-root` | `ZOTERO_MCP_FILE_ROOTS` (comma-separated) | none |

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
| `UNPAYWALL_EMAIL` | No | Contact email for open-access PDF lookup (defaults to `CROSSREF_MAILTO`) |
| `ZOTERO_WEBDAV_URL` | No | WebDAV URL for file storage (e.g. `https://dav.example.com`) |
| `ZOTERO_WEBDAV_USER` | No | WebDAV username |
| `ZOTERO_WEBDAV_PASSWORD` | No | WebDAV password |
| `ZOTERO_MCP_OAUTH_ISSUER` | No | External OAuth/OIDC issuer URL for protected HTTP deployments |
| `ZOTERO_MCP_OAUTH_RESOURCE` | No | Canonical HTTPS MCP resource/audience URL |
| `ZOTERO_MCP_OAUTH_JWKS_URL` | No | JWKS URL used to verify JWT access tokens |
| `ZOTERO_MCP_OAUTH_SCOPES` | No | Comma-separated required scopes (defaults to read and write) |
| `ZOTERO_MCP_FILE_ROOTS` | No | Comma-separated allowed roots when HTTP server paths are enabled |

> **Note:** If all three `ZOTERO_WEBDAV_*` variables are set, file attachments are uploaded to your WebDAV server instead of Zotero's built-in storage. The server automatically appends `/zotero` to the base URL, matching Zotero Desktop's behavior.

## Upgrading to 0.8

Two path-writing operations were split from their read-only counterparts so remote clients can apply correct safety approvals:

- `get_bibtex(save_path=...)` is now `save_bibtex(save_path=...)`; `get_bibtex` only returns data.
- `download_pdf(save_path=...)` is now `save_pdf(save_path=...)`; `download_pdf` returns an opaque MCP resource link.

Existing read-only calls to `get_bibtex` and `download_pdf` continue to work.

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
