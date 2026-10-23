"""The ArisMemory MCP server: lets an AI agent save and look up notes in a SQL Server table, within guardrails."""

from __future__ import annotations

import argparse
import os

import mssql_python
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from .memory_store import DEFAULT_MAX_DELETES, MemoryRow, MemoryStore, MemoryStoreError

CONNECTION_ENV_VAR = "ARI_MEMORY_CONNECTION_STRING"
MAX_DELETES_ENV_VAR = "ARI_MEMORY_MAX_DELETES"

# Hints to the host about what each tool does. Hosts can use destructive_hint to ask the user before running it.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ADDS_DATA = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
CHANGES_DATA = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

Topic = Field(min_length=1, max_length=100, description="Short category, e.g. 'preferences' or 'project-ari'.")
Content = Field(min_length=1, max_length=4000, description="The text to remember.")


class Memory(BaseModel):
    id: int
    topic: str
    content: str
    created_at: str
    updated_at: str | None


class MemoryList(BaseModel):
    memories: list[Memory]


def _to_model(row: MemoryRow) -> Memory:
    return Memory(
        id=row.id,
        topic=row.topic,
        content=row.content,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


def create_memory_server(store: MemoryStore) -> MCPServer:
    mcp = MCPServer(
        name="ari-memory",
        title="Ari memory",
        version="0.1.0",
        instructions=(
            "A small long-term memory in SQL Server. Use add_memory to save facts the user wants remembered, "
            "search_memories before answering questions about earlier conversations, and update_memory or "
            "delete_memory only for the specific memory the user asks about. Each change affects exactly one "
            "memory, and deletions are limited per session."
        ),
    )

    @mcp.tool(annotations=READ_ONLY)
    def search_memories(
        text: str = Field(default="", max_length=200),
        topic: str | None = Field(default=None, max_length=100),
        limit: int = Field(default=20, ge=1, le=50),
    ) -> MemoryList:
        """Find saved memories, newest first.

        Args:
            text: Words to look for in the topic or content. Empty returns the latest memories.
            topic: Only return memories with exactly this topic.
            limit: Maximum number of memories to return (at most 50).
        """
        return MemoryList(memories=[_to_model(row) for row in _run(store.search, text, topic, limit)])

    @mcp.tool(annotations=ADDS_DATA)
    def add_memory(topic: str = Topic, content: str = Content) -> Memory:
        """Save one new memory.

        Args:
            topic: Short category for the memory.
            content: The text to remember.
        """
        return _to_model(_run(store.add, topic, content))

    @mcp.tool(annotations=CHANGES_DATA)
    def update_memory(
        memory_id: int = Field(ge=1),
        content: str = Content,
        topic: str | None = Field(default=None, min_length=1, max_length=100),
    ) -> Memory:
        """Replace the content (and optionally the topic) of one existing memory.

        Args:
            memory_id: The id of the memory, from search_memories.
            content: The new text.
            topic: New topic, or leave out to keep the current one.
        """
        return _to_model(_run(store.update, memory_id, content, topic))

    @mcp.tool(annotations=CHANGES_DATA)
    def delete_memory(memory_id: int = Field(ge=1)) -> Memory:
        """Delete one memory by id. Only delete what the user explicitly asked to remove.

        Args:
            memory_id: The id of the memory, from search_memories.
        """
        return _to_model(_run(store.delete, memory_id))

    return mcp


def _run(operation, *args):
    """Call the store and turn expected failures into tool errors the model can read."""
    try:
        return operation(*args)
    except MemoryStoreError as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for a small AI memory in SQL Server.")
    parser.add_argument(
        "--connection-string",
        default=os.environ.get(CONNECTION_ENV_VAR),
        help=f"SQL Server connection string (or set {CONNECTION_ENV_VAR}).",
    )
    parser.add_argument(
        "--max-deletes",
        type=int,
        default=int(os.environ.get(MAX_DELETES_ENV_VAR, DEFAULT_MAX_DELETES)),
        help="Maximum number of deletions per session.",
    )
    args = parser.parse_args()
    if not args.connection_string:
        parser.error(f"Give a connection string with --connection-string or the {CONNECTION_ENV_VAR} variable.")

    store = MemoryStore(args.connection_string, max_deletes=args.max_deletes)
    try:
        store.check_connection()
    except mssql_python.Error as exc:
        # stderr ends up in the host's MCP log (Claude Desktop: Settings > Developer > Open Logs Folder).
        parser.exit(1, f"Cannot reach dbo.Memories in SQL Server: {exc}\n")
    create_memory_server(store).run("stdio")


if __name__ == "__main__":
    main()
