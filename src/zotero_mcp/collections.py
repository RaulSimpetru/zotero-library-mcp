"""Tools for managing Zotero collections."""

from ._helpers import _fmt_item, _get_zot


def register(mcp):
    @mcp.tool()
    async def list_collections() -> str:
        """List all collections in your Zotero library."""
        zot = _get_zot()
        collections = zot.collections()

        if not collections:
            return "No collections."

        parent_map = {}
        for col in collections:
            d = col.get("data", {})
            parent_map[d.get("key", "")] = d.get("parentCollection", None)

        def _depth(key: str) -> int:
            d, cur = 0, key
            while parent_map.get(cur):
                d += 1
                cur = parent_map[cur]
            return d

        lines = []
        for col in collections:
            d = col.get("data", {})
            key = d.get("key", "")
            name = d.get("name", "?")
            n = d.get("meta", {}).get("numItems", col.get("meta", {}).get("numItems", 0))
            lines.append(f"{'  ' * _depth(key)}[{key}] {name} ({n})")

        return "\n".join(lines)

    @mcp.tool()
    async def add_to_collection(item_key: str, collection_id: str) -> str:
        """Add an existing Zotero item to a collection.

        Args:
            item_key: The Zotero item key (from search results)
            collection_id: The collection key to add it to
        """
        zot = _get_zot()

        try:
            item = zot.item(item_key)
        except Exception as e:
            return f"Could not find item {item_key}: {e}"

        data = item.get("data", {})
        collections = data.get("collections", [])

        if collection_id in collections:
            return f"Item is already in collection {collection_id}."

        collections.append(collection_id)
        data["collections"] = collections

        try:
            zot.update_item(data)
        except Exception as e:
            return f"Failed to update item: {e}"

        return f"Added '{data.get('title', item_key)}' to {collection_id}."

    @mcp.tool()
    async def remove_from_collection(item_key: str, collection_id: str) -> str:
        """Remove an item from a collection without deleting it from the library.

        Args:
            item_key: The Zotero item key
            collection_id: The collection key to remove it from
        """
        zot = _get_zot()

        try:
            item = zot.item(item_key)
        except Exception as e:
            return f"Could not find item {item_key}: {e}"

        data = item.get("data", {})
        collections = data.get("collections", [])

        if collection_id not in collections:
            return f"Item is not in collection {collection_id}."

        title = data.get("title", item_key)

        try:
            zot.deletefrom_collection(collection_id, item)
        except Exception as e:
            return f"Failed to remove from collection: {e}"

        return f"Removed '{title}' from {collection_id}."

    @mcp.tool()
    async def create_collection(name: str, parent_collection_id: str | None = None) -> str:
        """Create a new collection in your Zotero library.

        Args:
            name: Name for the new collection
            parent_collection_id: Optional parent collection key to nest under
        """
        zot = _get_zot()

        payload = [{"name": name}]
        if parent_collection_id:
            payload[0]["parentCollection"] = parent_collection_id

        try:
            result = zot.create_collections(payload)
        except Exception as e:
            return f"Failed to create collection: {e}"

        successful = result.get("successful", {})
        if successful:
            col = list(successful.values())[0]
            key = col.get("key", "unknown")
            return f"Created [{key}] {name}"

        failed = result.get("failed", {})
        if failed:
            errors = list(failed.values())
            return f"Zotero rejected the collection: {errors}"

        return f"Unexpected response from Zotero: {result}"

    @mcp.tool()
    async def delete_collection(collection_id: str) -> str:
        """Permanently delete a collection from your Zotero library.

        Items in the collection are NOT deleted — they remain in your library.

        Args:
            collection_id: The collection key to delete
        """
        zot = _get_zot()

        try:
            col = zot.collection(collection_id)
        except Exception as e:
            return f"Could not find collection {collection_id}: {e}"

        name = col.get("data", {}).get("name", collection_id)

        try:
            zot.delete_collection(col)
        except Exception as e:
            return f"Failed to delete collection: {e}"

        return f"Deleted collection [{collection_id}] {name}"

    @mcp.tool()
    async def get_collection_items(collection_id: str, limit: int = 25) -> str:
        """Get all items in a specific collection.

        Args:
            collection_id: The collection key to browse
            limit: Maximum number of items to return (default 25)
        """
        zot = _get_zot()

        try:
            results = zot.collection_items(collection_id, limit=limit)
        except Exception as e:
            return f"Could not fetch collection items: {e}"

        if not results:
            return "Empty collection."

        lines = []
        for item in results:
            data = item.get("data", {})
            if data.get("itemType") not in ("attachment", "note"):
                lines.append(_fmt_item(data))

        return "\n".join(lines) if lines else "Empty collection."
