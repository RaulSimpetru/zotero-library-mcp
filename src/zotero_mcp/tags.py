"""Tools for managing Zotero tags."""

from ._helpers import _get_zot


def register(mcp):
    @mcp.tool()
    async def list_tags(limit: int = 100) -> str:
        """List all tags in your Zotero library.

        Args:
            limit: Maximum number of tags to return (default 100)
        """
        zot = _get_zot()

        try:
            all_tags = zot.everything(zot.tags())
        except Exception as e:
            return f"Could not fetch tags: {e}"

        if not all_tags:
            return "No tags in library."

        sorted_tags = sorted(all_tags, key=lambda t: t.lower())

        if limit and len(sorted_tags) > limit:
            sorted_tags = sorted_tags[:limit]

        return f"Tags ({len(sorted_tags)}):\n" + "\n".join(sorted_tags)

    @mcp.tool()
    async def delete_tags(tags: list[str]) -> str:
        """Delete tags from the entire Zotero library. This removes the tags from all items.

        Args:
            tags: List of tag names to delete from the library
        """
        zot = _get_zot()

        try:
            zot.delete_tags(*tags)
        except Exception as e:
            return f"Failed to delete tags: {e}"

        return f"Deleted {len(tags)} tag(s) from library: {', '.join(tags)}"

    @mcp.tool()
    async def add_tags(item_key: str, tags: list[str], color: str | None = None) -> str:
        """Add one or more tags to a Zotero item. Optionally assign a color to all added tags.

        Args:
            item_key: The Zotero item key
            tags: List of tags to add
            color: Optional hex color code (e.g. '#FF0000') to assign to the added tags
        """
        zot = _get_zot()

        try:
            item = zot.item(item_key)
        except Exception as e:
            return f"Could not find item {item_key}: {e}"

        title = item.get("data", {}).get("title", item_key)

        try:
            zot.add_tags(item, *tags)
        except Exception as e:
            return f"Failed to add tags: {e}"

        result = f"Tagged '{title}': {', '.join(tags)}"

        if color:
            color_results = []
            for tag in tags:
                r = await set_tag_color(tag=tag, color=color)
                color_results.append(r)
            result += "\n" + "\n".join(color_results)

        return result

    @mcp.tool()
    async def remove_tags(item_key: str, tags: list[str]) -> str:
        """Remove one or more tags from a Zotero item.

        Args:
            item_key: The Zotero item key
            tags: List of tags to remove
        """
        zot = _get_zot()

        try:
            item = zot.item(item_key)
        except Exception as e:
            return f"Could not find item {item_key}: {e}"

        data = item.get("data", {})
        title = data.get("title", item_key)
        current_tags = data.get("tags", [])
        tags_lower = {t.lower() for t in tags}
        new_tags = [t for t in current_tags if t.get("tag", "").lower() not in tags_lower]

        removed_count = len(current_tags) - len(new_tags)
        if removed_count == 0:
            return f"None of the specified tags were found on '{title}'."

        data["tags"] = new_tags
        try:
            zot.update_item(data)
        except Exception as e:
            return f"Failed to update item: {e}"

        return f"Removed {removed_count} tag(s) from '{title}'."

    @mcp.tool()
    async def set_tag_color(tag: str, color: str, position: int = 0) -> str:
        """Assign a color to a tag in the Zotero library. Colored tags appear in the tag selector and item lists.

        Args:
            tag: The tag name to colorize
            color: Hex color code (e.g. '#FF0000' for red, '#3366CC' for blue)
            position: Sort position for the colored tag (0-8, lower = higher priority)
        """
        zot = _get_zot()

        try:
            settings = zot.settings()
        except Exception:
            settings = {}

        tag_colors = []
        if isinstance(settings, dict) and "tagColors" in settings:
            tag_colors = settings["tagColors"].get("value", [])

        tag_colors = [tc for tc in tag_colors if tc.get("name") != tag]

        tag_colors.append({"name": tag, "color": color, "position": position})

        if len(tag_colors) > 9:
            return f"Cannot add color: Zotero supports a maximum of 9 colored tags. Currently have {len(tag_colors) - 1}."

        url = f"{zot.endpoint}/{zot.library_type}/{zot.library_id}/settings/tagColors"
        headers = {
            "Content-Type": "application/json",
        }
        if zot.api_key:
            headers["Authorization"] = f"Bearer {zot.api_key}"

        payload = {"value": tag_colors, "version": 0}
        try:
            resp = zot.client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                payload["version"] = data.get("version", 0)
        except Exception:
            pass

        try:
            resp = zot.client.put(url, headers=headers, json=payload)
            resp.raise_for_status()
        except Exception as e:
            return f"Failed to set tag color: {e}"

        return f"Set color {color} on tag '{tag}' at position {position}."

    @mcp.tool()
    async def rename_tag(old_name: str, new_name: str) -> str:
        """Rename a tag across all items in the Zotero library.

        Args:
            old_name: The current tag name
            new_name: The new tag name to replace it with
        """
        zot = _get_zot()

        try:
            items = zot.everything(zot.items(tag=old_name))
        except Exception as e:
            return f"Could not search for tag '{old_name}': {e}"

        if not items:
            return f"No items found with tag '{old_name}'."

        updated = 0
        errors = []
        for item in items:
            data = item.get("data", {})
            tags = data.get("tags", [])

            new_tags = []
            found = False
            for t in tags:
                if t.get("tag") == old_name:
                    new_tags.append({"tag": new_name, "type": t.get("type", 0)})
                    found = True
                else:
                    new_tags.append(t)

            if not found:
                continue

            data["tags"] = new_tags
            try:
                zot.update_item(data)
                updated += 1
            except Exception as e:
                errors.append(f"{data.get('key', '?')}: {e}")

        result = f"Renamed tag '{old_name}' → '{new_name}' on {updated} item(s)."
        if errors:
            result += f"\nErrors ({len(errors)}): " + "; ".join(errors[:5])
        return result
