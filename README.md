# Ari – MCP servers for documents and memory

Ari is two small [Model Context Protocol](https://modelcontextprotocol.io) (MCP) servers written in Python with
the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (v2). Each one gives an AI
agent a narrow, well-guarded capability:

| Server | Command | What the agent can do |
| --- | --- | --- |
| **ari-documents** | `ari-mcp` | Read and search one folder of public documents. Nothing else. |
| **ari-memory** | `ari-memory` | Save, find, update and delete notes in one SQL Server table, one row at a time. |

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

## ari-memory: a guarded SQL Server memory

A small long-term memory for the agent, stored in the `dbo.Memories` table of the `ArisMemory` database.

| Tool | What it does | Hint to the host |
| --- | --- | --- |
| `search_memories` | Finds memories by text and/or topic, newest first, at most 50. | read-only |
| `add_memory` | Saves one new memory (topic + content). | additive |
| `update_memory` | Replaces the content (and optionally topic) of one memory, by id. | destructive |
| `delete_memory` | Deletes one memory, by id. | destructive |

### Guardrails

The goal: the agent can never drop the database or a table, never touch more rows than the one it was asked
about, and nothing it does is unrecoverable. That is enforced in four layers, each of which would hold on its own:

1. **No free-form SQL.** The agent can only call the four tools above. Every statement is fixed and
   parameterized in [`memory_store.py`](src/ari_mcp/memory_store.py), so there is no tool that could drop,
   truncate or alter anything, and text like `'); DROP TABLE …` is stored as text.
2. **One row per change.** Updates and deletes take a single `memory_id` and match it against the primary key.
   Each write runs in a transaction, and the `OUTPUT` clause reports exactly which rows changed. The
   transaction commits only if that is exactly one row; otherwise it rolls back.
3. **Deletes are soft and capped.** `delete_memory` only sets `DeletedAt`; the row stays and can be restored
   with `UPDATE dbo.Memories SET DeletedAt = NULL WHERE Id = …`. Each server session allows at most
   5 deletions (`--max-deletes` / `ARI_MEMORY_MAX_DELETES`), so a confused agent cannot work through the
   table one row at a time.
4. **Least privilege in SQL Server.** [`sql/02_create_ai_login.sql`](sql/02_create_ai_login.sql) creates an
   `ari_mcp` login that may `SELECT` and `INSERT` on `dbo.Memories`, `UPDATE` only its text and timestamp
   columns, and is explicitly denied `DELETE`. It has no rights on anything else. The tests connect as this
   login with raw SQL and confirm that `DELETE`, `TRUNCATE`, `DROP TABLE`, `ALTER TABLE`, `CREATE TABLE` and
   `DROP DATABASE` are all refused by the server itself.

Inputs are validated before any SQL runs: topic ≤ 100 characters, content ≤ 4000, ids ≥ 1, search limit ≤ 50.

### Database setup

Run the two scripts once, as an administrator (SSMS, or `sqlcmd -S localhost -E -i <file>`):

1. [`sql/01_create_database.sql`](sql/01_create_database.sql) creates `ArisMemory` and `dbo.Memories`.
2. [`sql/02_create_ai_login.sql`](sql/02_create_ai_login.sql) creates the restricted `ari_mcp` login.
   Change the password first. SQL logins need *SQL Server and Windows Authentication mode* (Server
   Properties → Security in SSMS, then restart the SQL Server service).

The server connects with [mssql-python](https://github.com/microsoft/mssql-python), Microsoft's official
driver, which needs no separate ODBC driver. Give it a connection string via `ARI_MEMORY_CONNECTION_STRING`
or `--connection-string`:

```
Server=localhost;Database=ArisMemory;UID=ari_mcp;PWD=<password>;Encrypt=yes;TrustServerCertificate=yes
```

Use `Server=localhost\SQLEXPRESS` for SQL Server Express. Windows authentication
(`Trusted_Connection=yes` instead of `UID`/`PWD`) also works, but then the server runs with *your* database
rights and layer 4 no longer applies.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) (recommended), or plain `pip`
- For ari-memory: SQL Server 2016 or newer (Express and Developer editions work)

## Quick start

```bash
git clone https://github.com/theanarch/Ari.git
cd Ari
uv sync                       # creates .venv and installs mcp, python-docx, mssql-python and the dev tools
uv run pytest                 # document server tests; the SQL Server tests are skipped without a database
uv run python examples/demo_client.py sample-docs --query budget
```

Run a server yourself (it then waits for a client on stdin/stdout):

```bash
uv run ari-mcp --root "C:\Store\Dokument"
uv run ari-memory --connection-string "Server=localhost;Database=ArisMemory;UID=ari_mcp;PWD=…;Encrypt=yes;TrustServerCertificate=yes"
```

To run the SQL Server tests, point them at a server where they may create `ArisMemory` (for example a
throwaway container):

```bash
docker run -d -p 1433:1433 -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD='Str0ng!Passw0rd' mcr.microsoft.com/mssql/server:2022-latest
ARI_MEMORY_TEST_SERVER=localhost,1433 ARI_MEMORY_TEST_SA_PASSWORD='Str0ng!Passw0rd' uv run pytest
```

CI runs both suites: the document tests on Linux and Windows, and the memory tests against SQL Server 2022.

## Connect it to a host

### Claude Desktop

Open **Settings → Developer → Edit Config** (on Windows the file is
`%APPDATA%\Claude\claude_desktop_config.json`) and add the servers under `mcpServers`. A complete example is in
[examples/claude_desktop_config.json](examples/claude_desktop_config.json):

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

Restart Claude Desktop, and try *"What does the travel policy in the archive say about trains?"* or
*"Remember that I prefer answers in Swedish."*

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

## Project layout

```
src/ari_mcp/
  server.py         ari-documents: the three tools, their schemas, and the CLI entry point
  sandbox.py        DocumentRoot: the path check every document tool goes through
  readers.py        Text extraction for .txt/.md and .docx
  memory_server.py  ari-memory: the four tools and the CLI entry point
  memory_store.py   All SQL for ari-memory, with the one-row, soft-delete and delete-limit guardrails
sql/                Database and least-privilege login setup for ari-memory
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
