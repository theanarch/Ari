# Ari – MCP servers for documents and memory

Ari is two small [Model Context Protocol](https://modelcontextprotocol.io) (MCP) servers written in Python with
the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (v2). Each one gives an AI
agent a narrow, well-guarded capability:

| Server | Command | What the agent can do |
| --- | --- | --- |
| **ari-documents** | `ari-mcp` | Read and search one folder of public documents. Nothing else. |
| **ari-memory** | `ari-memory` | Create tables in SQL Server and read and write their rows, one row at a time. |

Both follow the same idea: the agent gets a few specific tools instead of general access, and every limit is
enforced in code (and, for the database, in SQL Server itself) rather than left to the model.

## ari-documents: read-only document access

| Tool | What it does |
| --- | --- |
| `list_documents` | Lists the readable documents in the folder (optionally one subfolder), recursively. |
| `read_document` | Returns the text of one document, with an optional `max_chars` limit. |
| `search_documents` | Case-insensitive search across all documents; returns file, line and a snippet. |

Supported formats: plain text (`.txt`, `.md`, `.csv`, `.log`) and Word (`.docx`, including tables).
Text files are read as UTF-8 and fall back to Windows-1252, so older Swedish files keep their å, ä and ö.

### Security model

The agent can only read, and only inside the configured folder.

- **Path sandbox** ([`sandbox.py`](src/ari_mcp/sandbox.py)). Every path from the client is joined to the
  document root and fully resolved, which collapses `..` and follows symlinks and Windows junctions. The
  result must still be inside the resolved root, or the call is refused. Absolute paths in any form
  (`C:\…`, `D:file`, `\\server\share`, `\rooted`, `/etc/…`) and NUL bytes are rejected before touching the disk.
- **Listing and search use the same rule.** Hidden files, unsupported file types and files that link outside
  the root are never listed or searched, so a symlink cannot leak content through search results.
- **Read-only by construction.** No tool writes, moves or deletes anything, and each tool advertises
  `readOnlyHint: true` and `openWorldHint: false` so hosts can show that to the user.
- **Bounded work.** Files over 20 MB are refused, `read_document` caps output with `max_chars`, and
  `search_documents` caps results with `max_results`.
- **Errors go to the model, not a stack trace.** Expected failures raise `ToolError`, which the SDK returns as
  a tool result with `isError: true` and a clear message the model can act on.

One thing the server cannot do for you: document *content* is untrusted input to the model. A public document
could contain text that looks like instructions. Hosts should treat tool results as data, which is one more
reason the server stays read-only.

## ari-memory: AI-managed tables in SQL Server, with guardrails

The agent can design its own tables in the `ArisMemory` database and keep data in them, for example a task
list, a reading log or notes about the user. Everything it creates lives in the `ai` schema.

| Tool | What it does | Hint to the host |
| --- | --- | --- |
| `list_tables` | Lists the tables and their columns. | read-only |
| `create_table` | Creates a table from a list of columns (`text`, `long_text`, `integer`, `decimal`, `boolean`, `date`, `datetime`). | additive |
| `query_rows` | Reads rows by exact-match filters and/or text search, newest first, at most 100. | read-only |
| `insert_row` | Adds one row. | additive |
| `update_row` | Changes one row, by `Id`. | destructive |
| `delete_row` | Deletes one row, by `Id`. | destructive |

Every table gets the same frame automatically: an `Id` primary key, the requested columns, and `CreatedAt`,
`UpdatedAt` and `DeletedAt`.

### Guardrails

The goal: the agent can build and fill tables, but can never drop or empty a table, never touch more rows
than the one it was asked about, and nothing it does is unrecoverable. That is enforced in four layers:

1. **No free-form SQL.** The agent names a table, columns and values; it never sends SQL. Every table and column
   name is checked against the database's own catalog (`sys.tables`, `sys.columns`) before it is quoted into a
   statement, and every value is a parameter. Text like `'); DROP TABLE …` is stored as text, and an unknown
   column name is refused with a list of the real ones.
2. **One row per change.** Inserts, updates and deletes affect a single row, chosen by primary key. Each write
   runs in a transaction, and the `OUTPUT` clause reports exactly which rows changed; the transaction commits
   only if that is exactly one row. Reads are capped at 100 rows.
3. **Deletes are soft and capped.** `delete_row` only sets `DeletedAt`; the row stays and can be restored with
   `UPDATE ai.<Table> SET DeletedAt = NULL WHERE Id = …`. Each server session allows at most 5 deletions
   (`--max-deletes` / `ARI_MEMORY_MAX_DELETES`), so a confused agent cannot work through a table row by row.
   There is no tool to drop, rename or alter a table.
4. **Least privilege in SQL Server.** The agent's `ari_mcp` login may only `SELECT`, `INSERT` and `UPDATE` in
   the `ai` schema, is explicitly denied `DELETE`, and has no `CREATE TABLE` or `ALTER` rights at all. Tables are
   created through one stored procedure, [`ai.CreateTable`](sql/01_create_database.sql), which runs with the
   owner's rights (`EXECUTE AS OWNER`), validates names and types itself, and only ever builds a
   `CREATE TABLE ai.[…]` statement from a fixed set of types. The tests connect as `ari_mcp` with raw SQL and
   confirm that `DELETE`, `TRUNCATE`, `DROP TABLE`, `ALTER TABLE`, `CREATE TABLE`, `DROP PROCEDURE`,
   `ALTER SCHEMA … TRANSFER` and `DROP DATABASE` are all refused by SQL Server, and that SQL injected through
   the procedure's arguments is rejected.

Layers 1–3 are what stop mass *updates*; layer 4 is what makes deleting or dropping impossible even if the
Python code were wrong.

The server connects with [mssql-python](https://github.com/microsoft/mssql-python), Microsoft's official
driver, which needs no separate ODBC driver. Setup is covered in [Getting started](#getting-started).

## Getting started

These steps are for Windows, which is where Claude Desktop and SQL Server usually run together. Each server
works on its own, so skip step 4 if you only want the document server.

### 1. Install the prerequisites

- **Python 3.11 or newer** and **[uv](https://docs.astral.sh/uv/)**. Install uv in PowerShell, then open a new
  terminal:
  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
- **[Claude Desktop](https://claude.ai/download)**, or another MCP host (see [Other hosts](#other-hosts-and-tools)).
- For ari-memory only: **SQL Server 2016 or newer** (the free Express or Developer edition is fine) and
  **SSMS** or `sqlcmd` to run the setup scripts.

### 2. Clone and install

```powershell
git clone https://github.com/theanarch/Ari.git
cd Ari
uv sync          # creates .venv and installs mcp, python-docx, mssql-python and the test tools
uv run pytest    # the document server tests; the SQL Server tests are skipped until step 4
```

### 3. Try the document server

Put a few `.txt`, `.md` or `.docx` files in a folder (or use `sample-docs`), then run the demo client, which
starts the server and calls each tool the way an AI host would:

```powershell
uv run python examples/demo_client.py "C:\Store\Dokument" --query budget
```

### 4. Set up the memory database

1. **Allow SQL logins.** In SSMS, right-click the server → *Properties* → *Security* → choose
   *SQL Server and Windows Authentication mode*. Then restart the SQL Server service (SSMS: right-click the
   server → *Restart*, or the *Services* app). Many installations only allow Windows logins by default, and
   the server's restricted login is a SQL login.
2. **Create the database.** Run [`sql/01_create_database.sql`](sql/01_create_database.sql) as an
   administrator. It creates `ArisMemory`, the `ai` schema and the `ai.CreateTable` procedure.
3. **Create the AI login.** Open [`sql/02_create_ai_login.sql`](sql/02_create_ai_login.sql), replace
   `CHANGE_ME_Str0ng!Pass` on the `DECLARE @Password` line with your own password, and run it. The script
   refuses to run with the placeholder, warns if step 1 is still needed, and can be run again later to reset
   the password.
4. **Check the login** with the password you chose:
   ```powershell
   sqlcmd -S localhost -U ari_mcp -P "<password>" -d ArisMemory -C -Q "SELECT 1"
   ```

From a terminal instead of SSMS, the scripts run with `sqlcmd -S localhost -E -C -i sql\01_create_database.sql`
(and the same for `02`). For SQL Server Express, use `localhost\SQLEXPRESS` everywhere this guide says
`localhost`.

Both scripts are safe to run again, for example after pulling a newer version of the repository.

### 5. Connect Claude Desktop

Open **Settings → Developer → Edit Config**. This opens `%APPDATA%\Claude\claude_desktop_config.json`.
Add an `mcpServers` entry at the **top level** of the file, next to any keys that are already there (not
inside another block). Use your own repository path, document folder and password.
[examples/claude_desktop_config.json](examples/claude_desktop_config.json) has the same content:

```json
{
  "mcpServers": {
    "ari-documents": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\Users\\<you>\\source\\repos\\Ari",
               "ari-mcp", "--root", "C:\\Store\\Dokument"]
    },
    "ari-memory": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\Users\\<you>\\source\\repos\\Ari", "ari-memory"],
      "env": {
        "ARI_MEMORY_CONNECTION_STRING": "Server=localhost;Database=ArisMemory;UID=ari_mcp;PWD=<password>;Encrypt=yes;TrustServerCertificate=yes"
      }
    }
  }
}
```

Backslashes in JSON are written twice (`\\`). Quit Claude Desktop completely (also from the system tray) and
start it again. Under **Settings → Developer** both servers should now show as *running*.

### 6. Try it

Start a new chat and ask, for example:

- *"What does the travel policy in the archive say about trains?"*
- *"Create a table for my reading list with title, author and whether I've finished it."*
- *"Add the book I'm reading now, then show me the list."*

Claude decides which tools to call from their descriptions, and asks for your permission before it uses them.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| *Couldn't load app settings … Expected double-quoted property name* | The config file is no longer valid JSON, usually because `mcpServers` was pasted inside another block. It must sit at the top level, and every `{` needs its `}`. |
| A server shows *failed* or *disconnected* | Open **Settings → Developer → Open Logs Folder** and read `mcp-server-ari-*.log`; the server's own errors are printed there. If `uv` is not found, use its full path as `command`, e.g. `C:\Users\<you>\.local\bin\uv.exe` (with `\\` in JSON). |
| *Login failed for user 'ari_mcp'* | Run `EXEC xp_readerrorlog 0, 1, N'ari_mcp';` as an administrator to see the reason. *Windows authentication only*: do step 4.1. *Password did not match*: run `02_create_ai_login.sql` again with the password from your connection string. |
| *Cannot connect* or a network error | Check the server name: `localhost\SQLEXPRESS` for Express, and that the SQL Server service is running. |
| The memory tools answer with a connection error | The server starts even when the database is unreachable and reports why on every call. Fix the cause above and restart Claude Desktop. |

The connection string can also be given with `--connection-string`, and the delete limit changed with
`--max-deletes` or `ARI_MEMORY_MAX_DELETES` (default 5).

## Other hosts and tools

### VS Code

The repository includes [.vscode/mcp.json](.vscode/mcp.json) for the document server. Open the folder in
VS Code, start `ari-documents` from that file, and enter the document folder when prompted. The tools are then
available to the chat in agent mode.

### MCP Inspector

The official Inspector is a good way to see exactly what a client sees:

```bash
npx @modelcontextprotocol/inspector uv run ari-mcp --root sample-docs
# or headless:
npx @modelcontextprotocol/inspector --cli uv run ari-mcp --root sample-docs --method tools/list
```

## Development

Run a server by hand (it then waits for a client on stdin/stdout):

```powershell
uv run ari-mcp --root "C:\Store\Dokument"
uv run ari-memory --connection-string "Server=localhost;Database=ArisMemory;UID=ari_mcp;PWD=…;Encrypt=yes;TrustServerCertificate=yes"
```

The SQL Server tests run the repository's own setup scripts and then work as `ari_mcp`, so point them at a
server where they may create `ArisMemory`, for example a throwaway container:

```bash
docker run -d -p 1433:1433 -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD='Str0ng!Passw0rd' mcr.microsoft.com/mssql/server:2022-latest
ARI_MEMORY_TEST_SERVER=localhost,1433 ARI_MEMORY_TEST_SA_PASSWORD='Str0ng!Passw0rd' uv run pytest
```

CI runs both suites: the document tests on Linux and Windows, and the memory tests against SQL Server 2022.

## Project layout

```
src/ari_mcp/
  server.py         ari-documents: the three tools, their schemas, and the CLI entry point
  sandbox.py        DocumentRoot: the path check every document tool goes through
  readers.py        Text extraction for .txt/.md and .docx
  memory_server.py  ari-memory: the six tools and the CLI entry point
  memory_store.py   All SQL for ari-memory, with the name checks, one-row, soft-delete and delete-limit guardrails
sql/                Database, ai.CreateTable procedure and least-privilege login for ari-memory
tests/              pytest suites; both servers are tested through a real MCP client
examples/           demo_client.py (a minimal MCP client) and a Claude Desktop config
sample-docs/        Small public documents for trying it out
```

## Design notes

- `create_server(root)` and `create_memory_server(store)` build a server from its dependencies instead of
  module-level globals, so tests can point them at a temporary folder or a test database.
- Tool inputs and outputs are typed. The SDK turns type hints, `Field` constraints and docstrings into the
  JSON Schemas the model sees, and Pydantic return types become `outputSchema` and `structuredContent`.
- Tool annotations (`readOnlyHint`, `destructiveHint`) tell hosts which calls only read and which change data,
  so they can ask the user before a destructive call.
- Transport is stdio, the standard for local servers: the host starts the process and owns its lifetime.
  Logs go to stderr, because stdout carries the protocol.
- The servers speak both the current stateless protocol (2026-07-28) and the older initialize handshake
  (2025-11-25), and the test suite checks both over a real stdio connection.
