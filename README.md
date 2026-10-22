# Ari – a read-only document MCP server

Ari is a [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server written in Python with the
official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (v2). It gives an AI agent
**read-only** access to one folder of public documents, through three tools:

| Tool | What it does |
| --- | --- |
| `list_documents` | Lists the readable documents in the folder (optionally one subfolder), recursively. |
| `read_document` | Returns the text of one document, with an optional `max_chars` limit. |
| `search_documents` | Case-insensitive search across all documents; returns file, line and a snippet. |

Supported formats: plain text (`.txt`, `.md`, `.csv`, `.log`) and Word (`.docx`, including tables).
Text files are read as UTF-8 and fall back to Windows-1252, so older Swedish files keep their å, ä and ö.

## Security model

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

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) (recommended), or plain `pip`

## Quick start

```bash
git clone https://github.com/theanarch/Ari.git
cd Ari
uv sync                       # creates .venv and installs mcp, python-docx and the dev tools
uv run pytest                 # 32 tests: sandbox, tools, and a real stdio round trip
uv run python examples/demo_client.py sample-docs --query budget
```

Run the server yourself (it then waits for a client on stdin/stdout):

```bash
uv run ari-mcp --root "C:\Store\Dokument"
# or: set ARI_DOCS_ROOT and run `uv run ari-mcp`
```

Without uv: `python -m venv .venv`, activate it, then `pip install -e .` and `pip install pytest anyio`.

## Connect it to a host

### Claude Desktop

Open **Settings → Developer → Edit Config** (on Windows the file is
`%APPDATA%\Claude\claude_desktop_config.json`) and add the server. A complete example is in
[examples/claude_desktop_config.json](examples/claude_desktop_config.json):

```json
{
  "mcpServers": {
    "ari-documents": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\Users\\<you>\\source\\repos\\Ari",
               "ari-mcp", "--root", "C:\\Store\\Dokument"]
    }
  }
}
```

Restart Claude Desktop, and ask something like *"Which documents are in the archive, and what does the travel
policy say about trains?"*

### VS Code

The repository includes [.vscode/mcp.json](.vscode/mcp.json). Open the folder in VS Code, start the
`ari-documents` server from that file, and enter the document folder when prompted. The tools are then
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
  server.py     MCP server: the three tools, their schemas, and the CLI entry point
  sandbox.py    DocumentRoot: the path check every tool goes through
  readers.py    Text extraction for .txt/.md and .docx
tests/          pytest suite; test_server.py drives the server through a real MCP client
examples/       demo_client.py (a minimal MCP client) and a Claude Desktop config
sample-docs/    Small public documents for trying it out
```

## Design notes

- `create_server(root)` builds the server for one folder instead of using module-level globals, so tests can
  point it at a temporary folder and the same code serves any folder.
- Tool inputs and outputs are typed. The SDK turns type hints, `Field` constraints and docstrings into the
  JSON Schemas the model sees, and Pydantic return types become `outputSchema` and `structuredContent`.
- Transport is stdio, the standard for local servers: the host starts the process and owns its lifetime.
  Logs go to stderr, because stdout carries the protocol.
- The server speaks both the current stateless protocol (2026-07-28) and the older initialize handshake
  (2025-11-25), and the test suite checks both over a real stdio connection.
