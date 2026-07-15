"""Tools for managing Zotero collections."""

from collections import defaultdict

from ._helpers import _fmt_item, _get_zot, _validate_limit, _zot_call
from .responses import tool_error
from .tool_annotations import DESTRUCTIVE, READ_ONLY, WRITE


async def _collections_bounded(zot, maximum: int = 5000) -> tuple[list[dict], bool]:
    collections: list[dict] = []
    start = 0
    while len(collections) < maximum:
        page_size = min(100, maximum - len(collections))
        page = await _zot_call(zot.collections, limit=page_size, start=start)
        if not page:
            break
        collections.extend(page)
        if len(page) < page_size:
            return collections, False
        start += page_size
    return collections, len(collections) >= maximum


def register(mcp):
    @mcp.tool(annotations=READ_ONLY)
    async def list_collections() -> str:
        """List all collections in your Zotero library."""
        try:
            zot = _get_zot()
            collections, capped = await _collections_bounded(zot)
        except Exception as exc:
            return tool_error(f"Could not list collections: {exc}")

        if not collections:
            return "No collections."

        by_key = {}
        children: defaultdict[str | None, list[dict]] = defaultdict(list)
        for col in collections:
            d = col.get("data", {})
            key = d.get("key", "")
            if key:
                by_key[key] = col
                parent = d.get("parentCollection") or None
                children[parent].append(col)

        lines = []
        visited = set()

        def _walk(parent: str | None, depth: int) -> None:
            for col in sorted(
                children.get(parent, []),
                key=lambda value: value.get("data", {}).get("name", "").casefold(),
            ):
                data = col.get("data", {})
                key = data.get("key", "")
                if key in visited:
                    continue
                visited.add(key)
                name = data.get("name", "?")
                count = col.get("meta", {}).get("numItems", 0)
                lines.append(f"{'  ' * depth}[{key}] {name} ({count})")
                _walk(key, depth + 1)

        _walk(None, 0)
        # Preserve malformed/orphaned collections instead of dropping them.
        for key, col in sorted(by_key.items()):
            if key not in visited:
                data = col.get("data", {})
                lines.append(f"[{key}] {data.get('name', '?')} (orphaned hierarchy)")

        if capped:
            lines.append("[Collection scan capped at 5,000 entries]")
        return "\n".join(lines)

    @mcp.tool(annotations=WRITE)
    async def add_to_collection(item_key: str, collection_id: str) -> str:
        """Add an existing Zotero item to a collection.

        Args:
            item_key: The Zotero item key (from search results)
            collection_id: The collection key to add it to
        """
        zot = _get_zot()

        try:
            await _zot_call(zot.collection, collection_id)
            item = await _zot_call(zot.item, item_key)
        except Exception as e:
            return tool_error(f"Could not validate item or collection: {e}")

        data = item.get("data", {})
        collections = data.get("collections", [])

        if collection_id in collections:
            return f"Item is already in collection {collection_id}."

        collections.append(collection_id)
        data["collections"] = collections

        try:
            await _zot_call(zot.update_item, data)
        except Exception as e:
            return tool_error(f"Failed to update item: {e}")

        return f"Added '{data.get('title', item_key)}' to {collection_id}."

    @mcp.tool(annotations=WRITE)
    async def remove_from_collection(item_key: str, collection_id: str) -> str:
        """Remove an item from a collection without deleting it from the library.

        Args:
            item_key: The Zotero item key
            collection_id: The collection key to remove it from
        """
        zot = _get_zot()

        try:
            await _zot_call(zot.collection, collection_id)
            item = await _zot_call(zot.item, item_key)
        except Exception as e:
            return tool_error(f"Could not validate item or collection: {e}")

        data = item.get("data", {})
        collections = data.get("collections", [])

        if collection_id not in collections:
            return f"Item is not in collection {collection_id}."

        title = data.get("title", item_key)

        try:
            await _zot_call(zot.deletefrom_collection, collection_id, item)
        except Exception as e:
            return tool_error(f"Failed to remove from collection: {e}")

        return f"Removed '{title}' from {collection_id}."

    @mcp.tool(annotations=WRITE)
    async def create_collection(name: str, parent_collection_id: str | None = None) -> str:
        """Create a new collection in your Zotero library.

        Args:
            name: Name for the new collection
            parent_collection_id: Optional parent collection key to nest under
        """
        name = name.strip()
        if not name:
            return tool_error("Collection name must not be empty")
        zot = _get_zot()

        try:
            if parent_collection_id:
                await _zot_call(zot.collection, parent_collection_id)
            existing, _ = await _collections_bounded(zot)
            for collection in existing:
                data = collection.get("data", {})
                if (
                    data.get("name", "").casefold() == name.casefold()
                    and (data.get("parentCollection") or None) == parent_collection_id
                ):
                    return (
                        f"Duplicate: [{data.get('key', '?')}] {data.get('name', name)} "
                        "already exists at this level"
                    )
        except Exception as exc:
            return tool_error(f"Could not validate collection: {exc}")

        payload = [{"name": name}]
        if parent_collection_id:
            payload[0]["parentCollection"] = parent_collection_id

        try:
            result = await _zot_call(zot.create_collections, payload)
        except Exception as e:
            return tool_error(f"Failed to create collection: {e}")

        successful = result.get("successful", {})
        if successful:
            col = list(successful.values())[0]
            key = col.get("key", "unknown")
            return f"Created [{key}] {name}"

        failed = result.get("failed", {})
        if failed:
            errors = list(failed.values())
            return tool_error(f"Zotero rejected the collection: {errors}")

        return tool_error(f"Unexpected response from Zotero: {result}")

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_collection(collection_id: str) -> str:
        """Permanently delete a collection from your Zotero library.

        Items in the collection are NOT deleted — they remain in your library.

        Args:
            collection_id: The collection key to delete
        """
        zot = _get_zot()

        try:
            col = await _zot_call(zot.collection, collection_id)
        except Exception as e:
            return tool_error(f"Could not find collection {collection_id}: {e}")

        name = col.get("data", {}).get("name", collection_id)

        try:
            await _zot_call(zot.delete_collection, col)
        except Exception as e:
            return tool_error(f"Failed to delete collection: {e}")

        return f"Deleted collection [{collection_id}] {name}"

    @mcp.tool(annotations=READ_ONLY)
    async def get_collection_items(collection_id: str, limit: int = 25) -> str:
        """Get all items in a specific collection.

        Args:
            collection_id: The collection key to browse
            limit: Maximum number of items to return (default 25)
        """
        try:
            limit = _validate_limit(limit, maximum=100)
            zot = _get_zot()
            await _zot_call(zot.collection, collection_id)
        except Exception as e:
            return tool_error(f"Could not fetch collection items: {e}")

        results = []
        start = 0
        page_size = min(100, max(limit * 2, 20))
        while len(results) < limit and start < 1000:
            try:
                page = await _zot_call(
                    zot.collection_items,
                    collection_id,
                    limit=page_size,
                    start=start,
                )
            except Exception as exc:
                return tool_error(f"Could not fetch collection items: {exc}")
            if not page:
                break
            results.extend(
                item
                for item in page
                if item.get("data", {}).get("itemType")
                not in ("attachment", "note", "annotation")
            )
            if len(page) < page_size:
                break
            start += page_size

        if not results:
            return "Empty collection."

        lines = [_fmt_item(item.get("data", {})) for item in results[:limit]]

        return "\n".join(lines) if lines else "Empty collection."

    @mcp.tool(annotations=WRITE)
    async def rename_collection(collection_id: str, new_name: str) -> str:
        """Rename a Zotero collection without changing its parent."""

        new_name = new_name.strip()
        if not new_name:
            return tool_error("Collection name must not be empty")
        try:
            zot = _get_zot()
            collection = await _zot_call(zot.collection, collection_id)
            data = collection.get("data", {})
            old_name = data.get("name", collection_id)
            data["name"] = new_name
            await _zot_call(zot.update_collection, data)
            return f"Renamed collection [{collection_id}] {old_name} → {new_name}"
        except Exception as exc:
            return tool_error(f"Failed to rename collection: {exc}")

    @mcp.tool(annotations=WRITE)
    async def move_collection(
        collection_id: str,
        parent_collection_id: str | None = None,
    ) -> str:
        """Move a collection under another collection, or to the library root."""

        if collection_id == parent_collection_id:
            return tool_error("A collection cannot be its own parent")
        try:
            zot = _get_zot()
            collection = await _zot_call(zot.collection, collection_id)
            if parent_collection_id:
                parent = await _zot_call(zot.collection, parent_collection_id)
                # Prevent a direct descendant cycle by walking parent links.
                current = parent.get("data", {})
                visited = set()
                while current:
                    key = current.get("key", "")
                    if key == collection_id:
                        return tool_error("Cannot move a collection beneath its descendant")
                    if key in visited:
                        return tool_error("Collection hierarchy contains a cycle")
                    visited.add(key)
                    if not current.get("parentCollection"):
                        break
                    ancestor = await _zot_call(zot.collection, current["parentCollection"])
                    current = ancestor.get("data", {})
            data = collection.get("data", {})
            data["parentCollection"] = parent_collection_id or False
            await _zot_call(zot.update_collection, data)
            destination = parent_collection_id or "library root"
            return f"Moved collection [{collection_id}] to {destination}"
        except Exception as exc:
            return tool_error(f"Failed to move collection: {exc}")
