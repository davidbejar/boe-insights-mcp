# CLAUDE.md

## What this is

`boe-insights-mcp` is an open-source MCP (Model Context Protocol) server, written in Python on
FastMCP 4.x, that gives LLMs analytical tools over Spain's BOE (Boletín Oficial del Estado) open
data: law version history, amendment analysis and legislative trends.

## Commands

- Install: `uv sync --all-groups`
- Test (no network): `uv run pytest`
- Live tests (hit the real BOE API; use sparingly): `uv run pytest -m live`
- Lint and format: `uv run ruff check . && uv run ruff format .`
- Type check: `uv run pyright`

## Layout

- `src/boe_insights_mcp/` - the package.
- `tests/` - pytest. `tests/fixtures/boe/` holds recorded real API responses.
- `docs/` - `docs/api-notes.md` records verified BOE API behaviour. Read it before touching the client.

## Conventions

- Python 3.11+, full type hints, async I/O.
- pydantic v2 models for all API data and tool outputs.
- Tests first for logic (client, cache, rate limiter, analytics).
- Unit tests never hit the network: use respx and fixtures.
- Code, comments, tool names and docs are in English. Spanish legal text is passed through unaltered.
- FastMCP 4.x: check the live docs at https://gofastmcp.com before using any API. Most online
  examples target older versions.

## Documentation standard

- Every module has a docstring saying what it is responsible for and how it relates to its
  neighbours.
- Every function, method and class has a docstring, private helpers included. One line is enough
  when the name and signature already say most of it; say what it returns and what it raises when
  that is not obvious.
- Non-obvious logic gets a `#` comment that explains WHY, not what: API quirks (cite the section
  of `docs/api-notes.md`), concurrency and cancellation reasoning, precedence rules, deliberate
  trade-offs, anything a reader would otherwise have to reverse-engineer. Do not write comments
  that restate the code.
- Model-facing text is different from code documentation. The docstrings of the tool functions in
  `server.py`, the `Field(description=...)` texts on tool parameters and on the result models in
  `schemas.py`, and the server instructions are sent to the language model on every session. Keep
  them short and precise, change them only deliberately, and never put developer notes in them:
  explain those functions to human readers with `#` comments instead.

## BOE reuse conditions (every tool result must honour these)

- Include the attribution "Basado en datos de la Agencia Estatal Boletín Oficial del Estado",
  with a link to https://www.boe.es.
- Label consolidated texts as informational, not official.
- State the data's last-update date.
- Never alter the meaning of legal text or imply official status.

## API etiquette

- All requests go through the shared rate limiter and cache.
- Send an identifying User-Agent.
- No API key exists.

## Docs rule

- Any change to `README.md` must be mirrored in `README.es.md` in the same pull request, and vice versa.

## Workflow

- One branch and one pull request per increment.
- Conventional commit messages (e.g. `feat:`, `fix:`, `docs:`, `test:`, `chore:`).
