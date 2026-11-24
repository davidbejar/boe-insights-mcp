# Run from source

Use this when you want to read or change the code, or to run the latest unreleased version from a clone of the repository.

## What you need

- [uv](https://docs.astral.sh/uv/).
- Python 3.11 or newer. uv finds or downloads one.
- Git.
- No API key.

## Get it and install it

```bash
git clone https://github.com/davidbejar/boe-insights-mcp.git
cd boe-insights-mcp
uv sync
```

`uv sync` creates a virtual environment in `.venv` and installs the locked dependencies. To also install the test and lint tools, run `uv sync --all-groups`.

## Run it

```bash
uv run boe-insights-mcp
```

By default the server speaks MCP over standard input and output (stdio). It starts and waits silently for a client. Press Ctrl+C to stop it.

You normally do not run it by hand: your MCP client starts it. In a client configuration, give the absolute path of your clone:

```json
{
  "mcpServers": {
    "boe-insights": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/boe-insights-mcp", "run", "boe-insights-mcp"]
    }
  }
}
```

`--directory` makes uv work in your clone no matter where the client starts it from. See the client pages for where this JSON goes: [Claude Code](clients/claude-code.md), [Claude Desktop](clients/claude-desktop.md), [Cursor](clients/cursor.md), [VS Code](clients/vscode.md), [other clients](clients/generic.md).

## Run over HTTP

```bash
uv run boe-insights-mcp --transport http --port 8000
```

This binds `127.0.0.1` by default. The details, and what to do before exposing it to others, are in [Run over HTTP](http.md).

## Settings

The same `BOE_INSIGHTS_*` environment variables apply as for [uvx](uvx.md#settings). For example, to keep everything in a throwaway folder while you experiment:

```bash
BOE_INSIGHTS_DATA_DIR=/tmp/boe/data BOE_INSIGHTS_CACHE_DIR=/tmp/boe/cache uv run boe-insights-mcp
```

## Check your changes

```bash
uv run pytest                              # tests; no network
uv run ruff check . && uv run ruff format .
uv run pyright
```

`uv run pytest -m live` runs tests against the real BOE API. Use it sparingly.
