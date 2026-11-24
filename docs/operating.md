# Security and operations

This page is for a person who runs `boe-insights-mcp` for other people. The project's author hosts
nothing: every operator runs their own copy and is responsible for it. If you only run the server
on your own machine over stdio, you can skip this page.

The page says what the server protects, what it does not, which settings matter, and what has not
been tested. Where something is untested, it says so. How to create the OAuth app and start the
server is on other pages: [HTTP](run/http.md), [container](run/container.md) and
[GitHub sign-in](run/github-login.md).

## The three modes

The mode is decided once, when the server starts.

| Mode | How you get it | What it exposes |
| --- | --- | --- |
| Local (stdio) | The default. No flags. | All analysis tools, for one person. No network port. Saved searches belong to the owner `local`. |
| Open HTTP | `BOE_INSIGHTS_TRANSPORT=http` (or `--transport http`), no login settings. | The analysis tools, to anyone who can reach the port. The five saved-search tools and the saved-search resource are not registered at all, because nobody can own a search, and the gazette briefing prompts leave out their suggestion to save the search. |
| HTTP with GitHub sign-in | HTTP plus the four login settings. An empty allow-list lets nobody in. | Everything, but only to GitHub accounts on the allow-list. Saved searches are owned by `github:<numeric id>`. |

A complete login configuration is ignored over stdio, and the server logs a warning. The values are still checked, so a partial or invalid login configuration stops a stdio server too.

Open HTTP has no authentication of any kind. That is acceptable for public BOE data on a private
network, or behind something else that authenticates. It is not a good idea on the open internet:
anyone can spend your BOE request budget and your bandwidth.

## Threat model

What the server tries to protect:

- The machine and quota of the operator: request size limits, a per-caller call limit, a closed
  allow-list, and a Host/Origin check.
- The saved searches of each user: every row has an owner taken from the verified sign-in, never
  from a tool argument, and no request over HTTP can obtain the `local` owner.
- Politeness towards boe.es: all outgoing requests share one limiter and one cache.

What it does not do:

- It is not hardened for hostile multi-tenant use. The allow-list is the trust boundary: someone on
  it is trusted to use your quota and to store up to 20 small saved searches.
- The BOE data is public. The server does not hide it and does not need to.
- It does not terminate TLS, and it does not filter by IP address.
- With sign-in on, anyone who can reach the server can make it fetch an `https` address of their
  choosing: a client may give the address of a metadata document as its client id, and the server
  fetches it (see [Client registrations and metadata documents](#client-registrations-and-metadata-documents)).
  FastMCP applies protection against server-side request forgery to that fetch. Put a request
  rate limit on `/authorize` and `/register` at the proxy.
- It has had two independent security reviews and a mutation check of the login code. That is
  evidence, not a guarantee.

## Settings

All settings are environment variables. Command-line flags `--transport`, `--host` and `--port`
override their variables. An invalid value stops the server at startup with a message that names
the variable. `BOE_INSIGHTS_HOST` is the exception: it is not checked in advance.

### What the server refuses to start without

- Login is all or nothing: `BOE_INSIGHTS_GITHUB_CLIENT_ID`, `BOE_INSIGHTS_GITHUB_CLIENT_SECRET`,
  `BOE_INSIGHTS_AUTH_SIGNING_KEY` and `BOE_INSIGHTS_BASE_URL`. If some are set and some are not, it
  refuses to start, so a half-configured login never becomes an open server.
- The signing key must be at least 32 characters.
- `BOE_INSIGHTS_BASE_URL` must be `https` unless its host is loopback.
- `BOE_INSIGHTS_ALLOWED_USERS` or `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` without the login settings
  is an error, because the list would silently do nothing.
- A malformed entry in any list, a wildcard in a host or origin, or a port outside 1-65535.

One thing it does not refuse: login configured with an empty allow-list. It starts, logs a warning,
and nobody can sign in. The list is closed by default.

### Table

| Variable | Default | What it does |
| --- | --- | --- |
| `BOE_INSIGHTS_TRANSPORT` | `stdio` | `stdio` or `http`. |
| `BOE_INSIGHTS_HOST` | `127.0.0.1` | Address the HTTP server binds to. The container image sets `0.0.0.0`. |
| `BOE_INSIGHTS_PORT` | `8000` | HTTP port. MCP is served at `/mcp`. `GET /health` returns a constant `{"status":"ok"}` without a login. |
| `BOE_INSIGHTS_BASE_URL` | none | The public URL clients reach, as `https://host[:port]`, with no path, query or credentials. Required for login. |
| `BOE_INSIGHTS_ALLOWED_HOSTS` | none | Comma-separated host names accepted in the `Host` header. Bare names or IP addresses only: no scheme, port, path or wildcard. Loopback is always accepted; with login, the host of the base URL is added. |
| `BOE_INSIGHTS_ALLOWED_ORIGINS` | none | Comma-separated browser origins, each `http(s)://host[:port]`. With login, the origin of the base URL is added. |
| `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` | none | Extra redirect URIs that OAuth clients may register. Exact absolute URLs, no wildcards; `https`, or `http` only for loopback. Loopback redirects on any port are always allowed. A registration is refused as a whole if any one of its redirect URIs is not allowed. |
| `BOE_INSIGHTS_GITHUB_CLIENT_ID` | none | Client id of your GitHub OAuth app. |
| `BOE_INSIGHTS_GITHUB_CLIENT_SECRET` | none | Its secret. A secret: see [Secrets](#secrets). |
| `BOE_INSIGHTS_AUTH_SIGNING_KEY` | none | Signs the tokens the server issues and encrypts its token store. At least 32 characters. A secret. |
| `BOE_INSIGHTS_ALLOWED_USERS` | none (nobody) | Comma-separated: GitHub logins, `id:<number>`, or `*` for every GitHub account. Read once at startup. |
| `BOE_INSIGHTS_CALLS_PER_MINUTE` | `60` | Sustained calls per caller per minute over HTTP. `0` switches the limit off. |
| `BOE_INSIGHTS_CALLS_BURST` | `20` | How many calls a caller can make at once before waiting. |
| `BOE_INSIGHTS_MIN_INTERVAL` | `1.0` | Minimum seconds between request starts to boe.es, for the whole server. |
| `BOE_INSIGHTS_MAX_CONCURRENCY` | `2` | Most simultaneous requests to boe.es, for the whole server. |
| `BOE_INSIGHTS_DATA_DIR` | platform data dir; the container uses `/data/saved-searches` | Where the saved-searches database lives. Not safe to delete. |
| `BOE_INSIGHTS_CACHE` | `disk` | `disk` or `memory`. With `memory` and no data dir, saved searches are also kept in memory only. |
| `BOE_INSIGHTS_CACHE_DIR` | platform cache dir; the container uses `/data/cache` | Where the response cache lives. Safe to delete. |
| `FASTMCP_HOME` | FastMCP's platform data dir; the container uses `/data/fastmcp` | Read by FastMCP, not by this project. Holds the sign-in token store. See [Token store](#token-store). |

`*` in the allow-list means any GitHub account can sign in. Use it only if you really mean that.

## Network

The server binds to `127.0.0.1` by default. Over HTTP it speaks plain HTTP: it has no TLS of its own.

For anything beyond your own machine, put a reverse proxy that terminates TLS in front of it, and
keep the server itself on loopback or on a private network. Then:

- Set `BOE_INSIGHTS_BASE_URL` to the public `https` address that clients use. The server refuses an
  `http` base URL unless it is loopback, because sign-in codes and tokens travel over those
  redirects.
- If the proxy passes the original `Host` header (the public name), list that name in
  `BOE_INSIGHTS_ALLOWED_HOSTS`; with login, the host of the base URL is accepted automatically. A
  proxy that sends the upstream loopback address (for example `127.0.0.1:8000`) as `Host` is
  accepted as it is.
- Do not publish the port directly. The container image binds `0.0.0.0` inside the container; what
  matters is how you publish the port (for example `-p 127.0.0.1:8000:8000`). Because of that
  binding, a container in open HTTP mode logs a warning at every start; see
  [Host and Origin check](#host-and-origin-check).
- Put a network-level rate limit at the proxy. The server's own limit counts only MCP calls that do
  work (see [The call limit](#the-call-limit)). It does not cover the routes that need no sign-in
  (`/register`, `/authorize`, `/token`, `/consent`, `/auth/callback`,
  `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`),
  `/health`, or connection floods.

### Host and Origin check

Every HTTP request is checked, on every route including `/health`, and there is no setting that
turns this off. A request whose `Host` is not on the list gets `421`. The list is the loopback
names, `BOE_INSIGHTS_ALLOWED_HOSTS`, the host of the base URL with login, and the address the server
is bound to unless that is `0.0.0.0` or `::`. This is protection against DNS rebinding: a web page
that tricks a browser into talking to a server on your network. A request with an `Origin` header
must come from an allowed origin, from the server's own address, or, when the server is reached on
a loopback name, from a loopback address; any other origin gets `403`.

In open HTTP mode, the server logs a warning at start when it is bound to anything other than a
loopback address. It still starts: you are responsible for not exposing it. The container image
always binds `0.0.0.0`, so in open mode the warning appears at every start; with the port published
on `127.0.0.1` it is expected.

### Size and time limits

- A request body to `/mcp` may be at most 1 MiB. Any other route (sign-in forms, client
  registration) at most 64 KiB. Larger bodies get `413`. A declared `Content-Length` over the limit
  is refused at once; otherwise the bytes actually received are counted, whatever the header says.
- Long tool calls (gazette scans, saved-search checks, the amendment-chain explorer and the
  most-amended ranking) have a budget of about 40 seconds. They start no uncached BOE request in the
  last 5 seconds and return what they have with `complete: false`; the server instructions tell the
  model to call again.
- There is no request timeout in front of the server. Set one at the proxy.

## Sign-in

The server uses FastMCP's GitHub provider. It asks GitHub for at most the scope `read:user`, and for
no scope at all when the client asks for none: no write access and no repository access. It needs
only the numeric id and login that GitHub returns for any valid token. Clients that ask for `user` or `repo` are refused.

### Allow-list entries

Two forms, which can be mixed:

- A GitHub login, such as `octocat`. Matched case-insensitively against the login GitHub reports.
- `id:<number>`, such as `id:583231`, the account's numeric GitHub id.

Prefer `id:`. A login can be renamed, and a name that is released can later be taken by someone
else, who would then match your entry. The id never changes. If a login entry admits someone, the
server logs once at INFO that `id:<n>` is the safer form (the log line contains the login and the
id).

A person you refused sees what to ask for: the refusal page names their `id:<n>` and their login,
so you can copy it into the list. The page says nothing about who is on the list.

### Where an unlisted account is stopped

There are four places, so a mistake in one does not let someone in:

1. At the GitHub callback. The browser gets a 403 page titled "Access not allowed". The one-time
   code is deleted.
2. At the code exchange. If the callback was somehow passed, the exchange fails with
   `invalid_grant` and stores nothing.
3. On token renewal. A refresh for an account that is no longer admitted fails with
   `invalid_grant`, and the refresh token presented, its token mapping and the GitHub token behind
   it are deleted. Other clients of the same account are refused in the same way when they next
   renew. If GitHub cannot say who a token belongs to (revoked or unreachable), the renewal is
   refused but the records stay, since that is not a decision about the list.
4. On every request. Each request to `/mcp` re-checks the identity against the list, and every tool,
   resource and prompt is checked again on listing and use.

### How removal takes effect

The allow-list is read once, at startup. Changing `BOE_INSIGHTS_ALLOWED_USERS` has no effect on a
running server: **restart it**. After the restart, a removed account is refused on its next request
(`401`) and on its next token renewal. Its saved searches stay in the database until you delete
them (see [Data](#data)).

The server remembers for up to 5 minutes that a GitHub token passed the check, so the load on
GitHub stays low. That cache only affects the question "does GitHub still accept this token?"; the
allow-list itself is checked on every request.

### Redirect URIs

OAuth clients register where the browser should be sent back. By default only loopback addresses
are accepted (`http://localhost:*`, `http://127.0.0.1:*`, `http://[::1]:*`, any port), which is what
command-line clients such as Claude Code use. A web-based client needs its exact callback URL in
`BOE_INSIGHTS_ALLOWED_REDIRECT_URIS`. Without a restriction a page of an attacker's choosing could
be handed a sign-in code; that is why the restriction exists.

A registration is refused as a whole if any one of the redirect URIs it lists is not allowed, even
when the others are loopback addresses. According to their documentation, Cursor and VS Code
register a web address besides a loopback one (`https://www.cursor.com/agents/mcp/oauth/callback`,
`https://vscode.dev/redirect`), so they can only register once that address is listed. Listing it
means that sign-in codes for that client may be delivered through that site. See
[Redirect addresses](run/github-login.md#redirect-addresses).

### Client registrations and metadata documents

A client identifies itself in one of two ways, and neither needs an account: the allow-list is
applied at sign-in, not here.

- **Registration.** The client registers at `/register`. Registrations are open to anyone who can
  reach the server.
- **Metadata document.** The client gives, as its client id, the `https` address of a document that
  describes it, and the server fetches that document. Claude Code works this way: in the real
  sign-in test its client id was `https://claude.ai/oauth/claude-code-client-metadata`. The address
  is chosen by the client, so anyone who can reach the server can make it fetch an `https` address
  of their choosing. FastMCP applies protection against server-side request forgery to that fetch.

Put a request rate limit on `/authorize` and `/register` at the proxy.

### Secrets

- The signing key signs the tokens the server issues and is the key under which the token store is
  encrypted. Generate a random one, for example with `openssl rand -hex 32`.
- The GitHub client secret comes from your OAuth app's page.
- Keep both outside the image and the repository: a secrets manager, or an environment file with
  mode 0600 that is not committed. The server never prints either, and no startup error repeats
  them.
- Changing the signing key signs everybody out. FastMCP then uses a different folder of the token
  store, so clients register and sign in again. The old folder stays; you can delete it.

### Token store

FastMCP keeps client registrations, sign-in transactions and issued tokens as encrypted files under
`$FASTMCP_HOME/oauth-proxy/<key fingerprint>/`. They are encrypted with a key derived from the
signing key. If you do not set `FASTMCP_HOME`, it is FastMCP's per-user data directory, which is a
poor choice for a service. The folder must:

- persist across restarts (otherwise everybody signs in again after each deploy);
- be private to the account that runs the server. FastMCP does not restrict it: it creates the
  folder with the default permissions (seen as `drwxr-xr-x`, readable by other accounts), so make
  it private yourself, for example with `chmod 700` on `$FASTMCP_HOME`. The container image
  creates `/data` with mode 0700 for the server's user; a bind-mounted folder keeps the
  permissions you give it;
- be backed up only if you accept that a copy holds encrypted GitHub tokens (they are useless
  without the signing key).

The container image sets `FASTMCP_HOME=/data/fastmcp`, on the volume.

#### Client registrations are kept without expiry

FastMCP stores every registration made at `/register` with no expiry and never removes it, even
when you restart. Each is small, but they accumulate, and registration needs no account. To clear
them: stop the server, delete the `$FASTMCP_HOME/oauth-proxy/<key fingerprint>/` folder, and start
it again. This also signs everyone out. A client is then expected to register again, or fetch its
metadata document again, and ask the person to sign in; this has not been tested for any client,
Claude Code included. There is no command to remove a single registration. Putting a request limit
on `/register` at the proxy keeps their number down.

## Limits and abuse

### The call limit

Each caller may make `BOE_INSIGHTS_CALLS_PER_MINUTE` calls a minute, with a burst of
`BOE_INSIGHTS_CALLS_BURST`. It is a token bucket. A caller over the limit gets a message that says
how many seconds to wait. Only tool calls, resource reads and prompt requests are counted. Listing
tools, the handshake and pings are not.

Who is "a caller":

- Signed in: the GitHub numeric id. One bucket per person, whatever device or address they use.
- Open HTTP: one bucket for everybody together. Without a login nothing in a request can be trusted
  to tell callers apart (an address is whatever the client or the proxy says), so the limit protects
  the server as a whole, and one noisy client can use up the share of all the others.

The server does not use IP addresses at all, so putting it behind a proxy does not change which
bucket a call falls into. For per-address limits, use the proxy.

The call limit is not a limit on the cost of a call. One call to a gazette scan or a trend tool can
cause many requests to boe.es. That is held down by the shared limiter described in
[The shared BOE budget](#the-shared-boe-budget).

### The shared BOE budget

Every request to boe.es, from every user, goes through one limiter: at most one request start per
`BOE_INSIGHTS_MIN_INTERVAL` seconds (default 1) and at most `BOE_INSIGHTS_MAX_CONCURRENCY` (default
2) at a time. So all your users share one polite budget, about one request a second. Results are
cached (search results for 30 minutes, law metadata and amendment analysis for 6 hours, code tables
for 7 days), so most repeated questions cost nothing. Many users with different questions will slow
each other down. Do not lower the interval to fix that: it is the courtesy the BOE API is asked for,
and no rate-limit signal has ever been seen from it (see `api-notes.md` in the repository).

### Saved searches

At most 20 per owner. A name is unique per owner. The words filter is at most 200 characters. A
check shows at most `limit` items per search (default 20, maximum 50) and at most 60 items over all
searches in one call, and it can look back at most 90 days.
There is no limit on the number of owners other than your allow-list.

## Data

| What | Where | Personal data | Safe to delete |
| --- | --- | --- | --- |
| Saved searches | `saved_searches.sqlite3` in `BOE_INSIGHTS_DATA_DIR` (file mode 0600, folders 0700) | The owner `github:<numeric id>` on each row, plus the filters the person chose. The login is not stored there. | No: that deletes people's searches. |
| Response cache | `BOE_INSIGHTS_CACHE_DIR` | None. Public BOE responses. | Yes. |
| Token store | `$FASTMCP_HOME/oauth-proxy/...` | Encrypted GitHub access tokens (and refresh tokens, if GitHub issued them), each with the id of the MCP client it was issued to. No GitHub id or login is stored there. | Yes, but everybody signs in again. |
| Logs | stderr, and the access log | See [Logs](#logs). | Yes: the server never reads them back. Keep them only as long as your retention rule says. |

Over open HTTP and stdio no accounts or tokens are stored. Over open HTTP the access log still
holds client addresses (see [Logs](#logs)). Over stdio the owner is `local`.

**Backup.** Back up the saved-searches folder. SQLite is a single file; stop the server, or use the
`sqlite3 .backup` command, for a consistent copy. The cache needs no backup.

**Deletion of a person's data.** There is no command for it. Stop the server and delete the rows
whose `owner` is `github:<id>` from the `saved_searches` table, or ask the person to delete their
searches with the `delete_saved_search` tool. Removing someone from the allow-list does not delete
their rows. If a person asks you to erase their data, also clear the token store (see [Token store](#token-store)), which
holds their GitHub token. The token store does not record whose token is whose, so clearing it
means clearing it for everybody.

### Logs

The server writes two logs, both to the process output.

**The access log** (uvicorn) shows the method, path and status of each request and the client
address. The server removes the query string from every line before it is written, because the
sign-in routes carry one-time codes and transaction ids there. Tokens travel in headers and bodies,
which the access log does not show. The client address is the address the server sees, which behind
a proxy is the proxy's unless you configure forwarding.

**The server log** (stderr) holds the startup warnings, one INFO line per user admitted by a login
entry of the allow-list (with their login and id, once per process), and FastMCP's own messages.
The server's own code does not log a token or a secret. FastMCP's messages can name a sign-in
transaction id, which is short-lived (seen: `IdP callback with invalid transaction ID: …`). No
one-time code or token was seen in them.

## Things you will see and should not worry about

- At sign-in, FastMCP's token check calls GitHub's `/user` and also `/user/repos`, even though only
  `read:user` is granted. The second call is how FastMCP reads the scopes of a token. The server
  does not use the answer and asks for no repository access; this page cannot say exactly what GitHub
  returns to the second call. These checks are cached for 5 minutes.
- The consent page, shown at each sign-in, comes from FastMCP. It shows this project's logo and the
  server name `boe-insights`, linked to the project repository. Its "Why am I seeing this?" note
  still mentions FastMCP and links to gofastmcp.com. FastMCP's error pages, including the 403
  "Access not allowed" page, keep the FastMCP logo, which the browser loads from gofastmcp.com.
- Registrations that accumulate (see
  [Client registrations are kept without expiry](#client-registrations-are-kept-without-expiry)).
- With a login entry in the allow-list, one INFO line per person suggesting `id:<n>`.
- In the container in open HTTP mode, a warning at every start that the server is reachable from
  other machines. It comes from the `0.0.0.0` binding inside the container; with the port published
  on `127.0.0.1` it is expected.
- With an `http://localhost` base URL, FastMCP's warning "Using non-secure cookies for development;
  deploy with HTTPS for production." at every start.

## Upgrades

`login.py` overrides private parts of FastMCP: `_handle_idp_callback`, `exchange_authorization_code`
and `exchange_refresh_token`, and reads the private stores `_code_store`, `_refresh_token_store`,
`_jti_mapping_store`, `_upstream_token_store` and the `_token_validator`, and imports the private
function `_hash_token`. FastMCP does not promise that these stay the same.

For that reason the dependency is limited to `fastmcp>=4.2.4,<4.3` and the exact version is fixed by
`uv.lock`; the container builds from that lock file. Do not upgrade FastMCP on your own deployment
by hand. When a new version of this project moves to a new FastMCP, it has been through the sign-in
tests (`tests/test_login_flow.py` and the other `test_login_*` and `test_launch_login.py` files). If
you do upgrade it yourself, re-run them first: a mismatch can leave the allow-list unenforced at one
of the four places above, and the tests are what show it.

## GitHub OAuth app notes

- The form field is called "Redirect URIs" (it used to be "Authorization callback URL"). Its value
  is your base URL followed by `/auth/callback`; see [GitHub sign-in](run/github-login.md) for the
  exact steps.
- "Expire user access tokens" is on by default for new apps. Sign-in works with it on. Token
  renewal after a real expiry has not been tested (see [Not tested](#not-tested)).
- Create the OAuth app under the account that should own it, and keep its secret as described in
  [Secrets](#secrets).

## Not tested

Say so plainly: these have not been tried, and you may be the first.

- Token renewal after a GitHub token has really expired. The renewal logic is tested with GitHub
  replaced by a fake.
- Running behind a real HTTPS reverse proxy. Host, Origin and base URL handling is tested, but not
  with a real proxy in front.
- Clients other than Claude Code in the sign-in flow, including how they show the 403 "Access not
  allowed" page.
- Sign-in against the container image. The image was built for linux/amd64 and linux/arm64; only
  the arm64 one was run, and in open HTTP mode. The amd64 one was only built and checked to import.
- A client registering again after the token store was deleted, for any client, Claude Code
  included.
- Load with many users. The limits above are by design, not measured.

The author did a real sign-in against GitHub during development (it found the two gaps fixed in
commit `e1dbc6d`). It was done with Claude Code only, on `localhost`, not behind a proxy and not
against the container.

## Your duties towards the BOE

The data is the BOE's, and its reuse conditions apply to your instance:

- Show the attribution "Basado en datos de la Agencia Estatal Boletín Oficial del Estado" with a
  link to <https://www.boe.es>. The server instructions and the prompts tell the model to pass it
  on; do not strip it from what you show to people.
- Consolidated texts are informational and have no official legal value. Do not remove that label.
- Do not present your instance as an official service, or as the BOE's. Do not alter the meaning of
  legal text.
- Keep the identifying User-Agent (`boe-insights-mcp/<version>` with the project URL) and the polite
  request rate. Do not lower `BOE_INSIGHTS_MIN_INTERVAL` below 1 second or raise the concurrency
  without a reason.

## Before you go live

- [ ] The server is behind a reverse proxy with TLS, and the port is not published directly.
- [ ] `BOE_INSIGHTS_BASE_URL` is the public `https` address.
- [ ] If the proxy passes the public `Host` header and sign-in is off, the public name is in
  `BOE_INSIGHTS_ALLOWED_HOSTS`.
- [ ] The proxy has a request rate limit and a request timeout, including for `/register` and
  `/authorize`.
- [ ] If clients such as Cursor or VS Code will sign in, their web redirect addresses are in
  `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS`.
- [ ] Sign-in is on, unless the instance is private. The allow-list uses `id:<n>` entries.
- [ ] The signing key is random, at least 32 characters, and kept outside the image and the repo.
- [ ] The GitHub client secret is kept the same way.
- [ ] `FASTMCP_HOME` and `BOE_INSIGHTS_DATA_DIR` are on persistent storage, private to the server
  account (FastMCP does not make `FASTMCP_HOME` private: `chmod 700` it). The saved-searches folder
  is backed up.
- [ ] You know that allow-list changes need a restart.
- [ ] Logs are kept for a limited time. You have decided who may read them.
- [ ] The attribution and the "informational, not official" label are intact, and your instance does
  not claim to be official.
- [ ] You have read the "Not tested" list and accept it.
