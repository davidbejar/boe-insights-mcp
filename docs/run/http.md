# Run over HTTP

By default the server talks to one client on your own machine over stdio. Over HTTP it listens on a network port, so several clients can use it, and clients that cannot start a program can still reach it. This page uses the MCP "streamable HTTP" transport.

!!! warning "Open HTTP has no sign-in"
    Without the GitHub login described in [GitHub login](github-login.md), anyone who can reach the port can use the server. The default address, `127.0.0.1`, is reachable only from your own machine. Read [Security and operations](../operating.md) before you bind any other address.

## Start it

```bash
uvx boe-insights-mcp --transport http
```

(From a clone, use `uv run boe-insights-mcp --transport http`.)

The server logs where it listens. With the defaults the MCP address is:

```text
http://127.0.0.1:8000/mcp
```

The path `/mcp` is fixed. Check that the process is up:

```bash
curl http://127.0.0.1:8000/health
```

The answer is always `{"status":"ok"}`, unless the [host and origin checks](#allowed-hosts-and-origins) refuse the request. It does not call boe.es and reveals nothing else.

Connect a client to the MCP address: see the [client pages](clients/generic.md).

## Command line and environment

Each flag overrides its environment variable.

| Flag | Variable | Default | Meaning |
|---|---|---|---|
| `--transport` | `BOE_INSIGHTS_TRANSPORT` | `stdio` | `stdio` or `http`. |
| `--host` | `BOE_INSIGHTS_HOST` | `127.0.0.1` | Address to bind. |
| `--port` | `BOE_INSIGHTS_PORT` | `8000` | Port, 1 to 65535. |

For example, `BOE_INSIGHTS_TRANSPORT=http BOE_INSIGHTS_PORT=9000 uvx boe-insights-mcp` is the same as passing `--transport http --port 9000`.

## Every HTTP setting

| Variable | Default | Meaning |
|---|---|---|
| `BOE_INSIGHTS_ALLOWED_HOSTS` | empty | Comma-separated host names the server accepts in the `Host` header, besides the loopback names. Bare host names or IP addresses only: no `https://`, no port, no path, no wildcard. |
| `BOE_INSIGHTS_ALLOWED_ORIGINS` | empty | Comma-separated browser origins accepted, each exactly `http://host[:port]` or `https://host[:port]`. No wildcard. |
| `BOE_INSIGHTS_CALLS_PER_MINUTE` | `60` | Sustained calls per caller per minute. `0` switches the limit off. |
| `BOE_INSIGHTS_CALLS_BURST` | `20` | How many calls may be made at once before the caller has to wait. At least 1. |

The GitHub login variables are on [their own page](github-login.md). The cache and data variables are the same as in [uvx](uvx.md#settings).

Invalid values stop the server at start-up with a message that names the variable. `BOE_INSIGHTS_HOST` is the exception: it is not checked in advance. Over stdio the HTTP settings have no effect, but they are still checked, so an invalid value stops a stdio server too.

## Binding and who can reach the server

- `127.0.0.1` (the default) accepts connections only from the same machine.
- `0.0.0.0` accepts connections on every network interface. Use it only inside a container or behind something that controls access. A server bound this way with no login is open to every machine that can reach it. Without GitHub login, the server logs a warning at start whenever it is bound to an address other than a loopback one.
- Behind a reverse proxy, keep the server on `127.0.0.1` and let the proxy face the network.

## Allowed hosts and origins

Two checks run on every HTTP request, always, and cannot be turned off.

- **Host check.** The `Host` header must be a loopback name (`localhost`, `127.0.0.1`, `[::1]`) or an entry of `BOE_INSIGHTS_ALLOWED_HOSTS`. The address the server is bound to (`--host`) is accepted too, unless it is `0.0.0.0` or `::`. Any other host is refused. When tested, a request with `Host: evil.example` got status 421. The check protects against DNS rebinding, where a web page tricks a browser into talking to a local server.
- **Origin check.** A request that carries an `Origin` header (browsers send one) must come from an entry of `BOE_INSIGHTS_ALLOWED_ORIGINS`, from the server's own address, or, when the server is reached on a loopback name, from a loopback address. Any other origin gets status 403. Command-line MCP clients send none and are not affected.

If you reach the server under a name other than `localhost` (for example `boe.example.org`), add it:

```bash
BOE_INSIGHTS_ALLOWED_HOSTS=boe.example.org uvx boe-insights-mcp --transport http
```

With GitHub login on, the host and origin of `BOE_INSIGHTS_BASE_URL` are accepted automatically.

## What is available over open HTTP

Open HTTP means HTTP without GitHub login. Every analytical tool is there: law search, article reading and history, amendments, citation checks, the daily gazette and the trend tools. There are 18 tools, and listing them on a running open server confirmed that.

The five saved-search tools are **not** there: `save_search`, `list_saved_searches`, `check_saved_searches`, `edit_saved_search` and `delete_saved_search`. The saved-searches resource and the sentence about saved searches in the server instructions are left out too, and the gazette briefing prompts no longer suggest saving the search. The eight prompts themselves stay. Reason: a saved search belongs to a person, and without a sign-in the server cannot tell people apart. Rather than let strangers share one list, it offers none.

To get saved searches over HTTP, turn on [GitHub login](github-login.md). Over stdio they always work, because there is one person.

## Call limit

Each caller may make `BOE_INSIGHTS_CALLS_PER_MINUTE` calls per minute (default 60), with bursts up to `BOE_INSIGHTS_CALLS_BURST` (default 20). A caller who goes over gets a message saying the rate limit was reached and how many seconds to wait.

- Only calls that do work count: tool calls, resource reads and prompt requests. Start-up and listing requests do not.
- Over open HTTP there is one shared limit for everybody, because without a sign-in the server cannot tell callers apart.
- With GitHub login, each signed-in GitHub account has its own limit.
- The limit exists to protect your server. It is separate from the pacing of the server's own requests to boe.es (`BOE_INSIGHTS_MIN_INTERVAL` and `BOE_INSIGHTS_MAX_CONCURRENCY`).
- Over stdio there is no call limit.

## Request size

The MCP address accepts request bodies up to 1 MiB. Every other address, including the sign-in routes, accepts up to 64 KiB. Larger requests get status 413. The server's access log leaves out query strings, so the one-time sign-in codes and transaction ids carried there never reach it. FastMCP's own warnings can still name a sign-in transaction id: see [Logs](../operating.md#logs).

## Sessions

The server keeps a session in memory for each connected client. Run one server process, or make sure a load balancer always sends a client to the same process. (This follows from the server's session design; running several processes behind a balancer was not tested.)

## See also

- [GitHub login](github-login.md) to close the server to a list of people.
- [Run in a container](container.md), which runs this transport for you.
- [Security and operations](../operating.md).
