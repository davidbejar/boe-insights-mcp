# boe-insights-mcp

**English** | [Español](README.es.md)

[![CI](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that gives AI assistants such as Claude analytical tools over the open data of Spain's BOE (Boletín Oficial del Estado). Instead of one tool per API endpoint, the tools are shaped around the questions people actually ask: "is this law still in force?", "what changed in this article?", "how has legislation on X evolved?".

**Status:** early development. Four tools are available today (law lookup, search, status overview and code lookup); everything else is on the [roadmap](#roadmap). Not yet published on PyPI: run it from source.

## What you can ask

| Prompt | Tool |
|---|---|
| "What is the BOE identifier of Real Decreto-ley 6/2019?" | `resolve_reference` |
| "Is Ley 40/2015 still in force, and how much has it been amended?" | `resolve_reference`, then `get_law_overview` |
| "Was Ley 30/1992 repealed, and on what date?" | `resolve_reference`, then `get_law_overview` |
| "Which organic laws about data protection are in the consolidated legislation?" | `search_legislation` |
| "List the state laws on energy published in 2020." | `search_legislation` |
| "What are the most recently published laws?" | `search_legislation` |
| "Which department code does the Agencia Española de Protección de Datos have?" | `find_codes`, then `search_legislation` |

## Tools

| Tool | Question it answers | Key parameters |
|---|---|---|
| `resolve_reference` | What is the BOE identifier of "Ley 40/2015"? | `reference` (a name such as `Ley Orgánica 3/2018`, `RDL 6/2019`, an identifier, or a title fragment) |
| `search_legislation` | Which laws are about X, or were published recently? | `title`, `text`, `rank`, `department_code`, `scope`, `subject_code`, `published_from`, `published_to`, `sort_by`, `descending`, `limit` (1-50), `offset` |
| `get_law_overview` | Is this law in force, and how much has it changed? | `law_id` (e.g. `BOE-A-2015-10566`) |
| `find_codes` | What is the code of this department, subject or rank (for the `search_legislation` filters)? | `kind` (`department`, `subject` or `rank`), `text`, `limit` (1-50) |

All four tools are read-only. `resolve_reference` returns `resolved`, `ambiguous` (with candidates and a hint) or `not_found`. `search_legislation` pages with `next_offset`, because the API gives no total count. `get_law_overview` returns a `status` (`in_force`, `partially_repealed`, `repealed`, `annulled`, `validity_exhausted` or `not_yet_in_force`) derived from the BOE flags and the amendment references (so it is an indication, not a legal determination), with a plain-English note, plus counts and samples of the norms that amend the law or that it amends.

`find_codes` returns the matching `{code, text}` pairs. Every result carries a `provenance` block. Trimmed output of `get_law_overview` for `BOE-A-2015-10566` (recorded test fixtures, no live request):

```json
{
  "identifier": "BOE-A-2015-10566",
  "title": "Ley 40/2015, de 1 de octubre, de Régimen Jurídico del Sector Público.",
  "rank": "Ley",
  "status": "partially_repealed",
  "status_note": "The BOE analysis has 1 reference in which later norms repeal this law or parts of it. The BOE flag only marks whole repeals and is not set here, so the law may be repealed in full or in part: check the amendment summary.",
  "amendments": { "affected_by_count": 24, "affects_count": 19 },
  "provenance": {
    "source": "Agencia Estatal Boletín Oficial del Estado",
    "attribution": "Basado en datos de la Agencia Estatal Boletín Oficial del Estado",
    "source_url": "https://www.boe.es",
    "generated_at": "2026-11-21T06:51:30.747423Z",
    "data_updated_at": "2026-11-20T08:50:00Z",
    "notice": "Consolidated texts are provided for information only and have no official legal value. Only the texts published in the official gazette are authentic."
  }
}
```

## Quick start

Prerequisites: Python 3.11+ and [uv](https://docs.astral.sh/uv/). No API key is needed: the BOE open data API is public.

Run from source:

```bash
git clone https://github.com/davidbejar/boe-insights-mcp.git
cd boe-insights-mcp
uv sync
```

The server speaks MCP over stdio, so you normally do not start it by hand: your MCP client does.

**Claude Desktop.** Add this to `claude_desktop_config.json` and restart the app (use the absolute path of your clone):

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

**Claude Code:**

```bash
claude mcp add boe-insights -- uv --directory /absolute/path/to/boe-insights-mcp run boe-insights-mcp
```

Or commit the same server definition in a project-level `.mcp.json`:

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

**Without cloning**, uv can fetch and run it straight from GitHub:

```bash
uvx --from git+https://github.com/davidbejar/boe-insights-mcp boe-insights-mcp
```

In a client configuration, use `"command": "uvx"` with `"args": ["--from", "git+https://github.com/davidbejar/boe-insights-mcp", "boe-insights-mcp"]`.

## Configuration

Optional environment variables; unset or empty variables keep their defaults, and invalid values stop the server at startup with a message naming the variable.

| Variable | Default | Meaning |
|---|---|---|
| `BOE_INSIGHTS_CACHE` | `disk` | Cache backend: `disk` (persistent) or `memory` (per process). |
| `BOE_INSIGHTS_CACHE_DIR` | platform cache directory | Where the disk cache lives. |
| `BOE_INSIGHTS_MIN_INTERVAL` | `1.0` | Minimum seconds between requests to boe.es (number >= 0). |
| `BOE_INSIGHTS_MAX_CONCURRENCY` | `2` | Maximum simultaneous requests to boe.es (integer >= 1). |

## How it works

```text
MCP tools            server.py        thin: parameters, descriptions, error translation
   |
use-case functions   legislation.py   plain functions returning typed pydantic models
   |
BOE client           client.py        BOE API calls, parsing, retries
   |
cache + rate limiter                  disk or memory cache; minimum interval and concurrency cap
   |
BOE open data API    https://www.boe.es/datosabiertos/
```

- **Cache-first.** Responses are cached (searches for 30 minutes, law metadata and amendment analysis for 6 hours), so repeated questions do not reach the BOE.
- **Polite by default.** The BOE publishes no rate limit, so the server imposes its own: at least one second between requests and at most two at a time.
- **Typed, structured results.** Every tool returns a pydantic model with a declared output schema, not free text.
- **Actionable errors.** Failures say what happened, whether retrying makes sense and what to do next (for example, "use `resolve_reference` to find the correct identifier").
- **Provenance everywhere.** Each result states its source, the required attribution, when the result was assembled (`generated_at`; the data itself may come from the cache), the API's last-update stamp and the informational-only notice.

## Data source, attribution and legal notice

The data comes from the BOE open data API: <https://www.boe.es/datosabiertos/>.

Basado en datos de la Agencia Estatal Boletín Oficial del Estado ([www.boe.es](https://www.boe.es)).

- Consolidated texts are informational only and have no official legal value. Only the texts published in the official gazette are authentic.
- This project is independent. It is not affiliated with, sponsored by or endorsed by the Agencia Estatal Boletín Oficial del Estado.
- Nothing here is legal advice.

## Roadmap

- [x] 1. Thin slice: client, cache, rate limiter and three tools
- [ ] 2. Article reading and version history: read an article as of a date, article diff, change timeline, amendment history
- [ ] 3. Trend analytics: legislation over time, top departments, compare topics, explain a spike, most-amended laws
- [ ] 4. Citation verification, amendment chain explorer, gazette scanning
- [ ] 5. MCP resources, prompts and stored watchlists
- [ ] 6. Remote mode: Streamable HTTP, GitHub OAuth, Podman container
- [ ] 7. Evaluation suite, documentation site, PyPI release

## Development

```bash
uv sync --all-groups                         # install, including dev tools
uv run pytest                                # tests; no network
uv run pytest -m live                        # tests against the real BOE API
uv run ruff check . && uv run ruff format .  # lint and format
uv run pyright                               # type check
```

Live tests hit the real BOE API; run them sparingly. Logic is written tests first, and unit tests never touch the network: they use [respx](https://lundberg.github.io/respx/) with real API responses recorded in `tests/fixtures/boe/`. Verified API behaviour is documented in [`docs/api-notes.md`](docs/api-notes.md).

## License

MIT. See [LICENSE](LICENSE).
