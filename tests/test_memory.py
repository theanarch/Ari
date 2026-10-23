"""Integration tests for the ArisMemory server against a real SQL Server.

They run the repository's own SQL scripts, then connect as the restricted ari_mcp login, exactly as the
server does in production. Skipped unless ARI_MEMORY_TEST_SERVER and ARI_MEMORY_TEST_SA_PASSWORD are set, e.g.:

    docker run -d -p 1433:1433 -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD='Str0ng!Passw0rd' \\
        mcr.microsoft.com/mssql/server:2022-latest
    ARI_MEMORY_TEST_SERVER=localhost,1433 ARI_MEMORY_TEST_SA_PASSWORD='Str0ng!Passw0rd' uv run pytest
"""

import json
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
    from ari_mcp.memory_store import TableStore

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
AI_PASSWORD = "Test_M3mory!Pass"
TLS = "Encrypt=yes;TrustServerCertificate=yes"

TASKS = [
    {"name": "Title", "type": "text", "required": True},
    {"name": "Notes", "type": "long_text"},
    {"name": "Priority", "type": "integer"},
    {"name": "Done", "type": "boolean"},
    {"name": "Due", "type": "date"},
]


def admin_connection():
    conn_str = f"Server={SERVER};Database=master;UID=sa;PWD={SA_PASSWORD};{TLS}"
    for attempt in range(30):  # A freshly started SQL Server container needs a few seconds.
        try:
            return mssql_python.connect(conn_str, autocommit=True)
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
    cursor.close()


def ai_connection_string() -> str:
    return f"Server={SERVER};Database=ArisMemory;UID=ari_mcp;PWD={AI_PASSWORD};{TLS}"


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
    """Admin cursor on an empty "ai" schema, for inspecting data behind the AI's back."""
    cursor = database.cursor()
    cursor.execute("USE ArisMemory")
    cursor.execute("SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID('ai')")
    for (name,) in cursor.fetchall():
        cursor.execute(f"DROP TABLE ai.[{name}]")
    yield cursor
    cursor.close()


@pytest.fixture
async def client(admin):
    store = TableStore(ai_connection_string(), max_deletes=2)
    async with Client(create_memory_server(store)) as c:
        yield c


@pytest.fixture
async def tasks(client):
    """A Tasks table created through the MCP tool, as the AI would."""
    result = await client.call_tool("create_table", {"table": "Tasks", "columns": TASKS})
    assert not result.is_error, result.content[0].text
    return client


async def insert(client, **values):
    result = await client.call_tool("insert_row", {"table": "Tasks", "values": values})
    assert not result.is_error, result.content[0].text
    return result.structured_content["row"]


def active_count(admin, table="Tasks") -> int:
    admin.execute(f"SELECT COUNT(*) FROM ai.[{table}] WHERE DeletedAt IS NULL")
    return admin.fetchall()[0][0]


# --- The tools work -------------------------------------------------------------------------------------------


async def test_tools_and_their_hints(client: Client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"list_tables", "create_table", "query_rows", "insert_row", "update_row", "delete_row"}
    assert tools["list_tables"].annotations.read_only_hint is True
    assert tools["query_rows"].annotations.read_only_hint is True
    assert tools["create_table"].annotations.destructive_hint is False
    assert tools["insert_row"].annotations.destructive_hint is False
    assert tools["update_row"].annotations.destructive_hint is True
    assert tools["delete_row"].annotations.destructive_hint is True


async def test_create_table_and_list_it(tasks: Client):
    tables = (await tasks.call_tool("list_tables", {})).structured_content["tables"]
    assert tables == [
        {
            "name": "Tasks",
            "columns": [
                {"name": "Title", "type": "text", "required": True},
                {"name": "Notes", "type": "long_text", "required": False},
                {"name": "Priority", "type": "integer", "required": False},
                {"name": "Done", "type": "boolean", "required": False},
                {"name": "Due", "type": "date", "required": False},
            ],
        }
    ]


async def test_insert_and_query(tasks: Client):
    row = await insert(tasks, Title="Skriv CV", Priority=1, Done=False, Due="2026-11-01")
    assert row["Id"] == 1
    assert row["Due"] == "2026-11-01"
    await insert(tasks, Title="Ring banken", Notes="Fråga om räntan", Priority=2)

    by_priority = await tasks.call_tool("query_rows", {"table": "Tasks", "where": {"Priority": 2}})
    assert [r["Title"] for r in by_priority.structured_content["rows"]] == ["Ring banken"]

    by_text = await tasks.call_tool("query_rows", {"table": "Tasks", "contains": "räntan"})
    assert [r["Title"] for r in by_text.structured_content["rows"]] == ["Ring banken"]

    newest_first = await tasks.call_tool("query_rows", {"table": "tasks", "limit": 1})
    assert [r["Title"] for r in newest_first.structured_content["rows"]] == ["Ring banken"]


async def test_query_treats_wildcards_as_text(tasks: Client):
    await insert(tasks, Title="100% klart")
    await insert(tasks, Title="100 procent")
    rows = (await tasks.call_tool("query_rows", {"table": "Tasks", "contains": "100%"})).structured_content["rows"]
    assert [r["Title"] for r in rows] == ["100% klart"]


async def test_update_changes_one_row(tasks: Client, admin):
    await insert(tasks, Title="old")
    await insert(tasks, Title="keep")
    result = await tasks.call_tool("update_row", {"table": "Tasks", "row_id": 1, "values": {"Title": "new", "Done": True}})
    row = result.structured_content["row"]
    assert (row["Title"], row["Done"]) == ("new", True)
    assert row["UpdatedAt"] is not None
    admin.execute("SELECT Title FROM ai.Tasks ORDER BY Id")
    assert [r[0] for r in admin.fetchall()] == ["new", "keep"]


# --- Guardrails in the code -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args", "message"),
    [
        ("insert_row", {"table": "Nope", "values": {"Title": "x"}}, "No table named"),
        ("insert_row", {"table": "Tasks", "values": {"Nope": "x"}}, "has no column"),
        ("insert_row", {"table": "Tasks", "values": {"Id": 5, "Title": "x"}}, "managed automatically"),
        ("insert_row", {"table": "Tasks", "values": {"DeletedAt": None, "Title": "x"}}, "managed automatically"),
        ("update_row", {"table": "Tasks", "row_id": 99, "values": {"Title": "x"}}, "No active row"),
        ("query_rows", {"table": "Tasks", "where": {"Title]; DROP TABLE ai.Tasks; --": "x"}}, "has no column"),
    ],
)
async def test_unknown_names_and_ids_are_refused(tasks: Client, admin, tool: str, args: dict, message: str):
    await insert(tasks, Title="a")
    result = await tasks.call_tool(tool, args)
    assert result.is_error
    assert message in result.content[0].text
    assert active_count(admin) == 1


async def test_delete_is_soft_and_recoverable(tasks: Client, admin):
    await insert(tasks, Title="forget")
    await insert(tasks, Title="keep")
    assert not (await tasks.call_tool("delete_row", {"table": "Tasks", "row_id": 1})).is_error

    rows = (await tasks.call_tool("query_rows", {"table": "Tasks"})).structured_content["rows"]
    assert [r["Title"] for r in rows] == ["keep"]
    admin.execute("SELECT COUNT(*), COUNT(DeletedAt) FROM ai.Tasks")
    assert tuple(admin.fetchall()[0]) == (2, 1)
    # A deleted row can't be deleted or edited again.
    assert (await tasks.call_tool("delete_row", {"table": "Tasks", "row_id": 1})).is_error
    assert (await tasks.call_tool("update_row", {"table": "Tasks", "row_id": 1, "values": {"Title": "x"}})).is_error


async def test_delete_limit_per_session(tasks: Client, admin):
    for title in ("1", "2", "3"):
        await insert(tasks, Title=title)
    assert not (await tasks.call_tool("delete_row", {"table": "Tasks", "row_id": 1})).is_error
    assert not (await tasks.call_tool("delete_row", {"table": "Tasks", "row_id": 2})).is_error
    third = await tasks.call_tool("delete_row", {"table": "Tasks", "row_id": 3})
    assert third.is_error
    assert "Delete limit reached" in third.content[0].text
    assert active_count(admin) == 1


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"table": "Bad Name", "columns": [{"name": "A", "type": "text"}]}, "pattern"),
        ({"table": "Tasks2", "columns": [{"name": "A", "type": "varchar(max)"}]}, "text"),
        ({"table": "Tasks2", "columns": []}, "at least 1"),
        ({"table": "Tasks2", "columns": [{"name": "Id", "type": "integer"}]}, "added automatically"),
        ({"table": "Tasks2", "columns": [{"name": "A", "type": "text"}, {"name": "A", "type": "integer"}]}, "only be used once"),
        ({"table": "Tasks", "columns": [{"name": "A", "type": "text"}]}, "already exists"),
    ],
)
async def test_create_table_validates_the_design(tasks: Client, args: dict, message: str):
    result = await tasks.call_tool("create_table", args)
    assert result.is_error
    assert message in result.content[0].text


async def test_sql_in_values_is_stored_as_text(tasks: Client, admin):
    evil = "'); DROP TABLE ai.Tasks; --"
    await insert(tasks, Title=evil)
    admin.execute("SELECT Title FROM ai.Tasks")
    assert admin.fetchall()[0][0] == evil


# --- Guardrails in the database itself ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM ai.Tasks",
        "TRUNCATE TABLE ai.Tasks",
        "DROP TABLE ai.Tasks",
        "ALTER TABLE ai.Tasks ADD Extra INT",
        "CREATE TABLE ai.Sneaky (Id INT)",
        "CREATE TABLE dbo.Sneaky (Id INT)",
        "DROP PROCEDURE ai.CreateTable",
        "ALTER SCHEMA dbo TRANSFER ai.Tasks",
        "DROP DATABASE ArisMemory",
    ],
)
async def test_ai_login_cannot_destroy_anything(tasks: Client, admin, statement: str):
    """Even with raw SQL, bypassing the server code entirely, the ari_mcp login is refused."""
    await insert(tasks, Title="survives")
    conn = mssql_python.connect(ai_connection_string(), autocommit=True)
    try:
        with pytest.raises(mssql_python.Error):
            conn.cursor().execute(statement)
    finally:
        conn.close()
    assert active_count(admin) == 1


@pytest.mark.parametrize(
    ("name", "columns"),
    [
        ("x]; DROP TABLE ai.Tasks; --", [{"name": "A", "type": "text"}]),
        ("Ok", [{"name": "A] INT); DROP TABLE ai.Tasks; --", "type": "text"}]),
        ("Ok", [{"name": "A", "type": "text); DROP TABLE ai.Tasks; --"}]),
    ],
)
async def test_create_table_procedure_rejects_injection(tasks: Client, admin, name: str, columns: list):
    """Calling ai.CreateTable directly as ari_mcp, without the Python checks, still can't inject SQL."""
    await insert(tasks, Title="survives")
    conn = mssql_python.connect(ai_connection_string(), autocommit=True)
    try:
        with pytest.raises(mssql_python.Error):
            conn.cursor().execute("EXEC ai.CreateTable ?, ?", (name, json.dumps(columns)))
    finally:
        conn.close()
    assert active_count(admin) == 1
