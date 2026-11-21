# boe-insights-mcp

**English** | [Español](README.es.md)

[![CI](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that gives AI assistants such as Claude analytical tools over the open data of Spain's BOE (Boletín Oficial del Estado). Instead of one tool per API endpoint, the tools are shaped around the questions people actually ask: "is this law still in force?", "what changed in this article?", "how has legislation on X evolved?".

**Status:** early development. Fourteen tools are available today (law lookup, search, status overview and code lookup; amendment history; reading, comparing and tracing the history of single articles; a law's table of contents; and trend analytics over the whole consolidated collection); everything else is on the [roadmap](#roadmap). Not yet published on PyPI: run it from source.

## What you can ask

Five worked examples, each from a real run of the server on 2026-11-21. The question comes first, then the tool calls in order and an extract of the result (long titles shortened with "…").

### 1. Read a well-known law

*"Show me the structure of the data protection law (Ley Orgánica 3/2018) and read the article on the right to erasure."*

```text
resolve_reference(reference="Ley Orgánica 3/2018")          -> BOE-A-2018-16673
get_law_structure(law_id, detail="headings", limit=12)      -> chapters and titles only
get_law_structure(law_id, limit=200)                        -> adds the article headings
read_article(law_id, article="artículo 15")
```

- The headings view lists 24 entries (preamble, 10 titles, 8 chapters, 5 sections), for example "TÍTULO III. Derechos de las personas". The full table of contents has 168 entries, among them 98 articles and 46 provisions.
- Article headings include "Artículo 15. Derecho de supresión" and "Artículo 93. Derecho al olvido en búsquedas de Internet".
- `read_article` returns article 15 (state `in_force`, in force since 2018-12-07): "1. El derecho de supresión se ejercerá de acuerdo con lo establecido en el artículo 17 del Reglamento (UE) 2016/679. …"
- Article 93 (`read_article` with `article="artículo 93"`) begins: "Toda persona tiene derecho a que los motores de búsqueda en Internet eliminen de las listas de resultados …"

### 2. What changed in an article

*"What changed in article 48 of Ley 40/2015 since 2017?"*

```text
resolve_reference(reference="Ley 40/2015")                  -> BOE-A-2015-10566
compare_article_versions(law_id, article="artículo 48", from_date="2017-01-01")
```

- The text in force on 2017-01-01 is the version that started on 2016-10-02; the latest one started on 2021-01-01, introduced by `BOE-A-2020-17340`. There is nothing in between (`intermediate_versions` 0).
- One paragraph changed (`changed` 1, `unchanged` 11, `added` 0, `removed` 0).
- Inline diff of the paragraph on convenios: "resultarán eficaces una vez [-inscritos-] {+inscritos, en el plazo de 5 días hábiles desde su formalización,+} en el Registro Electrónico estatal …" and, further on, "{+en el plazo de 10 días hábiles desde su formalización+} en el «Boletín Oficial del Estado»".

### 3. A trend and its explanation

*"Why were there so many decree-laws in 2020?"*

```text
legislation_over_time(rank="real_decreto_ley", period="year", from_date="2010-01-01", to_date="2025-12-31")
explain_spike(rank="real_decreto_ley", period_key="2020", from_date="2010-01-01", to_date="2025-12-31", max_laws=8)
```

| Year | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 |
|---|---|---|---|---|---|---|
| Real decretos-leyes | 19 | 12 | **38** | 28 | 20 | 8 |

- 2010-2025 has 230 of them; 2020 is the strongest spike, 38 against a baseline of 11.0 (ratio 3.45).
- `explain_spike` for 2020 puts the baseline at 10.2 (the 5 preceding years) and attributes the whole excess to Jefatura del Estado (state scope): 38 laws against 10.2 expected.
- Its list of laws opens with Real Decreto-ley 1/2020 (pensions) and 2/2020 (public-sector pay), then: "Real Decreto-ley 6/2020, de 10 de marzo, … medidas urgentes en el ámbito económico y para la protección de la salud pública", "Real Decreto-ley 7/2020, de 12 de marzo, … para responder al impacto económico del COVID-19" and "Real Decreto-ley 8/2020, de 17 de marzo, de medidas urgentes extraordinarias para hacer frente al impacto económico y social del COVID-19".
- The tool lists laws and counts; the link between the spike and the pandemic is something you read from the titles.

### 4. Most-amended laws

*"Which data protection laws have been amended most?"*

```text
most_amended_laws(title="protección de datos", limit=5, max_laws=10)   -> complete false
most_amended_laws(title="protección de datos", limit=5, max_laws=10)   -> complete true
```

Each call reads the amendment data of at most `max_laws` laws, one request each. Here 14 laws match; the first call examined 10 (`complete` false, with a note to call again) and the second continued from the cache and examined all 14 (`complete` true). Result of the second call, laws published since 2007:

| Amendments | Law |
|---|---|
| 5 | Ley Orgánica 3/2018, de Protección de Datos Personales y garantía de los derechos digitales |
| 3 | Ley 32/2010, de la Autoridad Catalana de Protección de Datos |
| 1 | Real Decreto 1720/2007, … Reglamento de desarrollo de la Ley Orgánica 15/1999 … |
| 1 | Ley 2/2004, de Ficheros de Datos de Carácter Personal de Titularidad Pública y de Creación de la Agencia Vasca de Protección de Datos |
| 1 | Real Decreto-ley 5/2018, … adaptación del Derecho español a la normativa de la Unión Europea en materia de protección de datos |

Repeals count as amendments: Ley 32/2010 has 2 modifications and 1 repeal.

### 5. Comparing the autonomous communities

*"Which autonomous communities have legislated most on housing since 2015?"*

```text
top_departments(scope="regional", title="vivienda", from_date="2015-01-01")
search_legislation(scope="regional", title="vivienda", department_code="8121", published_from="2015-01-01")
get_law_overview(law_id="BOE-A-2018-9774")
```

About 29% of the collection (3,623 of 12,477 laws when this was run) is regional. For "vivienda", 39 laws since 2015 across 12 communities:

| Community | Laws |
|---|---|
| Illes Balears | 7 |
| Región de Murcia | 5 |
| Canarias, Cataluña | 4 each |
| Andalucía, Extremadura, Navarra, Madrid | 3 each |
| Aragón, País Vasco | 2 each |

The 2 remaining communities (3 laws) are grouped as "other". (The `search_legislation` step is only there to find an identifier among the Balearic results; they include Ley 5/2018, "de la vivienda de las Illes Balears".) `get_law_overview` for it shows two dates:

- `first_published`: gazette BOIB, issue 78, 2018-06-26. This is where the law was first published, and it governs entry into force (`effective_from` 2018-06-27).
- `published_on`: 2018-07-13, the date of its reprint in the BOE. `enacted_on` is 2018-06-19.
- `status` `partially_repealed`; 20 later norms affect it (10 modifications, 3 repeals, among others).

"Cambio climático" (9 laws, 8 communities) and "turismo" (12 laws, 6 communities, 5 from the Balearic Islands) were also tried; "vivienda" gave the broadest spread, so it is the one shown.

### Good to know

- **Searches match exact words, so wording matters.** In a title, "fumar" finds nothing since 2020, while "tabaco" finds 8 (verified on 2026-11-21).
- **Coverage is the BOE's consolidated legislation collection**, not every item printed in the BOE. Counts say so in their `caveats`.
- **Regional coverage is laws, not regional decrees or orders**, which live in each community's own gazette.
- **Regional laws are dated by their BOE reprint** (which can come weeks or years after the regional publication) unless you pass `date_basis="enacted"`.
- **Consolidated texts are informational and have no official legal value.** See the [legal notice](#data-source-attribution-and-legal-notice).

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
| `get_law_structure` | What is this law's table of contents, and which article deals with a matter? | `law_id`, `as_of`, `detail` (`headings` or `all`), `offset`, `limit` |
| `read_article` | What does this article say, today or on a past date? | `law_id`, `article` or `block_id`, `as_of`, `max_chars` (500-20000), `offset` |
| `compare_article_versions` | What changed in this article between two dates? | `law_id`, `article` or `block_id`, `from_date`, `to_date`, `max_changes` (1-200), `max_chars` (1000-30000) |
| `get_article_history` | When did this article change, and which law changed it? | `law_id`, `article` or `block_id` |
| `get_amendment_history` | Which norms have affected this law, by kind and by year? | `law_id`, `group` (`all`, `repeals`, `modifies`, `adds`, `corrections` or `other`), `limit` (1-100), `offset` |

All fourteen tools are read-only. `resolve_reference` returns `resolved`, `ambiguous` (with candidates and a hint) or `not_found`. `search_legislation` pages with `next_offset`, because the API gives no total count. `find_codes` returns the matching `{code, text}` pairs.

**Law status.** `get_law_overview` returns a `status` (`in_force`, `partially_repealed`, `repealed`, `annulled`, `validity_exhausted` or `not_yet_in_force`) with a plain-English note, the BOE's initial note when the text has one, and counts and samples of the norms that amend the law or that it amends. The status is derived from the BOE flags and the amendment references, so it is an indication, not a legal determination. A whole repeal is also detected from the opening note of the consolidated text ("Norma derogada..."), because some wholly repealed laws carry only that note and not the metadata flag.

**Article states.** `read_article` reports the article's `state` on the requested date and `get_article_history` its `current_state`: `in_force`, `repealed`, `not_yet_in_force` (text published but not yet effective) or `did_not_exist` (the date precedes the article's first version). An article of a law repealed as a whole carries no repeal marker, so the article tools also check the law (its metadata and, when the text has one, its initial note): from the repeal date on, the state is `repealed` (the last text is still returned), `law_repealed` and `law_repealed_on` say so, and `note` explains it. A repeal dated in the future is only announced, and if that check cannot be done `law_repealed` is `null` and `note` says so.

**Naming an article.** The `article` argument (at most 200 characters) accepts the wording people use: `46 bis`, `art. 12`, `art. 1.902`, `artículo 48`, `disposición adicional tercera` (or `DA 3`), `anexo I`, `preámbulo`. Anything after the designation, such as a paragraph number (`46.2`, `149.1.18ª`) or a context (`14 de la Constitución`), is ignored, because the tools return whole articles; `note` says what was ignored. To pick an exact block, pass its `block_id` from `candidates` instead of `article` (exactly one of the two is required). If the wording matches several blocks (`ambiguous`) or none (`not_found`), the result is not an error: `outcome` says so and `candidates` lists blocks to retry with, using their `block_id`.

### Trends and rankings

| Tool | Question it answers | Key parameters |
|---|---|---|
| `legislation_over_time` | How many laws on X per year, quarter or month? | `title`, `text`, `subject_code`, `rank`, `scope`, `department_code`, `from_date`, `to_date`, `period`, `date_basis`, `group_by` (`none`, `rank`, `department` or `scope`), `top_groups` |
| `top_departments` | Which departments or autonomous communities have issued the most laws on X? | the same filters, plus `limit` |
| `compare_topics` | How do 2 to 5 topics compare over time? | `topics` (each with `name` and `title`, `text` or `subject_code`), `rank`, `scope`, `department_code`, `from_date`, `to_date`, `period`, `date_basis` |
| `explain_spike` | Why does this period have unusually many laws? | the same filters, plus `period_key` (`2020`, `2020-Q2` or `2020-03`), `period`, `baseline_periods`, `max_laws` |
| `most_amended_laws` | Which laws on X have been amended most by later laws? | the same filters, plus `limit`, `max_laws` (laws examined per call) |

The trend tools count over a local snapshot of the whole consolidated collection (see [How it works](#how-it-works)); the first one takes about half a minute. `from_date` defaults to 1 January 19 years ago and `to_date` to today. `date_basis` is `published` (BOE publication, the default) or `enacted`. `most_amended_laws` reads one law's amendment data per request, so each call examines at most `max_laws` laws: while `complete` is false, call again with the same arguments to continue from the cache.

Every result carries a `provenance` block. Trimmed output of `compare_article_versions` for article 48 of `BOE-A-2015-10566` between 2016-10-02 and 2026-11-21 (recorded test fixtures, no live request). Long strings (`inline`, `unified_diff`) are shortened here with `…`. `[-old-] {+new+}` marks a rewording inside a paragraph: a reworded paragraph is returned as `inline` alone (`old_text` and `new_text` are `null`), except tables, which return both texts and no `inline`:

```json
{
  "law_id": "BOE-A-2015-10566",
  "outcome": "found",
  "block_id": "a48",
  "block_title": "Artículo 48",
  "candidates": [],
  "hint": null,
  "provenance": {
    "source": "Agencia Estatal Boletín Oficial del Estado",
    "attribution": "Basado en datos de la Agencia Estatal Boletín Oficial del Estado",
    "source_url": "https://www.boe.es",
    "generated_at": "2026-11-21T12:41:13.262679Z",
    "data_updated_at": "2020-12-31T00:00:00Z",
    "notice": "Consolidated texts are provided for information only and have no official legal value. Only the texts published in the official gazette are authentic."
  },
  "law_repealed": false,
  "law_repealed_on": null,
  "note": null,
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
      "old_text": null,
      "new_text": null,
      "inline": "… resultarán eficaces una vez [-inscritos-] {+inscritos, en el plazo de 5 días hábiles desde su formalización,+} en el Registro Electrónico estatal de Órganos e Instrumentos de Cooperación del sector público estatal, al que se …"
    }
  ],
  "changes_truncated": false,
  "intermediate_versions": 0,
  "amending_laws": [
    "BOE-A-2020-17340"
  ],
  "unified_diff": "--- old\n+++ new\n@@ -8,5 +8,5 @@\n …",
  "unified_truncated": false
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
                     trends.py        counts and rankings over the catalogue snapshot (catalogue.py, analytics.py)
   |
BOE client           client.py        BOE API calls, retries; textparse.py parses the text XML
   |
cache + rate limiter                  disk or memory cache; minimum interval and concurrency cap
   |
BOE open data API    https://www.boe.es/datosabiertos/
```

The article tools add three pieces: an XML parser for the BOE's text blocks (`textparse.py`), an article locator (`locator.py`) and the version logic (`versions.py`: selection, repeal detection, diff and history). The last two are pure functions with no I/O.

- **Trends run on a local snapshot.** The API has no aggregate endpoint, so `catalogue.py` downloads the whole collection (about 12,500 laws) in 3 requests, keeps it for a day and counts locally. `most_amended_laws` is the exception: it needs each law's amendment data, which costs one request per law, hence the `max_laws` budget per call.
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
- [x] 3. Trend analytics: legislation over time, top departments, compare topics, explain a spike, most-amended laws, plus a law's table of contents
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
