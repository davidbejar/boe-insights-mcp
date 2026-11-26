# Generic MCP client

Most MCP clients accept the same two shapes of configuration. Use these if your client is not covered by a page of its own, and check your client's documentation for the file name and the name of the root key (it is often `mcpServers`).

!!! warning "Not tested by this project"
    The snippets here are the common form. This project tested Claude Code (stdio, and HTTP with GitHub login) and Claude Desktop (stdio only). For every other client the exact format is up to that client, so read its documentation.

## Local, over stdio

The client starts the server as a program and talks to it over standard input and output. You need [uv](https://docs.astral.sh/uv/).

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

From a clone:

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

Settings go in an `env` object next to `args`:

```json
"env": { "BOE_INSIGHTS_DATA_DIR": "/path/to/data" }
```

They are listed in [Run with uvx](../uvx.md#settings). If the client cannot find `uvx`, give its full path (`which uvx`).

## Over HTTP

Start the server first: [Run over HTTP](../http.md) or [Run in a container](../container.md). The address a client needs is:

```text
http://localhost:8000/mcp
```

The transport is "streamable HTTP", sometimes called just "HTTP". Do not pick "SSE": the server does not offer the old SSE transport. Many clients take the address in a field called `url`, sometimes with `"type": "http"`:

```json
{
  "mcpServers": {
    "boe-insights": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Some clients cannot speak HTTP themselves. Check the client's documentation, or use stdio instead.

### With GitHub login

If the server has [GitHub login](../github-login.md) on, the client must support the MCP OAuth sign-in: it finds the sign-in details at the server's well-known addresses, registers itself, and opens a browser. A client that cannot do this gets `401 Unauthorized` and cannot use the server. Command-line clients usually redirect back to a loopback address (`localhost`, `127.0.0.1` or `[::1]`), which the server accepts. Editors and web clients often register a web address as well. The server refuses the whole registration if any one of the client's redirect addresses is not allowed, so that address must be listed in `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` on the server. See [Redirect addresses](../github-login.md#redirect-addresses).

### Check the address by hand

```bash
curl http://localhost:8000/health
```

This answers `{"status":"ok"}` when the server is up. If the client reaches the server under a name other than `localhost`, add that name to `BOE_INSIGHTS_ALLOWED_HOSTS`, or the server refuses the request. See [Run over HTTP](../http.md#allowed-hosts-and-origins).

## See also

- [Claude Code](claude-code.md), [Claude Desktop](claude-desktop.md), [Cursor](cursor.md), [VS Code](vscode.md)
- [Security and operations](../../operating.md)
