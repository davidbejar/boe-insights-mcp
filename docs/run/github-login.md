# GitHub login

GitHub login is optional. It is off by default, and over stdio it does nothing. When you serve the server [over HTTP](http.md), it makes people sign in with a GitHub account, and only accounts on a list you write are let in. The list is closed: an empty list lets nobody in.

Turning it on also gives each person their own saved searches. Without login, the saved-search tools are not offered over HTTP.

This page is about setting it up. Threats, hardening and proxy advice are in [Security and operations](../operating.md).

## What you need

- A GitHub account that can create an OAuth app.
- The server reachable at a URL your users type into their client. For a first try on your own machine, `http://localhost:8000` is enough.
- For anything other than localhost, an `https` address. The server refuses to start with an `http` base URL unless its host is `localhost` or another loopback address.

## 1. Create a GitHub OAuth app

1. On GitHub, open **Settings**, then **Developer settings**, then **OAuth Apps**, then **New OAuth App**.
2. Fill in the form:
    - **Application name**: anything your users will recognise.
    - **Homepage URL**: any address, for example your server's base URL.
    - **Redirect URIs**: the server's base URL followed by `/auth/callback`. For a local test: `http://localhost:8000/auth/callback`. For a public server: `https://boe.example.org/auth/callback`.
3. Register the app, then generate a **client secret**. Copy the **Client ID** and the secret.

GitHub's form used to call the redirect field "Authorization callback URL". It is now "Redirect URIs". It is the same thing. The path `/auth/callback` is FastMCP's default, and this server does not change it. The address must match the server's base URL exactly, including `http` or `https` and the port.

GitHub has a setting "Expire user access tokens", on by default. Sign-in works with it on. Leave it on.

## 2. Configure the server

Login needs four variables, all or none. If only some are set, the server refuses to start and names the missing ones. Add the allow-list as a fifth.

```bash
export BOE_INSIGHTS_GITHUB_CLIENT_ID=<client id from GitHub>
export BOE_INSIGHTS_GITHUB_CLIENT_SECRET=<client secret from GitHub>
export BOE_INSIGHTS_AUTH_SIGNING_KEY=$(openssl rand -hex 32)
export BOE_INSIGHTS_BASE_URL=http://localhost:8000
export BOE_INSIGHTS_ALLOWED_USERS=octocat,id:1234567

uvx boe-insights-mcp --transport http
```

(Before release 0.1.0, use `uvx --from git+https://github.com/davidbejar/boe-insights-mcp boe-insights-mcp --transport http` or `uv run boe-insights-mcp --transport http` from a clone.)

Keep the signing key. If you generate a new one every time you start, people have to sign in again each time. Put the secret and the key in a private file or a secrets manager, never in the repository.

### Settings

| Variable | Meaning |
|---|---|
| `BOE_INSIGHTS_GITHUB_CLIENT_ID` | The OAuth app's client ID. |
| `BOE_INSIGHTS_GITHUB_CLIENT_SECRET` | The OAuth app's client secret. |
| `BOE_INSIGHTS_AUTH_SIGNING_KEY` | A secret of at least 32 characters. The server uses it to sign the tokens it hands to clients. Changing it signs everybody out, and the sign-in records the server kept under the old key are no longer used. |
| `BOE_INSIGHTS_BASE_URL` | The server's public address as users reach it: `http(s)://host[:port]`, with no path, no query and no credentials. The server's address for MCP is this plus `/mcp`. `https` is required unless the host is loopback. |
| `BOE_INSIGHTS_ALLOWED_USERS` | Comma-separated allow-list. See below. Empty means nobody can sign in, and the server logs a warning. |
| `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` | Optional. Extra addresses a client may be sent back to after sign-in, each an exact `https` URL (`http` only for loopback), with no wildcard. Loopback addresses (`localhost`, `127.0.0.1`, `[::1]`, any port) are always allowed, which is what command-line clients such as Claude Code use. |

Rules the server enforces at start-up:

- Setting `BOE_INSIGHTS_ALLOWED_USERS` or `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` without the four login variables is an error. Otherwise you could believe a list was protecting the server when it was not.
- Over stdio the login variables are ignored, with a warning. Login only applies to `--transport http`.
- The host of the base URL is added to the allowed hosts and origins automatically. See [Run over HTTP](http.md#allowed-hosts-and-origins).

Using a container? Pass the same variables with `-e` or `--env-file`. See [Run in a container](container.md). Sign-in against the container image was not tested.

## The allow-list

`BOE_INSIGHTS_ALLOWED_USERS` takes entries separated by commas. Each entry is one of:

| Entry | Meaning |
|---|---|
| `octocat` | A GitHub username. Not case-sensitive. |
| `id:1234567` | A GitHub account by its numeric id. |
| `*` | Every GitHub account. This is not a list any more, so use it only if you really mean it. |

A wrong entry stops the server at start-up with a message that shows the entry.

**Prefer `id:` entries.** A username entry stops matching if the person renames their account, and the old name can later be taken by someone else, who would then match the entry. The numeric id never changes. When a username entry lets someone in, the server logs once that `id:<n>` is the form that survives a rename.

To find a person's id, ask them to try to sign in. They get the "Access not allowed" page described below, which shows the id. You can also look it up through GitHub's public API: `curl https://api.github.com/users/<login>` returns an `id` field.

### Add or remove a person

1. Edit the value of `BOE_INSIGHTS_ALLOWED_USERS`.
2. **Restart the server.** The list is read once at start-up. A change does not apply to a running server.

After a restart:

- A person you added can sign in.
- A person you removed is refused on their next request. Their client gets "401 Unauthorized" and cannot renew its token, even though it holds one that was valid before. A new sign-in attempt shows them the "Access not allowed" page.
- Removing a person does not delete their saved searches. Nothing in the server does that.

## What a user sees

The first time a client connects to `http://localhost:8000/mcp` (or your public address):

1. The server answers that sign-in is needed, and the client opens a browser window. How this looks depends on the client. See your [client page](clients/claude-code.md).
2. The server's own consent page asks whether to allow the client. This page comes from the server, not from GitHub.
3. The browser goes to GitHub. The first time only, GitHub shows its authorize page for your OAuth app.
4. GitHub sends the browser back to the server, and the client is signed in.

The server asks GitHub only for the `read:user` scope, which is read-only access to the public profile. The server needs nothing but the account's numeric id and login name. It does not ask for access to repositories or to anything else.

After that the client keeps its token and renews it by itself. Each person's saved searches are stored under their GitHub id, and other people cannot see them.

### Someone who is not on the list

A GitHub account that is not on the allow-list does not get through. The browser shows a page titled "Access not allowed" with the message that the GitHub account is not allowed to use this server, and that the person should ask the operator to add `id:<n>` (followed by their GitHub login). The page does not say who is on the list. The status is 403.

Send the `id:<n>` text to the operator, who adds it to the list as described above.

## Token renewal

With "Expire user access tokens" on, GitHub's own tokens expire. The server's design lets a client renew its token without a new sign-in, and it refuses renewal for a person who has been removed from the list. A real renewal after a GitHub token has actually expired has not been observed yet. If a client asks you to sign in again, do so.

## Check it without a client

With the server running as above, an unauthenticated request is turned away:

```bash
curl -i -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}'
```

The answer is `401 Unauthorized` with a `WWW-Authenticate` header that points to the sign-in metadata. `/health` still answers without a sign-in.

## What was tested

- Sign-in from Claude Code over HTTP on the same machine, with an OAuth app from GitHub, "Expire user access tokens" on, an allowed account and a refused one.
- The server's start-up checks, the 401 answer and the sign-in metadata, with placeholder credentials.

Not tested:

- Behind a real HTTPS reverse proxy.
- Clients other than Claude Code.
- Sign-in against the container image.
- Renewal after GitHub's token has really expired.

## See also

- [Run over HTTP](http.md)
- [Security and operations](../operating.md)
- [Connect Claude Code](clients/claude-code.md)
