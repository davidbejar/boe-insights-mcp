# Claude Code

[Claude Code](https://code.claude.com/docs/en/mcp) is Anthropic's command-line coding assistant. This page connects it to `boe-insights-mcp`, either as a local program (stdio) or as an HTTP server.

!!! success "Tested by this project"
    Both ways were tested with Claude Code: stdio, and HTTP with [GitHub login](../github-login.md). Open HTTP without login uses the same Claude Code commands and was not tested separately in a client.

## Local, over stdio

Claude Code starts the server itself. You need [uv](https://docs.astral.sh/uv/).

```bash
claude mcp add boe-insights -- uvx boe-insights-mcp
```

Everything after `--` is the command that Claude Code runs. `boe-insights` is the name you give the server.

Before release 0.1.0 the package is not on PyPI. Use the repository form, which works today:

```bash
claude mcp add boe-insights -- uvx --from git+https://github.com/davidbejar/boe-insights-mcp boe-insights-mcp
```

From a clone, use the absolute path of the folder:

```bash
claude mcp add boe-insights -- uv --directory /absolute/path/to/boe-insights-mcp run boe-insights-mcp
```

To pass settings, add `-e NAME=value` before the name, for example `claude mcp add -e BOE_INSIGHTS_DATA_DIR=/path/to/data boe-insights -- uvx boe-insights-mcp`. The settings are listed in [Run with uvx](../uvx.md#settings).

### Where it is stored

By default the server is saved for you and the current project only ("local" scope). Add `--scope user` to have it in all your projects, or `--scope project` to write a `.mcp.json` file in the project root that you can commit and share:

```json
{
  "mcpServers": {
    "boe-insights": {
      "command": "uvx",
      "args": ["boe-insights-mcp"]
    }
  }
}
```

Claude Code asks you to approve a project-scoped server the first time.

## Over HTTP

First start the server: see [Run over HTTP](../http.md) or [Run in a container](../container.md). Then add its address:

```bash
claude mcp add --transport http boe-insights http://localhost:8000/mcp
```

For a server on another machine, use its public address, for example `https://boe.example.org/mcp`.

A server that is added while Claude Code is open shows up after you restart Claude Code.

### With GitHub login

If the server has [GitHub login](../github-login.md) on, Claude Code must sign in once:

1. In Claude Code, type `/mcp`.
2. Choose the `boe-insights` server and follow the steps to authenticate. A browser window opens.
3. You see the server's consent page, then (the first time only) GitHub's authorize page.
4. Back in Claude Code, the server shows as connected, and the saved-search tools are available.

If your GitHub account is not on the server's allow-list, the browser shows an "Access not allowed" page with your `id:<n>`. Send it to the operator.

Claude Code stores the token and renews it by itself. From a shell, `claude mcp login boe-insights` and `claude mcp logout boe-insights` do the same as the `/mcp` menu. Renewal after GitHub's token really expires has not been observed. If Claude Code asks you to authenticate again, do it with `/mcp`.

### Without login

Over open HTTP there is nothing to sign in to. The saved-search tools are not offered. See [Run over HTTP](../http.md#what-is-available-over-open-http).

## Check that it works

```bash
claude mcp list
```

Inside Claude Code, `/mcp` shows each server and its state. Then ask something that needs the data, for example "What does article 14 of the Spanish Constitution say?" Expand the tool call to confirm the answer came from a tool and not from memory.

## Remove it

```bash
claude mcp remove boe-insights
```

## See also

- [Generic client](generic.md) for the settings every client accepts.
- [Security and operations](../../operating.md).
