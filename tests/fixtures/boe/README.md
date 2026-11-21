# BOE open-data API fixtures

All files are real responses from `https://www.boe.es/datosabiertos/api/`, retrieved 2026-11-21 with
`User-Agent: boe-insights-mcp-dev/0.0 (open-source MCP server; API probe)`.
`Accept: application/json` unless stated otherwise. Base `B` = `https://www.boe.es/datosabiertos/api`.
Trimming only removes repeated array items / dictionary entries / XML sibling elements; no field names, nesting or values were altered.
Files marked "re-serialised" were passed through `json.dumps(indent=4, ensure_ascii=False)` (so `\/` and `\uXXXX` escapes differ from the raw bytes).
Error responses are XML even when `Accept: application/json` was sent (observed).

## Search (`legislacion-consolidada`)
| File | Request | Trim |
|---|---|---|
| `search_page_limit10.json` | `GET B/legislacion-consolidada?limit=10` | none (13.8 KB) |
| `search_titulo_phrase.json` | `GET B/legislacion-consolidada?limit=5&query={"query":{"query_string":{"query":"titulo:\"Ley 40/2015\""}}}` (query URL-encoded) | none; returns 3 hits (shows the phrase match is loose) |
| `search_numero_oficial_ambiguous.json` | `...?limit=20&query={"query":{"query_string":{"query":"numero_oficial:\"3/2018\""}}}` | none; 13 hits, mixed state/regional |
| `search_numero_oficial_rango_single.json` | `...?limit=10&query={"query":{"query_string":{"query":"numero_oficial:\"3/2018\" AND rango@codigo:1290"}}}` | none; exactly 1 hit (BOE-A-2018-16673) |
| `search_empty.json` | `...?limit=5&query={"query":{"query_string":{"query":"titulo:energía AND rango@codigo:1300"},"range":{"fecha_publicacion":{"gte":"20200101","lte":"20201231"}}}}` | none. Empty result is HTTP 200 with `"data": ""` (a string, not a list) |
| `search_offset12000_limit5_tail.json` | `...?offset=12000&limit=5` | none; total dataset was 12 477 records, offset 20000 also gives the empty shape |

## Errors (raw bodies, all `Content-Type: application/xml`)
| File | Request | HTTP |
|---|---|---|
| `error_500_syntax_code101.xml` | `query={"query":{"query_string":{"query":"titulo:energía AND ("}}}` | 500 "Server error - Code: 101" |
| `error_500_unknown_field_code101.xml` | `query={"query":{"query_string":{"query":"foo:bar"}}}` | 500 Code 101 |
| `error_500_nonjson_query_code109.xml` | `query=not json at all` | 500 Code 109 |
| `error_400_unsupported_query_format.xml` | `query={"query":{"match_all":{}}}` | 400 |
| `error_400_sort_field_not_found.xml` | `query={"query":{"query_string":{"query":"titulo:energía"}},"sort":[{"nonexistent":"desc"}]}` | 400 |
| `error_400_unsupported_accept.xml` | `GET B/legislacion-consolidada/id/BOE-A-2015-10566` with `Accept: application/json` (that endpoint is not served as JSON) | 400 "No soportado ningún mime type de la cabecera Accept." |

NOT captured (request budget of 120 was exhausted): a 404 body for an unknown id, and a request with no `Accept` header. See `docs/api-notes.md`, "Unverified".

## Metadata / analysis
| File | Request | Trim |
|---|---|---|
| `metadatos_in_force_ley40_2015.json` | `GET B/legislacion-consolidada/id/BOE-A-2015-10566/metadatos` | none |
| `metadatos_repealed_ley30_1992.json` | `.../id/BOE-A-1992-26318/metadatos` (has `fecha_derogacion`, `estatus_derogacion: S`) | none |
| `metadatos_superseded_partial_lopd1999.json` | `.../id/BOE-A-1999-23750/metadatos` (LO 15/1999, replaced by LO 3/2018 yet flagged `estatus_derogacion: N`) | none |
| `analisis_ley40_2015.json` | `.../id/BOE-A-2015-10566/analisis` | none (20.8 KB) |
| `analisis_constitucion_materias_dict.json` | `.../id/BOE-A-1978-31229/analisis` (`materias` is an object, not a list; no `notas`) | none |

## Text
| File | Request | Trim |
|---|---|---|
| `texto_indice_ley40_2015_trimmed.json` | `.../id/BOE-A-2015-10566/texto/indice` | 267 blocks -> first 30 plus 15 interesting ones (`a4-2` = Artículo 46 bis, `a1-2` = 108 bis, `a48`, `da`, `df*`...) in original order |
| `texto_indice_rd1006_2026_full.json` | `.../id/BOE-A-2026-24302/texto/indice` | none |
| `texto_bloque_a48_two_versions.xml` | `Accept: application/xml`, `.../id/BOE-A-2015-10566/texto/bloque/a48` | none |
| `texto_bloque_a4-2_articulo46bis.xml` | `.../id/BOE-A-2015-10566/texto/bloque/a4-2` | none; shows block id != article number |
| `texto_bloque_future_fecha_vigencia.xml` | `.../id/BOE-A-2026-24302/texto/bloque/a1` (version `fecha_vigencia="20270701"`, i.e. after retrieval date) | none |
| `texto_bloque_a1_repealed_law_no_marker.xml` | `.../id/BOE-A-1992-26318/texto/bloque/a1` (Ley 30/1992, repealed law: block shows no repeal marker) | none |
| `texto_bloque_a68_irpf_many_versions_trimmed.xml` | `.../id/BOE-A-2006-20764/texto/bloque/a68` (267 KB raw, 13 versions) | kept version 1 and version 13 (the latter carries the odd `fpub=""` attribute) |
| `texto_full_lopd1999_trimmed.xml` | `.../id/BOE-A-1999-23750/texto` (133 KB raw, 75 blocks) | kept 5 first blocks (`nota_inicial`, `preambulo`, `encabezado`, `precepto`...), the `a40` block and the `firma` block |
| `texto_bloque_a40_repealed_article_DERIVED.xml` | **Not a separate request.** The `a40` block cut out of the `/texto` response above and de-indented by two spaces to mimic `/texto/bloque/a40` (`<data><bloque>` instead of `<data><texto><bloque>`). Shows a `(Derogado)` version. | derived, treat the wrapper as inferred |

## Auxiliary tables (`datos-auxiliares/...`)
| File | Request | Trim |
|---|---|---|
| `aux_materias_trimmed.json` | `B/datos-auxiliares/materias` (6 822 entries, 337 KB) | first 20 entries |
| `aux_departamentos_trimmed.json` | `.../departamentos` (211 entries) | first 20 |
| `aux_ambitos.json`, `aux_estados_consolidacion.json`, `aux_rangos.json` | `.../ambitos`, `.../estados-consolidacion`, `.../rangos` | none (2, 2, 19 entries) |
| `aux_relaciones_anteriores.json`, `aux_relaciones_posteriores.json` | `.../relaciones-anteriores`, `.../relaciones-posteriores` | none (50 entries each) |

## Daily summaries
| File | Request | Trim |
|---|---|---|
| `sumario_boe_20261120_trimmed.json` | `GET B/boe/sumario/20261120` (407 KB raw) | per level (`departamento`, `epigrafe`, `item` lists) first 2 kept, all 7 `seccion` kept. Contains the single-element-collapsed-to-object cases (`epigrafe.item` as object, a `departamento` with only `texto`) |
| `sumario_borme_20261120_trimmed.json` | `GET B/borme/sumario/20261120` (80 KB raw) | `item` lists cut to 3; section B (single item as object) and C (no items) kept as is |

## Added after the probe (2026-11-21, untrimmed)

| File | Request | Result |
|---|---|---|
| `error_404_unknown_id.xml` | `GET legislacion-consolidada/id/BOE-A-1900-99999/metadatos`, `Accept: application/json` | HTTP 404, XML body despite JSON `Accept` |
| `error_400_missing_accept.xml` | `GET legislacion-consolidada?limit=1` with no `Accept` header | HTTP 400, XML body |
| `error_404_sumario_sunday_20261115.xml` | `GET boe/sumario/20261115` (a Sunday), `Accept: application/json` | HTTP 404, same body as an unknown id |
