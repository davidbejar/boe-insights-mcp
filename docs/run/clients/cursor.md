# Cursor

Cursor is a code editor with a built-in AI assistant. It can run MCP servers.

!!! warning "Not tested by this project"
    This page was written from Cursor's current documentation, [Model Context Protocol (MCP)](https://cursor.com/docs/context/mcp), and was not tried in Cursor. File names and field names below come from that page. If something differs in your version of Cursor, trust Cursor's documentation.

## Where the configuration goes

Cursor reads a JSON file named `mcp.json`:

- `.cursor/mcp.json` in a project folder: the server is available in that project only.
- `~/.cursor/mcp.json` in your home folder: the server is available in every project.

The settings screen for MCP servers is under **Customize** in Cursor's sidebar, where you can enable, disable and remove servers.

## Local, over stdio

You need [uv](https://docs.astral.sh/uv/).

```json
{
  "mcpServers": {
    "boe-insights": {
      "type": "stdio",
      "command": "uvx",
      "args": ["boe-insights-mcp"]
    }
  }
}
```

Cursor's page lists `type` as required for stdio servers, although some of its own examples leave it out. Keeping it is safe.

From a clone, use `"command": "uv"` and `"args": ["--directory", "/absolute/path/to/boe-insights-mcp", "run", "boe-insights-mcp"]`.

To pass settings, add an `env` object next to `args`. They are listed in [Run with uvx](../uvx.md#settings).

## Over HTTP

Start the server first: [Run over HTTP](../http.md) or [Run in a container](../container.md). Then:

```json
{
  "mcpServers": {
    "boe-insights": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

For a server on another machine, use its public address, for example `https://boe.example.org/mcp`.

### With GitHub login

If the server has [GitHub login](../github-login.md) on, Cursor signs in by OAuth. According to Cursor's page, it discovers the server's sign-in details by itself and registers as a public client, so you add nothing to the JSON. The server needs one setting, described below. Cursor then opens a browser, where you see the server's consent page and, the first time, GitHub's authorize page.

Cursor's page says that with dynamic registration the desktop app registers two redirect addresses, `http://localhost:8787/callback` and `https://www.cursor.com/agents/mcp/oauth/callback`, and may send the web one at sign-in. This server refuses the whole registration unless every address is allowed (see [Redirect addresses](../github-login.md#redirect-addresses)), so the operator must add `https://www.cursor.com/agents/mcp/oauth/callback` to `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS`. Listing it means that sign-in codes for Cursor may be delivered through cursor.com. A registration with these two addresses was refused without the setting and accepted with it; signing in from Cursor itself was not tested.

Nobody tested whether the web version of Cursor (Cursor Agents) can reach the server. The server would need to be on a public HTTPS address.

## See also

- [Generic client](generic.md)
- [Security and operations](../../operating.md)
