"""Integration tests for the ArisMemory server against a real SQL Server.

They run the repository's own SQL scripts, then connect as the restricted ari_mcp login, exactly as the
server does in production. Skipped unless ARI_MEMORY_TEST_SERVER and ARI_MEMORY_TEST_SA_PASSWORD are set, e.g.:

    docker run -d -p 1433:1433 -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD='Str0ng!Passw0rd' \\
        mcr.microsoft.com/mssql/server:2022-latest
    ARI_MEMORY_TEST_SERVER=localhost,1433 ARI_MEMORY_TEST_SA_PASSWORD='Str0ng!Passw0rd' uv run pytest
"""

import os
import re
import time
from pathlib import Path

import pytest
from mcp import Client

SERVER = os.environ.get("ARI_MEMORY_TEST_SERVER")
SA_PASSWORD = os.environ.get("ARI_MEMORY_TEST_SA_PASSWORD")

pytestmark = [
    pytest.mark.skipif(not (SERVER and SA_PASSWORD), reason="no test SQL Server configured"),
    pytest.mark.anyio,
]

if SERVER and SA_PASSWORD:
    import mssql_python

    from ari_mcp.memory_server import create_memory_server
    from ari_mcp.memory_store import MemoryStore

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
AI_PASSWORD = "Test_M3mory!Pass"
TLS = "Encrypt=yes;TrustServerCertificate=yes"


def admin_connection(autocommit: bool = True):
    conn_str = f"Server={SERVER};Database=master;UID=sa;PWD={SA_PASSWORD};{TLS}"
    for attempt in range(30):  # A freshly started SQL Server container needs a few seconds.
        try:
            return mssql_python.connect(conn_str, autocommit=autocommit)
        except mssql_python.Error:
            if attempt == 29:
                raise
            time.sleep(2)


def run_script(conn, script: str) -> None:
    """Run a .sql file the way SSMS does: one batch per GO separator."""
    cursor = conn.cursor()
    for batch in re.split(r"^\s*GO\s*$", script, flags=re.MULTILINE | re.IGNORECASE):
        if batch.strip():
            cursor.execute(batch)


@pytest.fixture(scope="module")
def database():
    conn = admin_connection()
    run_script(conn, (SQL_DIR / "01_create_database.sql").read_text())
    login_script = (SQL_DIR / "02_create_ai_login.sql").read_text().replace("CHANGE_ME_Str0ng!Pass", AI_PASSWORD)
    run_script(conn, login_script)
    yield conn
    conn.close()


@pytest.fixture
def admin(database):
    """Admin connection with an empty table, for setting up and inspecting data behind the AI's back."""
    cursor = database.cursor()
    cursor.execute("TRUNCATE TABLE ArisMemory.dbo.Memories")  # Also restarts the ids at 1.
    yield cursor
    cursor.close()


def ai_connection_string() -> str:
    return f"Server={SERVER};Database=ArisMemory;UID=ari_mcp;PWD={AI_PASSWORD};{TLS}"


@pytest.fixture
async def client(admin):
    store = MemoryStore(ai_connection_string(), max_deletes=2)
    async with Client(create_memory_server(store)) as c:
        yield c


def seed(admin, *rows: tuple[str, str]) -> None:
    for topic, content in rows:
        admin.execute("INSERT INTO ArisMemory.dbo.Memories (Topic, Content) VALUES (?, ?)", (topic, content))


# --- The tools work -------------------------------------------------------------------------------------------


async def test_tools_and_their_hints(client: Client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"search_memories", "add_memory", "update_memory", "delete_memory"}
    assert tools["search_memories"].annotations.read_only_hint is True
    assert tools["add_memory"].annotations.destructive_hint is False
    assert tools["update_memory"].annotations.destructive_hint is True
    assert tools["delete_memory"].annotations.destructive_hint is True


async def test_add_then_search(client: Client):
    added = await client.call_tool("add_memory", {"topic": "preferences", "content": "Boris föredrar svenska svar."})
    assert not added.is_error
    assert added.structured_content["id"] == 1

    found = (await client.call_tool("search_memories", {"text": "svenska"})).structured_content["memories"]
    assert [m["content"] for m in found] == ["Boris föredrar svenska svar."]


async def test_search_by_topic_newest_first_and_limited(client: Client, admin):
    seed(admin, ("work", "first"), ("work", "second"), ("home", "third"))
    found = (await client.call_tool("search_memories", {"topic": "work", "limit": 1})).structured_content["memories"]
    assert [m["content"] for m in found] == ["second"]


async def test_search_treats_wildcards_as_text(client: Client, admin):
    seed(admin, ("stats", "100% sure"), ("stats", "100 percent"))
    found = (await client.call_tool("search_memories", {"text": "100%"})).structured_content["memories"]
    assert [m["content"] for m in found] == ["100% sure"]


async def test_update_changes_one_memory(client: Client, admin):
    seed(admin, ("work", "old text"), ("work", "keep me"))
    updated = (await client.call_tool("update_memory", {"memory_id": 1, "content": "new text"})).structured_content
    assert updated["content"] == "new text"
    assert updated["topic"] == "work"
    assert updated["updated_at"] is not None
    admin.execute("SELECT Content FROM ArisMemory.dbo.Memories ORDER BY Id")
    assert [r[0] for r in admin.fetchall()] == ["new text", "keep me"]


# --- Guardrails in the code -----------------------------------------------------------------------------------


async def test_unknown_id_is_a_readable_error(client: Client):
    result = await client.call_tool("update_memory", {"memory_id": 99, "content": "x"})
    assert result.is_error
    assert "No active memory with that id" in result.content[0].text


async def test_delete_is_soft_and_recoverable(client: Client, admin):
    seed(admin, ("work", "to forget"), ("work", "to keep"))
    assert not (await client.call_tool("delete_memory", {"memory_id": 1})).is_error

    found = (await client.call_tool("search_memories", {})).structured_content["memories"]
    assert [m["content"] for m in found] == ["to keep"]
    # The row is still in the table, only marked as deleted, so a human can restore it.
    admin.execute("SELECT COUNT(*), COUNT(DeletedAt) FROM ArisMemory.dbo.Memories")
    assert tuple(admin.fetchone()) == (2, 1)
    # A deleted memory can't be deleted or edited again.
    assert (await client.call_tool("delete_memory", {"memory_id": 1})).is_error
    assert (await client.call_tool("update_memory", {"memory_id": 1, "content": "x"})).is_error


async def test_delete_limit_per_session(client: Client, admin):
    seed(admin, ("a", "1"), ("a", "2"), ("a", "3"))
    assert not (await client.call_tool("delete_memory", {"memory_id": 1})).is_error
    assert not (await client.call_tool("delete_memory", {"memory_id": 2})).is_error
    third = await client.call_tool("delete_memory", {"memory_id": 3})
    assert third.is_error
    assert "Delete limit reached" in third.content[0].text
    admin.execute("SELECT COUNT(*) FROM ArisMemory.dbo.Memories WHERE DeletedAt IS NULL")
    assert admin.fetchone()[0] == 1


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("add_memory", {"topic": "x", "content": "a" * 4001}),
        ("add_memory", {"topic": "", "content": "a"}),
        ("search_memories", {"limit": 51}),
        ("delete_memory", {"memory_id": 0}),
        ("delete_memory", {"memory_id": "1; DROP TABLE dbo.Memories"}),
    ],
)
async def test_invalid_arguments_are_rejected_before_sql(client: Client, admin, tool: str, args: dict):
    seed(admin, ("a", "1"))
    assert (await client.call_tool(tool, args)).is_error
    admin.execute("SELECT COUNT(*) FROM ArisMemory.dbo.Memories WHERE DeletedAt IS NULL")
    assert admin.fetchone()[0] == 1


async def test_sql_in_text_is_stored_as_text(client: Client, admin):
    evil = "'); DROP TABLE dbo.Memories; --"
    assert not (await client.call_tool("add_memory", {"topic": "test", "content": evil})).is_error
    admin.execute("SELECT Content FROM ArisMemory.dbo.Memories")
    assert admin.fetchone()[0] == evil


# --- Guardrails in the database itself ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM dbo.Memories",
        "TRUNCATE TABLE dbo.Memories",
        "DROP TABLE dbo.Memories",
        "ALTER TABLE dbo.Memories ADD Extra INT",
        "CREATE TABLE dbo.Other (Id INT)",
        "UPDATE dbo.Memories SET Id = 5",
        "DROP DATABASE ArisMemory",
    ],
)
def test_ai_login_cannot_destroy_anything(admin, statement: str):
    """Even with raw SQL, bypassing the server code entirely, the ari_mcp login is refused."""
    seed(admin, ("a", "1"))
    conn = mssql_python.connect(ai_connection_string(), autocommit=True)
    try:
        with pytest.raises(mssql_python.Error):
            conn.cursor().execute(statement)
    finally:
        conn.close()
    admin.execute("SELECT COUNT(*) FROM ArisMemory.dbo.Memories")
    assert admin.fetchone()[0] == 1
