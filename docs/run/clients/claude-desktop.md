# Claude Desktop

Claude Desktop is Anthropic's desktop app. It can run a local MCP server as a program on your computer. This is the way tested by this project.

!!! success "Tested by this project"
    Local use over stdio was tested with Claude Desktop on macOS on 2026-11-26, with the from-repository command shown below. The server started, and these tools were run and worked: resolving a law reference, reading an article, and saving and listing a saved search. The version of Claude Desktop was not recorded. Windows and Linux were not tested; their config paths below come from Anthropic's documentation.

!!! warning "Not tested"
    Connecting Claude Desktop to the server over HTTP was not tested. Read the HTTP section below for what Anthropic's documentation says and what that means for this server.

## Local, over stdio

You need [uv](https://docs.astral.sh/uv/) installed.

1. Open `claude_desktop_config.json` directly in a text editor. It lives here:
    - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
    - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
    - Linux: `~/.config/Claude/claude_desktop_config.json`

    Keep a backup copy of the file before you edit it. The file may already exist with other settings. Do not replace it: add the `mcpServers` key at the top level, next to the existing keys. Older versions of the app offered **Settings**, then **Developer**, then **Edit Config** to open this file; the current version does not have that entry.
2. Add the server under `mcpServers`. If the file already has an `mcpServers` key, add only the `boe-insights` entry inside it:

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

3. Save the file, then quit Claude Desktop completely and start it again. Closing the window is not enough.
4. In the left sidebar, open **Plugins** (the app shows a notice that connectors and skills are now plugins). `boe-insights` is listed under **Your plugins**, labelled "Local development" and "Local MCP server". Open the entry to see its tools: 23 at the time of testing, split into read-only tools and write/delete tools. The developer is shown as unknown, which is what the app shows for any server added through the config file.

The first start can be slow: with the repository form, uvx builds the package from GitHub (about 16 seconds in the test). Allow up to a minute. If the server shows as failed, restart the app once.

Before release 0.1.0, use the repository form, which works today:

```json
{
  "mcpServers": {
    "boe-insights": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/davidbejar/boe-insights-mcp", "boe-insights-mcp"]
    }
  }
}
```

From a clone, use the absolute path of the folder:

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

If the app says it cannot find `uvx` or `uv`, give the full path to the command, for example the output of `which uvx`. Apps started from the desktop do not always see the same `PATH` as your terminal.

To pass settings, add an `env` block next to `args`, for example `"env": { "BOE_INSIGHTS_DATA_DIR": "/path/to/data" }`. The settings are listed in [Run with uvx](../uvx.md#settings).

### If it does not appear

- Check the JSON for a missing comma or quote.
- Check the logs. On macOS they are in `~/Library/Logs/Claude`. `mcp.log` covers connections, and `mcp-server-boe-insights.log` has the server's own output.
- Run the same command in a terminal to see its error.

## Over HTTP

Anthropic's documentation describes two separate things.

- `claude_desktop_config.json`, above, is for local servers that run on your computer. Anthropic's pages do not say that it accepts a remote server by URL, so do not put an `http://` address in it.
- Remote servers are added as custom connectors: **Customize**, then **Connectors**, then **Add custom connector** (on Team plans, Owners and Primary Owners add them under the organisation settings; on Enterprise plans, Owners, Primary Owners and people with a custom role that includes managing access to Libraries). See [Anthropic's help page](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp), which still gave these steps when checked on 2026-11-26. In the app tested that day, connectors were listed under **Plugins** in the sidebar, with a **Connectors** tab and an **Add** button; adding a custom connector from there was not tried.

The same help page says that remote connectors connect from Anthropic's cloud, not from your device, in every Claude app including Claude Desktop. The server must be reachable from the public internet, and it must use the standard HTTPS port 443. Consequences for this server:

- A server on `localhost`, on a private network or behind a VPN is not reachable that way. This follows from Anthropic's description. It was not tried.
- To use a custom connector you would need to publish the server on a public HTTPS address (for example behind a reverse proxy). See [Security and operations](../../operating.md) first. Whether the server's [GitHub login](../github-login.md) works with Claude's connector sign-in was not tested. Anthropic's help page gives `https://claude.ai/api/mcp/auth_callback` as the address Claude's connector sign-in returns to. If Claude uses that address with this server, it must be listed in `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` (see [Redirect addresses](../github-login.md#redirect-addresses)).

If you only want it on your own computer, use the stdio setup above.

## See also

- [Generic client](generic.md)
- [Run with uvx](../uvx.md)
