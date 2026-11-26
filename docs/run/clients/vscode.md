# VS Code

Visual Studio Code can use MCP servers through its AI chat features.

!!! warning "Not tested by this project"
    This page was written from the current VS Code documentation and was not tried in VS Code. The pages used are [Use MCP servers](https://code.visualstudio.com/docs/copilot/customization/mcp-servers) and the [MCP configuration reference](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration). Those pages state that the editor-managed `mcp.json` described here belongs to the "Local" setup, which is deprecated and will be removed in February 2027, and that newer setups use other files. Check the documentation for what your version expects.

## Where the configuration goes

According to the configuration reference, the editor-managed files are:

- `.vscode/mcp.json` in the workspace: this project only.
- `mcp.json` in your VS Code user profile: all your projects.

The root key is `servers` (not `mcpServers`). You can also run **MCP: Add Server** from the Command Palette. According to the current documentation, its guided setup writes to the newer files (your Copilot Global configuration, or a `.mcp.json` file at the workspace root), not to the files above, so check where the entry went. Review the command it generates before you accept it. VS Code asks you to trust a server before it starts.

## Local, over stdio

You need [uv](https://docs.astral.sh/uv/).

```json
{
  "servers": {
    "boe-insights": {
      "type": "stdio",
      "command": "uvx",
      "args": ["boe-insights-mcp"]
    }
  }
}
```

From a clone, use `"command": "uv"` and `"args": ["--directory", "/absolute/path/to/boe-insights-mcp", "run", "boe-insights-mcp"]`.

To pass settings, add an `env` object next to `args`. They are listed in [Run with uvx](../uvx.md#settings).

## Over HTTP

Start the server first: [Run over HTTP](../http.md) or [Run in a container](../container.md). Then:

```json
{
  "servers": {
    "boe-insights": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

VS Code tries streamable HTTP first and falls back to the older SSE transport. This server speaks streamable HTTP.

### With GitHub login

The configuration reference says that for an HTTP server VS Code runs the OAuth flow by itself and opens a browser window the first time it connects. With this server's [GitHub login](../github-login.md) you would see the server's consent page and then, the first time, GitHub's authorize page. The reference shows an optional `oauth` object with a `clientId` for servers whose clients are pre-registered; this server has none, so you do not use it.

VS Code's developer guide says its redirect addresses are `http://127.0.0.1:33418` and `https://vscode.dev/redirect`. If VS Code registers dynamically with both, this server refuses the whole registration unless `https://vscode.dev/redirect` (and `https://insiders.vscode.dev/redirect` for VS Code Insiders) is listed in `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` (see [Redirect addresses](../github-login.md#redirect-addresses)). Listing them means that sign-in codes for VS Code may be delivered through vscode.dev. A registration with these addresses was refused without the setting and accepted with it. The server also accepts clients that identify themselves with a metadata document instead of registering; whether VS Code does that, and what happens then, was not tested. Signing in from VS Code itself was not tested.

## See also

- [Generic client](generic.md)
- [Security and operations](../../operating.md)
