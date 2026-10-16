"""Tools for managing Zotero tags."""

import re
from typing import Literal

from ._helpers import _fetch_all, _get_zot, _patch_item, _validate_limit, _zot_call
from .responses import sanitize_error_message, tool_error
from .tool_annotations import DESTRUCTIVE, READ_ONLY, WRITE


def _normalize_tags(tags: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for tag in tags:
        cleaned = tag.strip()
        if not cleaned:
            raise ValueError("Tags must not be empty")
        # Exact dedupe: Zotero treats "ML" and "ml" as different tags.
        if cleaned not in seen:
            normalized.append(cleaned)
            seen.add(cleaned)
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
    response = await _zot_call(
        zot.client.put, url, json={"value": colors, "version": version}
    )
    response.raise_for_status()


async def _delete_tag_colors(zot, version: int) -> None:
    # Zotero rejects {"value": []}, so clearing the last color deletes the setting.
    url = f"{zot.endpoint}/{zot.library_type}/{zot.library_id}/settings/tagColors"
    response = await _zot_call(
        zot.client.delete, url, headers={"If-Unmodified-Since-Version": str(version)}
    )
    response.raise_for_status()


def register(mcp):
    @mcp.tool(annotations=READ_ONLY)
    async def list_tags(
        limit: int = 100,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """List all tags in your Zotero library.

        Args:
            limit: Maximum number of tags to return (default 100)
        """
        try:
            limit = _validate_limit(limit, maximum=1000)
            zot = _get_zot(library_id, library_type)
            all_tags = await _fetch_all(zot.tags)
        except Exception as e:
            return tool_error(f"Could not fetch tags: {e}")

        if not all_tags:
            return "No tags in library."

        # /tags lists a name once per type (manual and automatic).
        sorted_tags = sorted(set(all_tags), key=str.casefold)

        total = len(sorted_tags)
        sorted_tags = sorted_tags[:limit]

        return f"Tags ({total} total, showing {len(sorted_tags)}):\n" + "\n".join(sorted_tags)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_tags(
        tags: list[str],
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Delete tags from the entire Zotero library. This removes the tags from all items.

        Args:
            tags: List of tag names to delete from the library
        """
        try:
            tags = _normalize_tags(tags)
            if any("||" in tag for tag in tags):
                # Pyzotero joins tags with " || ", the API's OR operator, so
                # "a || b" would delete the tags "a" and "b" instead.
                raise ValueError("Tags containing '||' cannot be deleted through this tool")
            zot = _get_zot(library_id, library_type)
            await _zot_call(zot.delete_tags, *tags)
        except Exception as e:
            return tool_error(f"Failed to delete tags: {e}")

        return f"Deleted {len(tags)} tag(s) from library: {', '.join(tags)}"

    @mcp.tool(annotations=WRITE)
    async def add_tags(
        item_key: str,
        tags: list[str],
        color: str | None = None,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
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
            zot = _get_zot(library_id, library_type)
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

        data = item.get("data", {})
        title = data.get("title", item_key)
        current = data.get("tags", [])
        present = {tag.get("tag") for tag in current}
        new_tags = current + [{"tag": tag} for tag in tags if tag not in present]

        try:
            # Partial PATCH: works for trashed items, unlike update_item.
            await _zot_call(
                _patch_item, zot, data.get("key", item_key), int(data.get("version", item.get("version"))),
                {"tags": new_tags},
            )
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
    async def remove_tags(
        item_key: str,
        tags: list[str],
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Remove one or more tags from a Zotero item.

        Args:
            item_key: The Zotero item key
            tags: List of tags to remove
        """
        try:
            tags = _normalize_tags(tags)
            zot = _get_zot(library_id, library_type)
        except Exception as exc:
            return tool_error(f"Invalid tag request: {exc}")

        try:
            item = await _zot_call(zot.item, item_key)
        except Exception as e:
            return tool_error(f"Could not find item {item_key}: {e}")

        data = item.get("data", {})
        title = data.get("title", item_key)
        current_tags = data.get("tags", [])
        # Exact matches first; fall back to case-insensitive only when a
        # requested tag has no exact match on this item.
        names = {t.get("tag", "") for t in current_tags}
        exact = {tag for tag in tags if tag in names}
        loose = {tag.casefold() for tag in tags if tag not in names}
        new_tags = [
            t for t in current_tags
            if t.get("tag", "") not in exact and t.get("tag", "").casefold() not in loose
        ]

        removed_count = len(current_tags) - len(new_tags)
        if removed_count == 0:
            return f"None of the specified tags were found on '{title}'."

        try:
            await _zot_call(
                _patch_item, zot, data.get("key", item_key), int(data.get("version", item.get("version"))),
                {"tags": new_tags},
            )
        except Exception as e:
            return tool_error(f"Failed to update item: {e}")

        return f"Removed {removed_count} tag(s) from '{title}'."

    @mcp.tool(annotations=WRITE)
    async def set_tag_color(
        tag: str,
        color: str,
        position: int = 0,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
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
            zot = _get_zot(library_id, library_type)
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
    async def rename_tag(
        old_name: str,
        new_name: str,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
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
        try:
            zot = _get_zot(library_id, library_type)
            items = await _fetch_all(zot.items, tag=old_name)
        except Exception as e:
            return tool_error(f"Could not search for tag '{old_name}': {e}")

        if not items:
            return f"No items found with tag '{old_name}'."

        changed_items = []
        for item in items:
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

            if found:
                data["tags"] = new_tags
                changed_items.append(data)

        errors = []
        updated = 0
        # Batch writes (pyzotero sends 50 per request) instead of one per item.
        for offset in range(0, len(changed_items), 50):
            batch = changed_items[offset:offset + 50]
            try:
                await _zot_call(zot.update_items, batch)
                # Zotero reports per-item failures (e.g. version conflicts)
                # inside a 200 response, which pyzotero does not raise on.
                failed = zot.request.json().get("failed", {})
                updated += len(batch) - len(failed)
                errors.extend(
                    f"{batch[int(index)].get('key', '?')}: {failure.get('message', failure)}"
                    for index, failure in failed.items()
                )
            except Exception as e:
                errors.append(f"items {offset + 1}-{offset + len(batch)}: {sanitize_error_message(str(e))}")

        try:
            colors, version = await _get_tag_colors(zot) if updated else ([], 0)
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
            errors.append(f"tag color: {sanitize_error_message(str(exc))}")

        result = f"Renamed tag '{old_name}' → '{new_name}' on {updated} item(s)."
        if errors:
            result += f"\nErrors ({len(errors)}): " + "; ".join(errors[:5])
        return result

    @mcp.tool(annotations=WRITE)
    async def unset_tag_color(
        tag: str,
        library_id: str | None = None,
        library_type: Literal["user", "group"] | None = None,
    ) -> str:
        """Remove a tag's assigned library color without deleting the tag.
        """

        tag = tag.strip()
        if not tag:
            return tool_error("tag must not be empty")
        try:
            zot = _get_zot(library_id, library_type)
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
            if remaining:
                await _put_tag_colors(zot, remaining, version)
            else:
                await _delete_tag_colors(zot, version)
            return f"Removed color from tag '{tag}'."
        except Exception as exc:
            return tool_error(f"Failed to remove tag color: {exc}")
