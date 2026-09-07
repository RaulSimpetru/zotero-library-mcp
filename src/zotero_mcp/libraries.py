"""Discover the personal and shared libraries available to the configured key."""

from ._helpers import (
    _get_zot,
    _total_results,
    _validate_limit,
    _validate_start,
    _zot_call,
)
from .responses import tool_error
from .tool_annotations import READ_ONLY


def register(mcp):
    @mcp.tool(annotations=READ_ONLY)
    async def list_libraries(limit: int = 100, start: int = 0) -> dict[str, object]:
        """List the key owner's personal library and a page of shared group libraries.

        Use the returned library_id and library_type on subsequent tool calls.
        This does not change the default library. Pagination applies to groups;
        the personal library is included on each page. Key permissions are not a
        guarantee of group membership rights; Zotero enforces access on each call.
        """
        try:
            limit = _validate_limit(limit)
            start = _validate_start(start)
            configured = _get_zot()
            info = await _zot_call(configured.key_info)
            # A group-configured server still discovers groups for the key owner.
            user_id = str(info["userID"])
            user = _get_zot(user_id, "user")
            groups = await _zot_call(user.groups, limit=limit, start=start)
            total = _total_results(user, start + len(groups))
            access = info.get("access", {})
            group_access = access.get("groups", {})
            default = {
                "library_id": str(configured.library_id),
                "library_type": str(configured.library_type).rstrip("s"),
            }

            def entry(library_id, library_type, name, permissions):
                return {
                    "library_id": library_id,
                    "library_type": library_type,
                    "name": name,
                    "is_default": default
                    == {"library_id": library_id, "library_type": library_type},
                    "key_permissions": {
                        field: permissions.get(field, False)
                        for field in ("library", "write", "files", "notes")
                    },
                }

            libraries = [entry(user_id, "user", "My Library", access.get("user", {}))]
            for group in groups:
                data = group["data"]
                group_id = str(group["id"])
                permissions = group_access.get(group_id, group_access.get("all", {}))
                libraries.append(entry(group_id, "group", data["name"], permissions))
            return {
                "default_library": default,
                "libraries": libraries,
                "group_total": total,
                "start": start,
                "next_start": start + len(groups)
                if groups and start + len(groups) < total
                else None,
            }
        except Exception as exc:
            return tool_error(f"Could not list Zotero libraries: {exc}")
