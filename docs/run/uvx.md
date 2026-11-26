# Run with uvx

`uvx` runs a Python program without installing it into your system. It downloads the program into a private, cached environment the first time and reuses it afterwards. This is the quickest way to run `boe-insights-mcp`.

## What you need

- [uv](https://docs.astral.sh/uv/), which provides the `uvx` command. Check with `uvx --version`.
- Python 3.11 or newer. uv finds one on your machine or downloads one for you.
- No API key. The BOE open data API is public.

## Run it

```bash
uvx boe-insights-mcp
```

The server speaks MCP over standard input and output (the "stdio" transport). That means it starts and then waits silently for a client to talk to it. Seeing a start-up line and no prompt is normal. Press Ctrl+C to stop it.

You normally do not run this by hand. You put the same command in your MCP client's configuration, and the client starts the server itself. See [Claude Code](clients/claude-code.md), [Claude Desktop](clients/claude-desktop.md), [Cursor](clients/cursor.md), [VS Code](clients/vscode.md) or [any other client](clients/generic.md).

Check the options:

```bash
uvx boe-insights-mcp --help
```

## Run the code from the repository

To try the latest code that is not released yet, run it straight from GitHub:

```bash
uvx --from git+https://github.com/davidbejar/boe-insights-mcp boe-insights-mcp
```

In a client configuration, use `"command": "uvx"` and these arguments:

```json
["--from", "git+https://github.com/davidbejar/boe-insights-mcp", "boe-insights-mcp"]
```

## Pin a version

You can ask for an exact version, so a client does not pick up a new one by surprise:

```bash
uvx boe-insights-mcp@0.1.0
```

## Settings

Settings are environment variables that start with `BOE_INSIGHTS_`. All are optional. Set them in the shell, or in the `env` block of your client's configuration.

```bash
BOE_INSIGHTS_DATA_DIR=$HOME/boe-data uvx boe-insights-mcp
```

| Variable | Default | Meaning |
|---|---|---|
| `BOE_INSIGHTS_CACHE` | `disk` | `disk` keeps the response cache between runs. `memory` keeps it only while the server runs. |
| `BOE_INSIGHTS_CACHE_DIR` | your platform's cache folder | Where the disk cache lives. Safe to delete. |
| `BOE_INSIGHTS_DATA_DIR` | your platform's user data folder | Where saved searches live (`saved_searches.sqlite3`). Not safe to delete: it holds your data. |
| `BOE_INSIGHTS_MIN_INTERVAL` | `1.0` | Minimum seconds between requests to boe.es. |
| `BOE_INSIGHTS_MAX_CONCURRENCY` | `2` | Maximum simultaneous requests to boe.es. |

Unset or empty variables keep their default. An invalid value stops the server at start-up with a message that names the variable.

With `BOE_INSIGHTS_CACHE=memory` and no `BOE_INSIGHTS_DATA_DIR`, saved searches are kept in memory too and are lost when the server stops.

The HTTP settings are described in [Run over HTTP](http.md). Over stdio they have no effect, but they are still checked: an invalid value, a partial GitHub login, or an allow-list without GitHub login stops the server at start-up.

## See also

- [Run from source](from-source.md) if you want to change the code.
- [Run over HTTP](http.md) to serve more than one client.
