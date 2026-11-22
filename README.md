# boe-insights-mcp

**English** | [Español](README.es.md)

[![CI](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that gives AI assistants such as Claude analytical tools over the open data of Spain's BOE (Boletín Oficial del Estado). Instead of one tool per API endpoint, the tools are shaped around the questions people actually ask: "is this law still in force?", "what changed in this article?", "how has legislation on X evolved?".

**Status:** early development. Twenty-three tools are available today (law lookup, search, status overview and code lookup; amendment history and amendment chains; reading, comparing and tracing the history of single articles; checking citations against the text; a law's table of contents; trend analytics over the whole consolidated collection; the BOE's daily gazette; and saved searches), plus five [resources](#resources-and-prompts) and eight prompts (four, each in English and Spanish); everything else is on the [roadmap](#roadmap). Not yet published on PyPI: run it from source.

## What you can ask

Nine worked examples, each from a real run of the server (examples 1 to 4 on 2026-11-21, examples 5 to 9 on 2026-11-22). The question comes first, then the tool calls in order and an extract of the result (long titles shortened with "…").

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
| Real decretos-leyes in the collection (`count`) | 19 | 12 | **38** | 28 | 20 | 8 |
| Highest official number seen (`highest_number`) | 28 | 18 | **39** | 32 | 20 | 8 |

- 2010-2025 has 230 of them; 2020 is the strongest spike, 38 against a baseline of 11.0 (ratio 3.45).
- The collection does not hold every law of a year, which is why this result also carries `highest_number`: the highest official number seen that year, a lower bound of how many were enacted. 2020 holds 38 of at least 39. The neighbouring years are thinner (2019: 12 of at least 18; 2018: 19 of 28; 2021: 28 of 32), while 2022 and 2023 look complete (20 of 20, 8 of 8).
- The conclusion holds given that coverage: 2020 has the most decree-laws of any year in 2010-2025, both by the laws held (38) and by the highest number seen (39; the next is 32, in 2021). Recent years are thin (2025 holds 4 of at least 16), so do not read the fall since 2023 as a trend.
- `explain_spike` for 2020 puts the baseline at 10.2 (the 5 preceding years). Its breakdown (`contributions`) is empty here: all 38 laws are state laws of Jefatura del Estado, and a dimension with a single value is not reported.
- `laws` is an evenly spaced sample of the 38 in date order (`max_laws=8`, first and last included): Real Decreto-ley 1/2020 (pensions), 7/2020 (impact of COVID-19 on the economy), 13/2020 (agrarian employment), 18/2020 (social measures in defence of employment), 23/2020 (energy and economic reactivation), 28/2020 (remote work), 34/2020 (business solvency, energy and taxation) and 36/2020 (modernisation of the public administration and the Recovery Plan).
- The tool lists laws and counts; the link between the spike and the pandemic is something you read from the titles.

### 4. Most-amended laws

*"Which data protection laws have been amended most?"*

```text
most_amended_laws(title="protección de datos", from_date="2007-01-01", limit=5, max_laws=10)   -> complete false
most_amended_laws(title="protección de datos", from_date="2007-01-01", limit=5, max_laws=10)   -> complete true
```

Each call has a 40-second budget (counted from the start of the call, so the first one includes downloading the catalogue) and reads the amendment data of at most `max_laws` laws, one request each. This tool has no default lower bound on dates: here `from_date="2007-01-01"` was added to the calls above, which leaves 14 matching laws (without it, 28 match). Run on an empty cache, the first call examined 9 (`complete` false, with a note to call again) and the second continued from the cache and examined all 14 (`complete` true). Result of the second call, laws published since 2007:

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

About 29% of the collection (3,623 of 12,477 laws when this was run) is regional. For "vivienda", 48 laws since 2015 across 12 communities (the title filter also matches the plural "viviendas"):

| Community | Laws |
|---|---|
| Illes Balears | 8 |
| Canarias, Cataluña | 7 each |
| Región de Murcia | 6 |
| Andalucía, Extremadura, Navarra, Madrid, Comunitat Valenciana | 3 each |
| Aragón | 2 |

The 2 remaining communities (3 laws) are grouped as "other". (The `search_legislation` step is only there to find an identifier among the Balearic results; it returns 7 laws, including Ley 5/2018, "de la vivienda de las Illes Balears". Its `title` filter is matched by the BOE itself on exact words, so it misses the eighth Balearic law, whose title says "viviendas".) `get_law_overview` for Ley 5/2018 shows two dates:

- `first_published`: gazette BOIB, issue 78, 2018-06-26. This is where the law was first published, and it governs entry into force (`effective_from` 2018-06-27).
- `published_on`: 2018-07-13, the date of its reprint in the BOE. `enacted_on` is 2018-06-19.
- `status` `partially_repealed`; 20 later norms affect it (10 modifications, 3 repeals, among others).

### 6. Checking citations

*"Check these citations from a memo."*

```text
read_article(law_id="BOE-A-2015-10566", article="artículo 48")                      -> text in force
read_article(law_id="BOE-A-2015-10566", article="artículo 48", as_of="2017-01-01")  -> the 2016 wording
verify_citation(citations=[five items: law, article, and a quote for four of them])
```

All five citations were checked in one call (`checked` 5, `not_checked` 0, run on 2026-11-22):

| Citation | `status` | What the result says |
|---|---|---|
| Ley 40/2015, art. 48, the current wording of the term for registration ("resultarán eficaces una vez inscritos, en el plazo de 5 días hábiles desde su formalización, en el Registro Electrónico …") | `matches` | `quote_match.level` `exact`. |
| The same sentence as worded from 2016 ("… una vez inscritos en el Registro Electrónico …", without the 5 days) | `matches_older_version` | `other_version`: an `exact` match with a version in force from 2016-10-02 to 2020-12-31. |
| The current wording with "5 días" changed to "15 días" | `text_differs` | `level` `close`, `similarity` 0.97, `flags` `["number"]`, `differences`: "… en el plazo de [-15-] {+5+} días hábiles …". |
| "Las aportaciones financieras que se comprometan a realizar los firmantes (...) podrán ser superiores a los gastos derivados de la ejecución del convenio." | `matches` | `level` `exact`, but `omitted` is `["no"]` and `flags` is `["omitted_negation"]`. |
| Ley 30/1992, art. 1, no quote | `not_in_force` | `state` `repealed`; note: "The whole law was repealed on 2016-10-02, so this article is no longer in force; the text shown is its last consolidated version." |

The fourth row is why `omitted` exists. Every word of the quote is in the article, so it matches, but the quote skips a "no": article 48.6 says the financial contributions "no podrán ser superiores a los gastos", and the quote reads as if they may be. The tool compares words, not meaning, and reports facts, not legal correctness.

### 7. Following amendments

*"What did Real Decreto-ley 8/2020 change, and has it been changed since?"*

```text
explore_amendment_chain(law_id="BOE-A-2020-3824", direction="amends", depth=1)
explore_amendment_chain(law_id="BOE-A-2020-3824", direction="amended_by", depth=2)
```

- `amends`: the decree-law acts on 9 norms (`levels` [1, 9]): 6 modifications, 2 suspensions and 1 repeal. Edges, with the BOE's own wording in `detail`: `modifies` Real Decreto-ley 7/2020, "el art. 16 del Real Decreto-ley 7/2020, de 12 de marzo"; `suspends` `BOE-A-2015-2495`, "en la forma y con los efectos indicados la vigencia de los arts. 3.5 y 6 de la Orden IET/389/2015, de 5 de marzo".
- `amended_by`, depth 2: 72 laws (`levels` [1, 23, 48]) and 142 edges, 117 `modifies` and 25 `repeals` (repeals include partial ones), all 24 laws examined (`complete` true). Two edges that touch the starting law: `repeals`, Ley 2/2021, "el art. 42, SE MODIFICA el art. 40.1 y 2, Ley 2/2021, de 29 de marzo"; `modifies`, `BOE-A-2023-6967`, "el art. 17.9, por Real Decreto-ley 2/2023, de 16 de marzo".
- `court_rulings_excluded` is 3: three court rulings concerning this decree-law are left out by default because only amendments are followed; `include="all"` lists them.
- Caution: the 48 laws at level 2 amended one of the 23 laws at level 1, not necessarily the parts that touch Real Decreto-ley 8/2020. Their `detail` is not shown by default.

### 8. The daily gazette

*"What did the BOE publish on 14 March 2020?"*

```text
get_daily_summary(date="2020-03-14", limit=5)
```

- Two issues that day: number 67 (`BOE-S-2020-67`) and number 66. 419 items in all: section 1 (general provisions) 5, 2A 4, 2B 7, 3 (other provisions) 41, 4 (administration of justice) 280, 5A 22, 5B 52, 5C 8.
- The first item is in issue 67, the extraordinary one: `BOE-A-2020-3692`, "Real Decreto 463/2020, de 14 de marzo, por el que se declara el estado de alarma para la gestión de la situación de crisis sanitaria ocasionada por el COVID-19."

*"Anything about vivienda in the BOE this week?"*

```text
scan_gazette(words="vivienda")      -> default range: the last 7 days, 2026-11-16 to 2026-11-22
```

- 7 days scanned, 6 with an issue (the 22nd is a Sunday, with none); 11 titles match: 2026-11-16 1, 11-17 2, 11-18 1, 11-19 2, 11-20 4, 11-21 1.
- 2026-11-21, `BOE-A-2026-24487`: "Resolución de 10 de noviembre de 2026, de la Dirección General de Vivienda y Suelo, por la que se publica el Convenio con la Comunidad Autónoma de Castilla-La Mancha, …"
- 2026-11-19, `BOE-B-2026-38005`: "Anuncio del concurso de proyectos de: Secretaría General de Agenda Urbana, Vivienda y Arquitectura. …"
- 2026-11-20, `BOE-B-2026-38218`: "Resolución del Departamento de Territorio, Vivienda y Transición Ecológica TER/4133/2026, por la que se otorga …"

### 9. Following a topic

*"Follow housing in the BOE for me, and tell me what is new."* then *"Also tell me if Ley 40/2015 changes."* (see [How saved searches work](#how-saved-searches-work))

```text
save_search(words="vivienda")                                -> saved as "vivienda"
list_saved_searches()
check_saved_searches(since="2026-11-15", limit=3)
save_search(law_id="BOE-A-2015-10566")                       -> saved as "ley-40-2015"
check_saved_searches(name="ley-40-2015")
```

Run on a Sunday, 2026-11-22, with an empty store. Saving a gazette search reads today's issue once (a Sunday: none yet) and records "checked through yesterday" (`checked_through` 2026-11-21); the note says "The first check reports what is published from now on." So the first check below, right after saving, has nothing new: it read only today (`matched` 0, with the note that today has no issue yet). To look at the past week the next check passes `since` seven days back (2026-11-15). `since` reads again without marking anything as seen, so nothing stored changed (`last_checked_at` stays `null`).

- `list_saved_searches` returns one search: `name` "vivienda", `kind` `gazette`, `words` "vivienda", `checked_through` 2026-11-21, `total` 1 of a `limit` of 20.
- The `since` check read 2026-11-15 to 2026-11-22 (`days_pending` 0; the 22nd had no issue, which the result notes) and found 11 matches (`matched`), the same count as the `scan_gazette` run in example 8. Items come oldest day first: with `limit=3` it shows the first 3 (`has_more` true) and says "Showing 3 of 11; scan_gazette with the same filters lists them all (at most 31 days per call)." The oldest, 2026-11-16, `BOE-B-2026-37684`: "Anuncio de la Demarcación de Costas en Murcia sobre solicitud de prórroga de la concesión administrativa de ocupación de dominio público marítimo-terrestre con destino a vivienda …" Without `since`, a check shows what is new and remembers it, so asking again does not repeat it.
- Saving a law search reads the law once and remembers the amendments it already has, so they do not count as news; the note says "The first check reports changes recorded from now on (the BOE data read can be up to 6 hours old)." The name was proposed from the law's rank and number: `ley-40-2015` (`name_was_proposed` true, `total` 2).
- Its check is the honest "nothing new": `status` `checked`, `changes` empty, `changes_total` 0. The same check with `since="2026-08-25"` does not read the law at all and says "since applies to gazette searches; get_amendment_history lists a law's past amendments."

### Good to know

- **Title filters match whole words, and wording matters.** The trend tools and the gazette tools match singular and plural forms ("vivienda" also finds "viviendas"); the BOE's own search (the `title` and `text` filters of `search_legislation`) matches exact words. In a title, "fumar" finds nothing since 2020, while "tabaco" finds 8 (verified on 2026-11-21). A text or subject filter that matches more than 5,000 laws is refused as too broad.
- **The gazette is wider than the consolidated collection.** `get_daily_summary` and `scan_gazette` list everything the BOE prints, and most of those identifiers (appointments, announcements, many resolutions) are not in the consolidated collection, so the law and article tools will not find them.
- **Citation checks compare words, not meaning.** A paraphrase does not match, and a quote that skips a word with "(...)" matches while `omitted` and `flags` say what was skipped. The result is facts about wording, not a view on legal correctness.
- **Coverage is the BOE's consolidated legislation collection**, not every item printed in the BOE, and it holds only part of each year's laws, a varying part (thinner before about 2010 and for the most recent year or two). A change in a count is not by itself a change in how much was legislated. Counting one rank (Ley with `scope="state"`, Ley Orgánica, Real Decreto-ley or Real Decreto Legislativo) by year, `legislation_over_time` adds `highest_number` so you can judge the coverage. Counts say so in their `caveats`.
- **Regional coverage is laws and decree-laws with the rank of law**, not regional decrees or orders, which live in each community's own gazette.
- **Regional laws are dated by their BOE reprint** (which can come weeks or years after the regional publication) unless you pass `date_basis="enacted"`; under `enacted` the last three months are incomplete, because laws are published weeks after being enacted.
- **Saved searches do not alert anyone.** The server only answers when asked, and it cannot send a message or run on a timer. See [How saved searches work](#how-saved-searches-work) for what a check covers and where the data lives. A few limits: an item is delivered once (`since` re-reads it if a result never reached you); a law reference whose wording the BOE later changes is reported again whole; `section` and `department_code` are not checked against the BOE tables, so a wrong code simply finds nothing; and the same filters can be saved twice.
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
| `explore_amendment_chain` | Which laws did this law amend, and which laws amended it, up to three levels deep? | `law_id`, `direction` (`amended_by` or `amends`), `depth` (1-3), `include` (`amendments` or `all`), `details` (`root`, `all` or `none`), `max_laws` (1-50) |

### Checking citations

| Tool | Question it answers | Key parameters |
|---|---|---|
| `verify_citation` | Do these citations (law, article, quoted words) exist, are they in force, and do the words match the text, the current version or an older one? | `citations` (1 to 10, each with `law`, `article` or `block_id`, optional `quote` of at most 5,000 characters, optional `as_of`) |

### The daily gazette

These two tools cover everything the BOE prints (appointments, grants, announcements, every disposition) since 1960-09-01, unlike the consolidated collection the other tools use.

| Tool | Question it answers | Key parameters |
|---|---|---|
| `get_daily_summary` | What did the BOE publish on this day? | `date` (default: the latest issue), `section`, `department_code`, `words`, `offset`, `limit` (1-100) |
| `scan_gazette` | Which items in the last days have these words in the title? | `words`, `section`, `department_code`, `from_date` (default: 6 days before `to_date`), `to_date` (default: today), `offset`, `limit` (1-100); at most 31 days |

The first eighteen tools are read-only; the five [saved-search tools](#how-saved-searches-work) are the only ones that change anything, and only in the server's own store, never at the BOE. `resolve_reference` returns `resolved`, `ambiguous` (with candidates and a hint) or `not_found`. `search_legislation` pages with `next_offset`, because the API gives no total count. `find_codes` returns the matching `{code, text}` pairs.

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

The trend tools count over a local snapshot of the whole consolidated collection (see [How it works](#how-it-works)); the first one takes about half a minute. `from_date` defaults to 1 January of the year 19 years before `to_date` (`most_amended_laws` has no default lower bound) and `to_date` to today. A result of more than 1,500 points (series times periods) is refused with advice to use a coarser period, a shorter range or fewer groups. `date_basis` is `published` (BOE publication, the default) or `enacted`. `most_amended_laws` reads one law's amendment data per request, so each call examines at most `max_laws` laws: while `complete` is false, call again with the same arguments to continue from the cache.

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

### Saved searches

| Tool | Question it answers | Key parameters |
|---|---|---|
| `save_search` | Keep following this topic or this law. | `name` (optional), `words`, `section`, `department_code` (gazette search) or `law_id` (law search) |
| `list_saved_searches` | Which topics am I following, and when were they last checked? | none |
| `check_saved_searches` | Is there anything new in what I follow? | `name` (default: all), `since` (up to 90 days back), `limit` (1-50, default 20) |
| `edit_saved_search` | Rename a saved search or change its gazette filters. | `name`, `new_name`, `words`, `section`, `department_code` (empty string removes a filter) |
| `delete_saved_search` | Stop following this. | `name` |

## How saved searches work

A saved search is a search kept on your machine plus a bookmark of how far it was last checked. It exists so that you can ask, a week later, "anything new?" without repeating the filters and without seeing the same items twice.

**The server cannot alert anyone.** It runs only while your app is using it and has no way to send a message. Nothing happens until you ask ("anything new?"), and then the assistant calls `check_saved_searches`. A search you never check just waits.

**Two kinds of search.**

| Kind | You give | A check reports |
|---|---|---|
| Gazette | `words` (whole words of the title; singular and plural both match), `section`, `department_code`: at least one | The items of the daily gazette published since the last check that match the filters, oldest day first. |
| Law | `law_id` of a consolidated law (use `resolve_reference` to find it) | New amendments, additions, suspensions, repeals and corrections of that law, with the BOE's own wording. Court rulings and citations that mention the law are only counted (`other_new`: `court_rulings`, `citations`, `other`), not listed; `explore_amendment_chain` with `include="all"` shows the rulings. |

Saving a gazette search reads today's issue once and remembers what it already holds, so the first check reports what is published from now on (an item added to today's issue later counts as new). Saving a law search reads the law once and remembers the amendments it already has, so only later ones count as news. A law search cannot change its law: to follow another, save a new search.

**Names.** Give one (`name`) or let the server propose one from the filters, such as `vivienda`, `seccion-1` or `ley-40-2015`. A name is stored as lowercase letters, digits and hyphens, at most 60 characters, so "Ley 40/2015" becomes `ley-40-2015`; a name already taken gets `-2`, `-3` and so on. `edit_saved_search` renames a search and, for a gazette search, changes its filters while keeping the bookmark.

**What a check covers.** A gazette check reads the days from the last check to today, at most the last 31 days; when more days were missed it says how many were left out. It shows what is new, oldest day first, and remembers what it showed: asking again never repeats an item, and an item added to today's issue after a check is shown by the next one. Today's issue, and a recent weekday that answered "no issue" (the BOE can publish late), are read again by the next check. A check shows at most `limit` items per search and 60 in all per call, serving your searches in list order; when more is waiting, `complete` is false and the next call brings the rest. A law search reports the new references the same way. Pass `since` to read gazette searches again from an earlier date, up to 90 days back: nothing is marked as seen, so use it to look back, to try a search saved today, or to recover a result that never reached you (delivery is at most once: an item shown to the assistant is not shown again, even if the app lost the reply). `since` does not apply to law searches; `get_amendment_history` lists a law's past amendments. A check has a time budget of about 40 seconds: when `complete` is false, ask again and it continues from where it stopped. Most Sundays have no issue, and a search that could not be read at all (a law the BOE no longer has) says so in its own note without keeping the call open.

**Limit.** 20 saved searches. Saving a 21st fails with a message telling you to delete one first.

**Where the data lives.** In a small SQLite file, `saved_searches.sqlite3`, in your user data directory (on macOS `~/Library/Application Support/boe-insights-mcp/`, on Linux `~/.local/share/boe-insights-mcp/`). The file is private to your user, stays on your machine and is never sent anywhere; a check sends only the BOE requests it needs. To move your searches, copy the file to the same place on the other machine, or set `BOE_INSIGHTS_DATA_DIR` ([Configuration](#configuration)). To delete everything, delete the file. It is separate from the response cache, which is safe to delete at any time: clearing the cache never loses a saved search. With `BOE_INSIGHTS_CACHE=memory` and no `BOE_INSIGHTS_DATA_DIR`, searches are kept in memory only and are lost when the server stops.

**These are the only tools that change anything**, and only in that file. `edit_saved_search` and `delete_saved_search` are marked as destructive, so some apps ask you to confirm before running them; a deleted search cannot be recovered. `check_saved_searches` also changes something (it records what it showed), unless you pass `since`.

Everyday phrases and what happens:

| You say | What happens |
|---|---|
| "Follow housing grants in the BOE" | `save_search` with `words`, then the name it chose is reported back. |
| "Which topics are we following?" | `list_saved_searches`. |
| "Anything new?" | `check_saved_searches` for all of them. |
| "Anything new on housing?" | `check_saved_searches` with that `name`. |
| "What did the housing search find in the last month?" | `check_saved_searches` with `since`, without marking anything as seen. |
| "Also tell me if Ley 40/2015 changes" | `resolve_reference`, then `save_search` with its `law_id`. |
| "Rename the housing search to grants" | `edit_saved_search` with `new_name`. |
| "Only section 3 from now on" | `edit_saved_search` with `section`. |
| "Stop following housing" | `delete_saved_search`. |

## Resources and prompts

Both are optional extras that mirror the tools: they offer nothing the tools cannot do. Support depends on the app, and many apps let the person pick a resource or a prompt but do not let the model open one itself.

**Resources** are things a person attaches to the conversation, each returning exactly what the matching tool returns (as JSON, with the attribution and the informational notice).

| URI | What it returns | Same as |
|---|---|---|
| `boe://law/{law_id}` | Overview of a law: status, dates, department, amendment counts. | `get_law_overview` |
| `boe://law/{law_id}/article/{block_id}` | Current text of one article, by its `block_id`. | `read_article` |
| `boe://gazette/{day}` | What the BOE published on one day (`YYYY-MM-DD`), with up to 100 items. | `get_daily_summary` |
| `boe://codes/{kind}` | The whole table of departments (`department`) or ranks (`rank`). The subject table is too large; use `find_codes`. | `find_codes` |
| `boe://saved-searches` | Your saved searches and when each was last checked. | `list_saved_searches` |

In Claude Code you attach a resource by typing `@` and choosing it, for example `@boe-insights:boe://law/BOE-A-2015-10566`.

**Prompts** are ready-made requests that tell the assistant which tools to call and how to present the result. Each exists in English and in Spanish, with the same steps; the arguments are named in the language of the prompt.

| English | Español | Arguments | What it asks for |
|---|---|---|---|
| `explain_law` | `explicar_ley` | `law` / `ley` | What a law regulates, its status, its structure and how often it was amended. |
| `article_changes` | `cambios_articulo` | `law`, `article`, `since` (optional) / `ley`, `articulo`, `desde` (optional) | How one article changed and which norms changed it. |
| `check_citations` | `comprobar_citas` | `text` / `texto` | Check the citations of laws in a text against the consolidated texts. |
| `gazette_briefing` | `resumen_boe` | `topic`, `days` (1-31, default 7) / `tema`, `dias` | What the BOE published on a topic in the last days. |

In Claude Code, prompts appear as slash commands, for example `/mcp__boe-insights__explain_law`.

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
| `BOE_INSIGHTS_DATA_DIR` | platform user data directory | Where saved searches live (`saved_searches.sqlite3`). Separate from the cache on purpose: the cache can be deleted, your saved searches should not be. |
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
- **Citation matching compares words and versions.** A quote and each version of the article are reduced to comparable words (case, punctuation and quotation marks ignored, accents kept); the best aligned passage is graded `exact`, `close`, `partial` or `none`, and the other versions of the article are compared too, which is how an older wording is recognised.
- **Daily summaries are read as XML, and past days are cached.** The BOE's summary of a day is fetched as XML; a past day never changes, so it is kept for 30 days (today's summary for an hour).
- **Polite by default.** The BOE publishes no rate limit, so the server imposes its own: at least one second between requests and at most two at a time.
- **Typed, structured results.** Every tool returns a pydantic model with a declared output schema, not free text.
- **Actionable errors.** Failures say what happened, whether retrying makes sense and what to do next (for example, "use `resolve_reference` to find the correct identifier").
- **Provenance everywhere.** Each result states its source, the required attribution, when the result was assembled (`generated_at`; the data itself may come from the cache), the API's last-update stamp and the informational-only notice.

## Data source, attribution and legal notice

The data comes from the BOE open data API: <https://www.boe.es/datosabiertos/>.

Basado en datos de la Agencia Estatal Boletín Oficial del Estado ([www.boe.es](https://www.boe.es)).

- Consolidated texts are informational only and have no official legal value. Only the texts published in the official gazette are authentic. Gazette summaries are a listing, and only the texts published in the BOE itself are authentic.
- This project is independent. It is not affiliated with, sponsored by or endorsed by the Agencia Estatal Boletín Oficial del Estado.
- Nothing here is legal advice.

## Roadmap

- [x] 1. Thin slice: client, cache, rate limiter and four tools
- [x] 2. Article reading and version history: read an article as of a date, article diff, change timeline, amendment history
- [x] 3. Trend analytics: legislation over time, top departments, compare topics, explain a spike, most-amended laws, plus a law's table of contents
- [x] 4. Citation verification, amendment chain explorer, gazette scanning
- [x] 5. MCP resources, prompts and saved searches
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
