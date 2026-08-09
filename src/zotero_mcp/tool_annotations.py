"""Shared MCP tool annotations for client safety and approval UX."""

from mcp.types import ToolAnnotations


READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

READ_ONLY_OPEN_WORLD = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    open_world_hint=False,
)

WRITE_OPEN_WORLD = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    open_world_hint=True,
)

DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    open_world_hint=False,
)
