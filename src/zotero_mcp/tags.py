"""Tools for managing Zotero tags."""

import asyncio
import re

from ._helpers import _get_zot, _validate_limit, _zot_call
from .responses import tool_error
from .tool_annotations import DESTRUCTIVE, READ_ONLY, WRITE


def _normalize_tags(tags: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for tag in tags:
        cleaned = tag.strip()
        if not cleaned:
            raise ValueError("Tags must not be empty")
        folded = cleaned.casefold()
        if folded not in seen:
            normalized.append(cleaned)
            seen.add(folded)
    if not normalized:
        raise ValueError("At least one tag is required")
    if len(normalized) > 50:
        raise ValueError("A maximum of 50 tags can be changed at once")
    return normalized


def _validate_color(color: str) -> str:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("color must be a six-digit hex value such as #3366CC")
    return color.upper()


async def _get_tag_colors(zot) -> tuple[list[dict], int]:
    settings = await _zot_call(zot.settings)
    tag_setting = settings.get("tagColors", {}) if isinstance(settings, dict) else {}
    return list(tag_setting.get("value", [])), int(tag_setting.get("version", 0))


async def _put_tag_colors(zot, colors: list[dict], version: int) -> None:
    url = f"{zot.endpoint}/{zot.library_type}/{zot.library_id}/settings/tagColors"
    headers = {"Content-Type": "application/json"}
    if zot.api_key:
        headers["Zotero-API-Key"] = zot.api_key
    response = await _zot_call(
        zot.client.put,
        url,
        headers=headers,
        json={"value": colors, "version": version},
    )
    response.raise_for_status()


def register(mcp):
    @mcp.tool(annotations=READ_ONLY)
    async def list_tags(limit: int = 100) -> str:
        """List all tags in your Zotero library.

        Args:
            limit: Maximum number of tags to return (default 100)
        """
        try:
            limit = _validate_limit(limit, maximum=1000)
            zot = _get_zot()
            all_tags = await _zot_call(lambda: zot.everything(zot.tags()))
        except Exception as e:
            return tool_error(f"Could not fetch tags: {e}")

        if not all_tags:
            return "No tags in library."

        sorted_tags = sorted(all_tags, key=lambda t: t.lower())

        total = len(sorted_tags)
        sorted_tags = sorted_tags[:limit]

        return f"Tags ({total} total, showing {len(sorted_tags)}):\n" + "\n".join(sorted_tags)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_tags(tags: list[str]) -> str:
        """Delete tags from the entire Zotero library. This removes the tags from all items.

        Args:
            tags: List of tag names to delete from the library
        """
        try:
            tags = _normalize_tags(tags)
            zot = _get_zot()
            await _zot_call(zot.delete_tags, *tags)
        except Exception as e:
            return tool_error(f"Failed to delete tags: {e}")

        return f"Deleted {len(tags)} tag(s) from library: {', '.join(tags)}"

    @mcp.tool(annotations=WRITE)
    async def add_tags(item_key: str, tags: list[str], color: str | None = None) -> str:
        """Add one or more tags to a Zotero item. Optionally assign a color to all added tags.

        Args:
            item_key: The Zotero item key
            tags: List of tags to add
            color: Optional hex color code (e.g. '#FF0000') to assign to the added tags
        """
        try:
            tags = _normalize_tags(tags)
            existing_colors: list[dict] = []
            color_version = 0
            if color:
                color = _validate_color(color)
            zot = _get_zot()
            if color:
                existing_colors, color_version = await _get_tag_colors(zot)
                new_names = {
                    tag.casefold() for tag in tags
                    if not any(c.get("name", "").casefold() == tag.casefold() for c in existing_colors)
                }
                if len(existing_colors) + len(new_names) > 9:
                    return tool_error(
                        "Cannot color these tags: Zotero supports at most 9 colored tags"
                    )
        except Exception as exc:
            return tool_error(f"Invalid tag request: {exc}")

        try:
            item = await _zot_call(zot.item, item_key)
        except Exception as e:
            return tool_error(f"Could not find item {item_key}: {e}")

        title = item.get("data", {}).get("title", item_key)

        try:
            await _zot_call(zot.add_tags, item, *tags)
        except Exception as e:
            return tool_error(f"Failed to add tags: {e}")

        result = f"Tagged '{title}': {', '.join(tags)}"

        if color:
            try:
                by_name = {tag.casefold(): tag for tag in tags}
                found = set()
                for tag_color in existing_colors:
                    folded = tag_color.get("name", "").casefold()
                    if folded in by_name:
                        tag_color["name"] = by_name[folded]
                        tag_color["color"] = color
                        found.add(folded)
                for folded, tag in by_name.items():
                    if folded not in found:
                        existing_colors.append({"name": tag, "color": color})
                for index, tag_color in enumerate(existing_colors):
                    tag_color["position"] = index
                await _put_tag_colors(zot, existing_colors, color_version)
            except Exception as exc:
                return tool_error(
                    "Tags were added, but color assignment failed: "
                    f"{exc}"
                )
            result += f"\nSet color {color} on {len(tags)} tag(s)."

        return result

    @mcp.tool(annotations=WRITE)
    async def remove_tags(item_key: str, tags: list[str]) -> str:
        """Remove one or more tags from a Zotero item.

        Args:
            item_key: The Zotero item key
            tags: List of tags to remove
        """
        try:
            tags = _normalize_tags(tags)
            zot = _get_zot()
        except Exception as exc:
            return tool_error(f"Invalid tag request: {exc}")

        try:
            item = await _zot_call(zot.item, item_key)
        except Exception as e:
            return tool_error(f"Could not find item {item_key}: {e}")

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
            await _zot_call(zot.update_item, data)
        except Exception as e:
            return tool_error(f"Failed to update item: {e}")

        return f"Removed {removed_count} tag(s) from '{title}'."

    @mcp.tool(annotations=WRITE)
    async def set_tag_color(tag: str, color: str, position: int = 0) -> str:
        """Assign a color to a tag in the Zotero library. Colored tags appear in the tag selector and item lists.

        Args:
            tag: The tag name to colorize
            color: Hex color code (e.g. '#FF0000' for red, '#3366CC' for blue)
            position: Sort position for the colored tag (0-8, lower = higher priority)
        """
        try:
            tag = tag.strip()
            if not tag:
                raise ValueError("tag must not be empty")
            color = _validate_color(color)
            if not 0 <= position <= 8:
                raise ValueError("position must be between 0 and 8")
            zot = _get_zot()
            tag_colors, version = await _get_tag_colors(zot)
            folded = tag.casefold()
            tag_colors = [
                tc for tc in tag_colors if tc.get("name", "").casefold() != folded
            ]
            if len(tag_colors) >= 9:
                return tool_error("Cannot add color: Zotero supports at most 9 colored tags")
            tag_colors.insert(min(position, len(tag_colors)), {"name": tag, "color": color})
            for index, tag_color in enumerate(tag_colors):
                tag_color["position"] = index
            await _put_tag_colors(zot, tag_colors, version)
        except Exception as e:
            return tool_error(f"Failed to set tag color: {e}")

        return f"Set color {color} on tag '{tag}' at position {position}."

    @mcp.tool(annotations=WRITE)
    async def rename_tag(old_name: str, new_name: str) -> str:
        """Rename a tag across all items in the Zotero library.

        Args:
            old_name: The current tag name
            new_name: The new tag name to replace it with
        """
        old_name = old_name.strip()
        new_name = new_name.strip()
        if not old_name or not new_name:
            return tool_error("old_name and new_name must not be empty")
        if old_name.casefold() == new_name.casefold():
            return "Tag already has the requested name."
        zot = _get_zot()

        try:
            items = await _zot_call(lambda: zot.everything(zot.items(tag=old_name)))
        except Exception as e:
            return tool_error(f"Could not search for tag '{old_name}': {e}")

        if not items:
            return f"No items found with tag '{old_name}'."

        async def _rename_one(item: dict) -> tuple[bool, str | None]:
            data = item.get("data", {})
            tags = data.get("tags", [])

            new_tags = []
            found = False
            for t in tags:
                if t.get("tag") == old_name:
                    if not any(
                        existing.get("tag", "").casefold() == new_name.casefold()
                        for existing in tags
                        if existing is not t
                    ):
                        new_tags.append({"tag": new_name, "type": t.get("type", 0)})
                    found = True
                else:
                    new_tags.append(t)

            if not found:
                return False, None

            data["tags"] = new_tags
            try:
                await _zot_call(zot.update_item, data)
                return True, None
            except Exception as e:
                return False, f"{data.get('key', '?')}: {e}"

        semaphore = asyncio.Semaphore(8)

        async def _bounded(item: dict):
            async with semaphore:
                return await _rename_one(item)

        outcomes = await asyncio.gather(*(_bounded(item) for item in items))
        updated = sum(success for success, _ in outcomes)
        errors = [error for _, error in outcomes if error]

        try:
            colors, version = await _get_tag_colors(zot)
            old_folded = old_name.casefold()
            new_folded = new_name.casefold()
            changed = False
            target_exists = any(
                color.get("name", "").casefold() == new_folded for color in colors
            )
            updated_colors = []
            for tag_color in colors:
                if tag_color.get("name", "").casefold() == old_folded:
                    changed = True
                    if target_exists:
                        continue
                    tag_color["name"] = new_name
                updated_colors.append(tag_color)
            if changed:
                for index, tag_color in enumerate(updated_colors):
                    tag_color["position"] = index
                await _put_tag_colors(zot, updated_colors, version)
        except Exception as exc:
            errors.append(f"tag color: {exc}")

        result = f"Renamed tag '{old_name}' → '{new_name}' on {updated} item(s)."
        if errors:
            result += f"\nErrors ({len(errors)}): " + "; ".join(errors[:5])
        return result

    @mcp.tool(annotations=WRITE)
    async def unset_tag_color(tag: str) -> str:
        """Remove a tag's assigned library color without deleting the tag."""

        tag = tag.strip()
        if not tag:
            return tool_error("tag must not be empty")
        try:
            zot = _get_zot()
            colors, version = await _get_tag_colors(zot)
            folded = tag.casefold()
            remaining = [
                color for color in colors
                if color.get("name", "").casefold() != folded
            ]
            if len(remaining) == len(colors):
                return f"Tag '{tag}' does not have an assigned color."
            for index, color in enumerate(remaining):
                color["position"] = index
            await _put_tag_colors(zot, remaining, version)
            return f"Removed color from tag '{tag}'."
        except Exception as exc:
            return tool_error(f"Failed to remove tag color: {exc}")
