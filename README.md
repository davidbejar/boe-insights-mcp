# boe-insights-mcp

**English** | [Español](README.es.md)

[![CI](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that gives AI assistants such as Claude analytical tools over the open data of Spain's BOE (Boletín Oficial del Estado). Instead of one tool per API endpoint, the tools are shaped around the questions people actually ask: "is this law still in force?", "what changed in this article?", "how has legislation on X evolved?".

**Status:** early development. Eight tools are available today (law lookup, search, status overview and code lookup; amendment history; and reading, comparing and tracing the history of single articles); everything else is on the [roadmap](#roadmap). Not yet published on PyPI: run it from source.

## What you can ask

| Prompt | Tool |
|---|---|
| "What is the BOE identifier of Real Decreto-ley 6/2019?" | `resolve_reference` |
| "Is Ley 40/2015 still in force, and how much has it been amended?" | `resolve_reference`, then `get_law_overview` |
| "Was Ley 30/1992 repealed, and on what date?" | `resolve_reference`, then `get_law_overview` |
| "Which organic laws about data protection are in the consolidated legislation?" | `search_legislation` |
| "Which department code does the Agencia Española de Protección de Datos have?" | `find_codes`, then `search_legislation` |
| "What does article 46 bis of Ley 40/2015 say?" | `resolve_reference`, then `read_article` |
| "What did article 48 of Ley 40/2015 say on 1 January 2017?" | `read_article` with `as_of` |
| "What changed in article 48 of Ley 40/2015 between 2016 and today?" | `compare_article_versions` |
| "When was article 48 of Ley 40/2015 last changed, and by which law?" | `get_article_history` |
| "Which laws have amended Ley 40/2015, and in which years?" | `get_amendment_history` |

## Tools

### Finding laws

| Tool | Question it answers | Key parameters |
|---|---|---|
| `resolve_reference` | What is the BOE identifier of "Ley 40/2015"? | `reference` (a name such as `Ley Orgánica 3/2018`, `RDL 6/2019`, an identifier, or a title fragment) |
| `search_legislation` | Which laws are about X, or were published recently? | `title`, `text`, `rank`, `department_code`, `scope`, `subject_code`, `published_from`, `published_to`, `sort_by`, `descending`, `limit` (1-50), `offset` |
| `get_law_overview` | Is this law in force, and how much has it changed? | `law_id` (e.g. `BOE-A-2015-10566`) |
| `find_codes` | What is the code of this department, subject or rank (for the `search_legislation` filters)? | `kind` (`department`, `subject` or `rank`), `text`, `limit` (1-50) |

### Reading and tracking changes

| Tool | Question it answers | Key parameters |
|---|---|---|
| `read_article` | What does this article say, today or on a past date? | `law_id`, `article`, `as_of`, `max_chars` (500-20000), `offset` |
| `compare_article_versions` | What changed in this article between two dates? | `law_id`, `article`, `from_date`, `to_date`, `max_changes` (1-200) |
| `get_article_history` | When did this article change, and which law changed it? | `law_id`, `article` |
| `get_amendment_history` | Which norms have affected this law, by kind and by year? | `law_id`, `group` (`all`, `repeals`, `modifies`, `adds`, `corrections` or `other`), `limit` (1-100), `offset` |

All eight tools are read-only. `resolve_reference` returns `resolved`, `ambiguous` (with candidates and a hint) or `not_found`. `search_legislation` pages with `next_offset`, because the API gives no total count. `find_codes` returns the matching `{code, text}` pairs.

**Law status.** `get_law_overview` returns a `status` (`in_force`, `partially_repealed`, `repealed`, `annulled`, `validity_exhausted` or `not_yet_in_force`) with a plain-English note, the BOE's initial note when the text has one, and counts and samples of the norms that amend the law or that it amends. The status is derived from the BOE flags and the amendment references, so it is an indication, not a legal determination. A whole repeal is also detected from the opening note of the consolidated text ("Norma derogada..."), because some wholly repealed laws carry only that note and not the metadata flag.

**Article states.** `read_article` reports the article's `state` on the requested date and `get_article_history` its `current_state`: `in_force`, `repealed`, `not_yet_in_force` (text published but not yet effective) or `did_not_exist` (the date precedes the article's first version).

**Naming an article.** The `article` argument accepts the wording people use: `46 bis`, `art. 12`, `artículo 48`, `disposición adicional tercera` (or `DA 3`), `anexo I`, `preámbulo`, or a `block_id` taken from `candidates`. If the wording matches several blocks (`ambiguous`) or none (`not_found`), the result is not an error: `outcome` says so and `candidates` lists blocks to retry with, using their `block_id`.

Every result carries a `provenance` block. Trimmed output of `compare_article_versions` for article 48 of `BOE-A-2015-10566` between 2016-10-02 and 2026-11-21 (recorded test fixtures, no live request). Besides the fields shown, the real result also has the old and new text of each change, a `unified_diff` and the truncation flags; `[-old-] {+new+}` marks a rewording inside a paragraph:

```json
{
  "law_id": "BOE-A-2015-10566",
  "outcome": "found",
  "block_id": "a48",
  "block_title": "Artículo 48",
  "from_date": "2016-10-02",
  "to_date": "2026-11-21",
  "from_version": {
    "index": 1,
    "starts_on": "2016-10-02",
    "effective_from": "2016-10-02",
    "published_on": "2015-10-02",
    "introduced_by": "BOE-A-2015-10566"
  },
  "to_version": {
    "index": 2,
    "starts_on": "2021-01-01",
    "effective_from": "2021-01-01",
    "published_on": "2020-12-31",
    "introduced_by": "BOE-A-2020-17340"
  },
  "identical": false,
  "added": 0,
  "removed": 0,
  "changed": 1,
  "unchanged": 11,
  "changes": [
    {
      "op": "changed",
      "inline": "... resultarán eficaces una vez [-inscritos-] {+inscritos, en el plazo de 5 días hábiles desde su formalización,+} en el Registro Electrónico estatal de Órganos e Instrumentos de Cooperación ..."
    }
  ],
  "amending_laws": ["BOE-A-2020-17340"],
  "provenance": {
    "source": "Agencia Estatal Boletín Oficial del Estado",
    "attribution": "Basado en datos de la Agencia Estatal Boletín Oficial del Estado",
    "source_url": "https://www.boe.es",
    "generated_at": "2026-11-21T12:09:47.258224Z",
    "data_updated_at": "2020-12-31T00:00:00Z",
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
use-case functions   legislation.py   laws, search, amendments: plain functions returning typed pydantic models
                     articles.py      article text, comparison, history, built on locator.py and versions.py
   |
BOE client           client.py        BOE API calls, retries; textparse.py parses the text XML
   |
cache + rate limiter                  disk or memory cache; minimum interval and concurrency cap
   |
BOE open data API    https://www.boe.es/datosabiertos/
```

The article tools add three pieces: an XML parser for the BOE's text blocks (`textparse.py`), an article locator (`locator.py`) and the version logic (`versions.py`: selection, repeal detection, diff and history). The last two are pure functions with no I/O.

- **Cache-first.** Responses are cached (searches for 30 minutes; law metadata, amendment analysis and the text index for 6 hours; article text for 6 hours, or 30 days when the index's update date is known), so repeated questions do not reach the BOE.
- **Articles are found by title.** The BOE's block ids are opaque (article 46 bis of Ley 40/2015 is `a4-2`), so the server reads the law's text index and matches the wording against the block titles instead of building an id.
- **Versions are selected by start date.** Each version of an article starts on its effective date (the publication date when that is missing), and the text in force on a date is the latest version that started on or before it.
- **Long articles come in pages.** `read_article` returns at most `max_chars` characters, with `truncated` and `next_offset` to fetch the rest; `compare_article_versions` and `get_article_history` cap their output and say so with `changes_truncated`, `unified_truncated` and `truncated`.
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

- [x] 1. Thin slice: client, cache, rate limiter and four tools
- [x] 2. Article reading and version history: read an article as of a date, article diff, change timeline, amendment history
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
