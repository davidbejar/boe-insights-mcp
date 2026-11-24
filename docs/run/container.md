# Run in a container

The container image runs the server over HTTP. It is built from the `Containerfile` in the repository root. The commands below use Podman. Docker works the same way: replace `podman` with `docker`, except where noted.

!!! note "Release status"
    The image `ghcr.io/davidbejar/boe-insights-mcp` is published together with release 0.1.0. Until then, build the image yourself from a clone (next section).

## Get the image

After the release:

```bash
podman pull ghcr.io/davidbejar/boe-insights-mcp:0.1.0
```

Today, from a clone of the repository:

```bash
git clone https://github.com/davidbejar/boe-insights-mcp.git
cd boe-insights-mcp
podman build --format docker -t localhost/boe-insights-mcp:dev -f Containerfile .
```

With Docker, drop `--format docker`: `docker build -t localhost/boe-insights-mcp:dev -f Containerfile .`. With Podman the flag is required. Without it Podman drops the image's health check silently.

The commands below use the name `localhost/boe-insights-mcp:dev`. After the release, use `ghcr.io/davidbejar/boe-insights-mcp:0.1.0` instead.

## Run it

```bash
podman run --rm --name boe-insights -p 127.0.0.1:8000:8000 \
    --read-only --tmpfs /tmp --cap-drop=all --security-opt no-new-privileges \
    -v boe-insights-data:/data localhost/boe-insights-mcp:dev
```

What each part does:

- `-p 127.0.0.1:8000:8000` publishes the port on your own machine only. This is the line that decides who can reach the server. Inside the container the server listens on `0.0.0.0`, because otherwise the published port would not work.
- `--read-only --tmpfs /tmp --cap-drop=all --security-opt no-new-privileges` lock the container down. The server needs nothing more than `/data` to write to.
- `-v boe-insights-data:/data` keeps your data in a named volume that survives the container.

Check it:

```bash
curl http://127.0.0.1:8000/health
```

The answer is `{"status":"ok"}`. The MCP address is `http://127.0.0.1:8000/mcp`. Connect a client as described on the [client pages](clients/generic.md).

The image defines a health check that calls `/health` from inside the container. `podman ps` shows the state.

!!! warning "No sign-in by default"
    As started above, anyone who can reach the published port can use the server. Keep the `127.0.0.1:` in `-p`, or turn on [GitHub login](github-login.md). See [Security and operations](../operating.md) before publishing it more widely.

Over open HTTP the saved-search tools are not offered. See [Run over HTTP](http.md#what-is-available-over-open-http).

## The volume

The image keeps all its state under `/data`, the only place it writes:

| Path in the container | Holds | Set by |
|---|---|---|
| `/data/saved-searches` | Saved searches (`saved_searches.sqlite3`). Not safe to delete. | `BOE_INSIGHTS_DATA_DIR` |
| `/data/cache` | The response cache. Safe to delete. | `BOE_INSIGHTS_CACHE_DIR` |
| `/data/fastmcp` | With GitHub login, the server's sign-in records (registered clients and tokens, stored encrypted). Without login it stays empty. | `FASTMCP_HOME` |

The server runs as user and group `10001`, not root. A named volume inherits the right owner the first time it is used. If you use a bind mount instead, the folder must be writable by `10001`:

```bash
mkdir -p ./boe-data && podman unshare chown 10001:10001 ./boe-data
podman run --rm -p 127.0.0.1:8000:8000 -v ./boe-data:/data localhost/boe-insights-mcp:dev
```

`podman unshare` is for rootless Podman. With Docker or rootful Podman, use `sudo chown 10001:10001 ./boe-data`. The bind-mount commands were not run for this page.

## Environment variables

The image sets these. You can override any of them with `-e NAME=value`.

| Variable | Value in the image |
|---|---|
| `BOE_INSIGHTS_TRANSPORT` | `http` |
| `BOE_INSIGHTS_HOST` | `0.0.0.0` |
| `BOE_INSIGHTS_PORT` | `8000` |
| `BOE_INSIGHTS_DATA_DIR` | `/data/saved-searches` |
| `BOE_INSIGHTS_CACHE_DIR` | `/data/cache` |
| `FASTMCP_HOME` | `/data/fastmcp` |

All the other settings work as on the other pages, passed with `-e`: the [HTTP settings](http.md#every-http-setting) (allowed hosts and origins, call limit), the pacing settings (`BOE_INSIGHTS_MIN_INTERVAL`, `BOE_INSIGHTS_MAX_CONCURRENCY`) and the [GitHub login settings](github-login.md#settings).

If you change `BOE_INSIGHTS_PORT`, change the right side of `-p` to match. The health check follows the variable.

Put secrets (the GitHub client secret and the signing key) in an environment file rather than on the command line, so they do not appear in your shell history or in `ps`:

```bash
podman run --rm -p 127.0.0.1:8000:8000 --env-file boe.env -v boe-insights-data:/data localhost/boe-insights-mcp:dev
```

Keep that file private (`chmod 600 boe.env`).

## Stop, logs, update

```bash
podman logs boe-insights     # server log
podman stop boe-insights     # clean shutdown; the server is the main process and gets the signal
```

To update, pull or build the new image and start a new container with the same volume.

## Not tested

- Sign-in (GitHub login) against the container image.
- The bind-mount commands above.
- Running the published image: it does not exist yet.

## See also

- [Run over HTTP](http.md)
- [GitHub login](github-login.md)
- [Security and operations](../operating.md)
