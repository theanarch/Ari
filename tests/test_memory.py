"""Integration tests for the ari-memory server against a real SQL Server.

They run the repository's own sql/setup.sql with test values (and a non-default schema), then connect as the
restricted login it creates, exactly as the server does in production. Skipped unless ARI_MEMORY_TEST_SERVER and ARI_MEMORY_TEST_SA_PASSWORD are set, e.g.:

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

SETUP_SQL = Path(__file__).resolve().parent.parent / "sql" / "setup.sql"
DATABASE = "AriTest"
LOGIN = "ari_test_agent"
AI_PASSWORD = "Test_M3mory!Pass"
SCHEMA = "agent"
SETUP_VALUES = {"DatabaseName": DATABASE, "LoginName": LOGIN, "LoginPassword": AI_PASSWORD, "SchemaName": SCHEMA}
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


def render_sqlcmd(script: str, values: dict[str, str]) -> str:
    """Do what sqlcmd does with :setvar lines and $(Name) references, with our values taking precedence."""
    variables = dict(re.findall(r'^:setvar\s+(\w+)\s+"([^"]*)"\s*$', script, flags=re.MULTILINE))
    variables.update(values)
    script = re.sub(r"^:setvar.*$", "", script, flags=re.MULTILINE)
    return re.sub(r"\$\((\w+)\)", lambda m: variables[m.group(1)], script)


def run_script(conn, script: str) -> None:
    """Run a .sql file the way SSMS does: one batch per GO separator."""
    cursor = conn.cursor()
    for batch in re.split(r"^\s*GO\s*$", script, flags=re.MULTILINE | re.IGNORECASE):
        if batch.strip():
            cursor.execute(batch)
    cursor.close()


def ai_connection_string() -> str:
    return f"Server={SERVER};Database={DATABASE};UID={LOGIN};PWD={AI_PASSWORD};{TLS}"


@pytest.fixture(scope="module")
def database():
    conn = admin_connection()
    conn.cursor().execute("SET QUOTED_IDENTIFIER OFF")  # sqlcmd's default; the script must cope with it.
    run_script(conn, render_sqlcmd(SETUP_SQL.read_text(), SETUP_VALUES))
    yield conn
    conn.close()


@pytest.fixture
def admin(database):
    """Admin cursor on an empty schema, for inspecting data behind the AI's back."""
    cursor = database.cursor()
    cursor.execute(f"USE {DATABASE}")
    cursor.execute("SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID(?)", (SCHEMA,))
    for (name,) in cursor.fetchall():
        cursor.execute(f"DROP TABLE {SCHEMA}.[{name}]")
    yield cursor
    cursor.close()


@pytest.fixture
async def client(admin):
    store = TableStore(ai_connection_string(), max_deletes=2, schema=SCHEMA)
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
    admin.execute(f"SELECT COUNT(*) FROM {SCHEMA}.[{table}] WHERE DeletedAt IS NULL")
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
    admin.execute("SELECT Title FROM agent.Tasks ORDER BY Id")
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
        ("query_rows", {"table": "Tasks", "where": {"Title]; DROP TABLE agent.Tasks; --": "x"}}, "has no column"),
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
    admin.execute("SELECT COUNT(*), COUNT(DeletedAt) FROM agent.Tasks")
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
    evil = "'); DROP TABLE agent.Tasks; --"
    await insert(tasks, Title=evil)
    admin.execute("SELECT Title FROM agent.Tasks")
    assert admin.fetchall()[0][0] == evil


# --- Guardrails in the database itself ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM agent.Tasks",
        "TRUNCATE TABLE agent.Tasks",
        "DROP TABLE agent.Tasks",
        "ALTER TABLE agent.Tasks ADD Extra INT",
        "CREATE TABLE agent.Sneaky (Id INT)",
        "CREATE TABLE dbo.Sneaky (Id INT)",
        "DROP PROCEDURE agent.CreateTable",
        "ALTER SCHEMA dbo TRANSFER agent.Tasks",
        "DROP DATABASE AriTest",
    ],
)
async def test_ai_login_cannot_destroy_anything(tasks: Client, admin, statement: str):
    """Even with raw SQL, bypassing the server code entirely, the AI login is refused."""
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
        ("x]; DROP TABLE agent.Tasks; --", [{"name": "A", "type": "text"}]),
        ("Ok", [{"name": "A] INT); DROP TABLE agent.Tasks; --", "type": "text"}]),
        ("Ok", [{"name": "A", "type": "text); DROP TABLE agent.Tasks; --"}]),
    ],
)
async def test_create_table_procedure_rejects_injection(tasks: Client, admin, name: str, columns: list):
    """Calling CreateTable directly as the AI login, without the Python checks, still can't inject SQL."""
    await insert(tasks, Title="survives")
    conn = mssql_python.connect(ai_connection_string(), autocommit=True)
    try:
        with pytest.raises(mssql_python.Error):
            conn.cursor().execute("EXEC agent.CreateTable ?, ?", (name, json.dumps(columns)))
    finally:
        conn.close()
    assert active_count(admin) == 1


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({}, "Choose your own"),
        ({"LoginPassword": "Real_Pass!123"}, "Choose your own"),
        ({"DatabaseName": "Bad Name; DROP", "LoginName": "x", "LoginPassword": "Real_Pass!123"}, "may only contain"),
    ],
)
def test_setup_script_refuses_placeholders_and_bad_names(database, values: dict, message: str):
    """Someone who runs setup.sql without choosing values gets an error, and nothing is changed."""
    cursor = database.cursor()
    with pytest.raises(mssql_python.Error, match=message):
        run_script(database, render_sqlcmd(SETUP_SQL.read_text(), values))
    cursor.execute("SET NOEXEC OFF")  # The script stops by switching execution off for the rest of the session.
    cursor.execute("SELECT DB_ID(N'CHANGE_ME_database'), DB_ID(N'Bad Name; DROP')")
    assert tuple(cursor.fetchall()[0]) == (None, None)
    conn = mssql_python.connect(ai_connection_string())  # The test login and password are untouched.
    conn.close()


def test_setup_script_without_sqlcmd_mode_changes_nothing(database):
    """In SSMS without SQLCMD mode, :setvar is a syntax error and $(Name) stays literal text. Every batch still
    runs (SSMS carries on after an error), so the guard has to stop the rest of the script on its own."""
    cursor = database.cursor()
    errors = []
    for batch in re.split(r"^\s*GO\s*$", SETUP_SQL.read_text(), flags=re.MULTILINE | re.IGNORECASE):
        if batch.strip():
            try:
                cursor.execute(batch)
            except mssql_python.Error as exc:
                errors.append(str(exc))
    cursor.execute("SET NOEXEC OFF")
    assert any("Turn on SQLCMD mode" in e for e in errors)
    cursor.execute("SELECT DB_ID(N'$(DatabaseName)'), SUSER_ID(N'$(LoginName)')")
    assert tuple(cursor.fetchall()[0]) == (None, None)
