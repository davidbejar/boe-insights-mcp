# boe-insights-mcp container image (HTTP transport).
#
# Build (native architecture). `--format docker` is REQUIRED with Podman: the default OCI
# image format has no HEALTHCHECK field, so Podman silently drops the instruction below.
#   podman build --format docker -t localhost/boe-insights-mcp:dev -f Containerfile .
#
# Multi-architecture (linux/amd64 + linux/arm64; nothing below is architecture-specific):
#   podman build --format docker --platform linux/amd64,linux/arm64 \
#       --manifest localhost/boe-insights-mcp:dev -f Containerfile .
#
# Run (publish on loopback only; the server has no authentication of its own by default):
#   podman run --rm --name boe-insights -p 127.0.0.1:8000:8000 \
#       --read-only --tmpfs /tmp --cap-drop=all --security-opt no-new-privileges \
#       -v boe-insights-data:/data localhost/boe-insights-mcp:dev
#   curl http://127.0.0.1:8000/health
#
# Pin by digest for reproducible builds: replace each tag below with
# `tag@sha256:<digest>`, where the digest comes from
# `podman manifest inspect <image:tag>` (the index digest, so both architectures still
# resolve) or `skopeo inspect --raw docker://<image:tag> | sha256sum`.

ARG PYTHON_IMAGE=docker.io/library/python:3.13-slim-trixie

# ---------------------------------------------------------------------------------------
# Build stage: install the locked dependencies and the package into a virtualenv.
# ---------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build

# uv comes from its official image as a single static binary; it never reaches the final stage.
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv

# - COMPILE_BYTECODE: the runtime filesystem may be read-only, so .pyc files must be
#   produced at build time; they are then part of the image layer.
# - LINK_MODE=copy: the uv cache is not copied to the final stage, so hardlinks would break.
# - PYTHON_DOWNLOADS=never: use the base image's interpreter, the same one the final stage has.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# WORKDIR /app so the venv is created at its final path: console-script shebangs are absolute.
WORKDIR /app

# Dependencies first, in their own layer: it is rebuilt only when pyproject.toml or uv.lock
# change, not on every source edit. --no-install-project skips the package itself, which
# needs src/, README.md and LICENSE (all referenced by pyproject.toml / hatchling).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY README.md LICENSE ./
COPY src ./src
# --no-editable installs a real copy into site-packages, so the final stage can copy
# just /app/.venv and leave the source tree behind. --frozen refuses to touch uv.lock.
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------------------
# Final stage: slim Python plus the virtualenv. No uv, no compiler, no sources, no tests.
# ---------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="boe-insights-mcp" \
      org.opencontainers.image.description="MCP server with analytical tools over Spain's BOE (Boletín Oficial del Estado) open data: law version history, amendment analysis and legislative trends." \
      org.opencontainers.image.source="https://github.com/davidbejar/boe-insights-mcp" \
      org.opencontainers.image.licenses="MIT"

# Fixed numeric UID/GID: stable across rebuilds, so ownership of a bind-mounted or named
# volume does not drift, and Kubernetes `runAsNonRoot` can verify a numeric user. No login
# shell and no home directory: nothing needs to write there.
RUN groupadd --gid 10001 boe \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin boe \
 && mkdir /data \
 && chown 10001:10001 /data \
 && chmod 0700 /data

# Same absolute path as in the build stage (see WORKDIR above): shebangs and pyvenv.cfg hard-code it.
COPY --from=build /app/.venv /app/.venv

# /data is the only writable location. Because it is created and chowned above, a fresh
# named volume mounted there inherits that ownership on first use.
#  - PATH: puts the console script on PATH so ENTRYPOINT needs no absolute path.
#  - PYTHONDONTWRITEBYTECODE: nothing under /app may be written at runtime (bytecode is precompiled).
#  - PYTHONUNBUFFERED: log lines reach `podman logs` immediately.
#  - HOST=0.0.0.0: inside a container 127.0.0.1 would be unreachable from the published port;
#    exposure is controlled by how the port is published (-p 127.0.0.1:8000:8000), not here.
#  - DATA_DIR / FASTMCP_HOME hold state that must persist; CACHE_DIR is public data that is
#    safe to lose, but lives on the volume to survive restarts and stay off the read-only root.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOE_INSIGHTS_TRANSPORT=http \
    BOE_INSIGHTS_HOST=0.0.0.0 \
    BOE_INSIGHTS_PORT=8000 \
    BOE_INSIGHTS_DATA_DIR=/data/saved-searches \
    BOE_INSIGHTS_CACHE_DIR=/data/cache \
    FASTMCP_HOME=/data/fastmcp

VOLUME /data
EXPOSE 8000
USER 10001:10001

# The probe targets 127.0.0.1 inside the container (the server listens on 0.0.0.0, so loopback
# works, and the Host header "127.0.0.1:<port>" is accepted by the host check). It uses the
# standard library because the image has no curl or wget. urlopen raises on non-2xx, which
# makes the interpreter exit non-zero. The port is read from the environment so the check
# follows BOE_INSIGHTS_PORT if the operator changes it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('BOE_INSIGHTS_PORT', '8000'), timeout=3)"]

# Exec form: the server is PID 1 and receives SIGTERM directly from `podman stop`.
ENTRYPOINT ["boe-insights-mcp"]
