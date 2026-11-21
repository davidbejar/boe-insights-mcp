# BOE open-data API: verified behaviour notes

Probed on 2026-11-21 with 120 requests in total (>= 1.2 s apart, `User-Agent: boe-insights-mcp-dev/0.0 (open-source MCP server; API probe)`). No 429 and no throttling were seen; the only 5xx responses were deliberate malformed queries.
Base: `https://www.boe.es/datosabiertos/api/` (`B` below). Fixtures for every shape are in `tests/fixtures/boe/` (see its README for the exact requests).
"Observed" = seen in a response. "Inferred" = my conclusion from observations. The request budget ran out before some checks; they are listed under "Unverified / open".

## 0. Overview of what the dataset is

* `legislacion-consolidada` is not only BOE state law. Across the whole set (12 477 records at probe time) the first 10 000 by default order were: 9 913 `BOE-*` ids and the rest regional gazettes (`BOJA`, `DOGC`, `DOGV`, `BOA`, `BORM`, `BOC`, `BOIB`, `BON`, `BOCL`...). `ambito` is `1` Estatal (7 053 of those 10 000) or `2` Autonómico (2 947). Regional laws that were republished in the BOE have a `BOE-A-` id but `ambito` 2. (observed)
* JSON responses use the envelope `{"status":{"code":"200","text":"ok"},"data": ...}`. Every number and date is a string. Dates are `yyyymmdd`, except `fecha_actualizacion` = `yyyymmddTHHMMSSZ`.
* XML-to-JSON artefact (observed, important for parsing): a collection with exactly one element is serialised as an object, not a one-element list. Examples: `/analisis` `materias` is a list for most laws but an object for the Constitución; `notas` is an object (`{"nota": "..."}`) for Ley 40/2015 but a list of `{"nota":[...]}` for the Código Civil; `referencias.anteriores` is absent/`null` for the Código Civil; in the daily summaries `departamento`, `epigrafe`, `item` are sometimes objects. An empty result is `"data": ""` (a string). Client code must normalise "absent / '' / object / list" into a list.

## 1. Search paging (`GET B/legislacion-consolidada`)

| Request | Result (observed) |
|---|---|
| no params | HTTP 200, 50 records, 64.6 KB, 1.6 s. Default page size is 50 |
| `limit=10` | 10 records, 13.8 KB, 1.4 s |
| `limit=100` | 100, 127 KB, 1.9 s |
| `limit=1000` | 1000, 1.28 MB, 4.9 s (about 1.28 KB per record) |
| `limit=5000&offset=10000` | 2 477 records (end of data), 3.1 MB, 10.4 s |
| `limit=20000&offset=12400` | accepted, 77 records (not rejected, so the maximum is at least 20000; a full 20000 page was not requested) |
| `limit=-1` | HTTP 200, exactly 10 000 records, 13 MB, 20.3 s. So `-1` means "up to 10 000", not "all" (inferred: capped at 10 000 because `limit=5000&offset=10000` shows more records exist) |
| `offset=50&limit=5` | continues exactly where page one stopped (ids equal `limit=-1` slice 50..54) |
| `offset=10000&limit=5` | works; continues contiguously from record 9 999 (no 10 000 cap on offset) |
| `offset=12000&limit=5` | 5 records. `offset=12400` gives 77, so the total was 12 477 |
| `offset=20000&limit=5` | HTTP 200 with `"data": ""` (empty-string shape, not an error) |

* There is no total-count field: the body has only `status` and `data`, and no `Content-Range`/`X-Total-Count` header (the CORS header lists `Content-Range` as exposed but it is never sent). To count the matches of a query you must page through them, or request a large `limit` and count (about 1.3 KB per hit, no field projection exists).
* Ordering: without `sort` the order is `fecha_actualizacion` descending (observed: the whole 10 000-record list is monotonic non-increasing; a query gives the same order as an explicit `sort fecha_actualizacion desc`). Identical requests returned byte-identical bodies (with sort and without), and paged slices equalled the corresponding slice of a larger page (both unsorted and with `sort fecha_publicacion desc`, 30 records vs offset 10 limit 10). Caveat (inferred): the default order changes whenever a law is updated, so a long crawl by offset can skip or repeat records. Sorting by `fecha_publicacion` is more stable but has many ties (secondary order unknown).
* Valid `sort` fields seen: `fecha_publicacion`, `fecha_actualizacion` (asc/desc). `sort` on `titulo` is a 400 ("can not sort on multivalued field: TITULOS"); an unknown sort field is a 400 "sort param field can't be found".
* Typical latency 0.9 to 1.9 s for pages of up to 100 records. Time grows about linearly: roughly 5 ms per record.

### Search hit shape (see `search_page_limit10.json`)
`fecha_actualizacion`, `identificador`, `ambito{codigo,texto}`, `departamento{codigo,texto}`, `rango{codigo,texto}`, `fecha_disposicion`, `numero_oficial` (missing for some resolutions/orders, e.g. `BOE-A-2026-23892`), `titulo`, `diario`, `fecha_publicacion`, `diario_numero`, `fecha_vigencia`, `vigencia_agotada` (`N`/`S`), `estado_consolidacion{codigo,texto}`, `url_eli`, `url_html_consolidada`. A hit has NO `estatus_derogacion`, `estatus_anulacion`, `fecha_derogacion` (only `/metadatos` has them).

## 2. Query language

Passed as the `query` URL parameter holding a JSON document:
`{"query":{"query_string":{"query":"<expr>"},"range":{"<date field>":{"gte":"yyyymmdd","lte":"yyyymmdd"}}},"sort":[{"fecha_publicacion":"desc"}]}`.

Fields accepted inside `query_string` (HTTP 200 and effective; observed unless noted):
* `titulo` (analysed text: case-insensitive and accent-insensitive, `energía` = `energia` = `ENERGÍA`, identical response bytes), `texto` (full-text of the body: `texto:"ley del suelo"` returns hits, a nonsense term returns empty; accepted, relevance of the hits not checked), `numero_oficial` (`numero_oficial:"40/2015"`, also unquoted `6/2019` worked), `identificador`, `fecha_disposicion`, `fecha_vigencia` (also with range syntax `fecha_vigencia:[20261122 TO 20301231]`, returned laws with vigencia 2026-12 to 2027-11), `rango@codigo`, `departamento@codigo`, `ambito@codigo`, `estado_consolidacion@codigo`, `materia@codigo`, `vigencia_agotada` (`S`/`N`), `estatus_derogacion` (returned exactly the `vigencia_agotada=S` set in a 200-hit sample, i.e. same flag, inferred), `estatus_anulacion`.
* Code syntax is `field@codigo:value` (e.g. `rango@codigo:1300`, `departamento@codigo:3120` returned 100/100 hits of department 3120). Text variants such as `materia:"Seguridad Social"` are NOT valid (HTTP 500 code 101).
* Operators: `AND`, `OR`, `NOT` (`AND NOT` works), parentheses, double-quoted phrases all worked. Phrase matching is loose: `titulo:"Ley 40/2015"` returned 3 hits including two Reales Decretos. For references use `numero_oficial` plus `rango@codigo` (section 3).
* Anomaly (observed): `rango@codigo:1300` (Ley) returned 2 hits of rango 1450 (Ley Foral) among 200; combined with `titulo:energía` one Ley Foral hit appeared. Treat rank filters as approximate and re-check `rango.codigo` on the hits.
* `range` supports `fecha_publicacion` (verified). Both bounds are required: a one-sided range (`gte` only or `lte` only) is silently ignored and the unfiltered result set is returned (identical bytes to the query without `range`). Dates are `yyyymmdd`. Combining the query with the range worked (2020 `titulo:energía` gave RD 1183/2020, RDL 38/2020...).
* Top-level `from`/`to` params filter on `fecha_actualizacion`, not publication: `from=20260101&to=20260131&limit=5` returned 5 records whose `fecha_actualizacion` were all 20260130 and whose publication dates ranged from 1994 to 2026. `to=20260131` includes records of 20260130 (inclusivity of the `to` day itself not tested).
* Errors (all XML bodies even with `Accept: application/json`, `Content-Type: application/xml`, shape `<response><status><code/><text/></status><data/></response>`):
  * unbalanced parenthesis: HTTP 500, text `Server error - Code: 101`;
  * unknown field (`foo:bar`): HTTP 500 Code 101 (same code as a syntax error, so they cannot be told apart);
  * `query` not JSON: HTTP 500 `Code: 109`;
  * valid JSON with unsupported structure (`match_all`): HTTP 400 `El parámetro query tiene un formato no soportado.`;
  * bad sort field: HTTP 400 `Search error: [sort param field can't be found: nonexistent][{"query":"(TITULOS:energía) ",...` (leaks the internal index name `TITULOS`).
* Zero matches: HTTP 200, `"data": ""`.

## 3. Resolving "Ley 40/2015" and similar

Reliable recipe (observed): `numero_oficial:"N/YYYY" AND rango@codigo:<code> AND ambito@codigo:1`.

| Reference | Rank code | Query without rank | With rank (+`ambito@codigo:1`) |
|---|---|---|---|
| Ley 40/2015 | 1300 | 1 hit | 1: `BOE-A-2015-10566` |
| Real Decreto 1065/2007 | 1340 | 1 hit | 1: `BOE-A-2007-15984` |
| Ley Orgánica 3/2018 | 1290 | 13 hits (state LO + 11 regional Leyes + 1 regional decree-law) | 1: `BOE-A-2018-16673` |
| Real Decreto-ley 6/2019 | 1320 | 8 hits (Circular CNMV, 6 regional Leyes, the RDL) | 1: `BOE-A-2019-3244` |

Rank codes (table `rangos`): Ley 1300, Ley Orgánica 1290, Real Decreto 1340, Real Decreto-ley 1320, Real Decreto Legislativo 1310, Orden 1350, Resolución 1370, Circular 1390, Ley Foral 1450, Decreto 1510, Decreto-ley 1500, Decreto Legislativo 1470, Constitución 1070, etc. (19 total).
Tie-breaking: (1) always add the rank; (2) `ambito@codigo:1` removes the regional laws, which are numbered independently (Ley 1/2015 gave 11 hits with rank 1300 alone, 10 of them `ambito` 2); (3) the `departamento` of national laws is typically `7723` Jefatura del Estado, state RDs vary; (4) if still more than one hit, prefer the one whose `titulo` starts with the exact words "Ley 40/2015," and then the earliest `fecha_publicacion` / the `BOE-A-` id (inferred; a collision with `ambito=1`, same rank, same number and year was not met). Note `numero_oficial` carries the year of the disposition, not of publication (Ley 3/2018 `BOE-A-2019-3997` is a regional law published in 2019). For Órdenes and Resoluciones `numero_oficial` is often absent or like `APA/315/2020`.

## 4. Law metadata (`GET B/legislacion-consolidada/id/{id}/metadatos`)

Response `data` is a list with one object (fixtures `metadatos_*.json`). Fields: all the search-hit fields except `url_*` order differences, plus `estatus_derogacion` (`N`/`S`), `estatus_anulacion` (`N`/`S`), and, only when repealed, `fecha_derogacion` (`yyyymmdd`; Ley 30/1992: `"20210402"`). Ley 40/2015 (in force): `estatus_derogacion N`, `vigencia_agotada N`, `estado_consolidacion {3,Finalizado}`, `fecha_vigencia 20161002`, no `fecha_derogacion`. Ley 30/1992 (repealed): `estatus_derogacion S`, `vigencia_agotada S`, `fecha_derogacion 20210402`, `fecha_vigencia 19930227`, `estado_consolidacion 3`.
* `fecha_vigencia` is the date the law (original text) entered into force, not "valid until". It can be in the future for new laws (`BOE-A-2026-24302` has 20270701).
* `estado_consolidacion`: table `3` Finalizado, `4` Desactualizado (consolidated text not yet updated for recent amendments). It says nothing about whether the law is in force. `ambito`: `1` Estatal, `2` Autonómico.
* `vigencia_agotada` equals `estatus_derogacion` in the 200-hit sample (both S). Hits carry only `vigencia_agotada`, so use `vigencia_agotada` for in-force filtering in search results, `estatus_derogacion`/`fecha_derogacion` from `/metadatos` for the date.
* Partial repeals: LO 15/1999 (replaced by LO 3/2018 from 2018-12-07, per the note in its text) still has `estatus_derogacion N`, `vigencia_agotada N` (the BOE keeps some provisions alive). So the flags mean "wholly repealed", not "superseded". The `/texto` response begins with a `nota_inicial` block ("Norma derogada, con efectos de 7 de diciembre de 2018...").
* A repealed law's blocks carry no repeal marker in the per-block XML (Ley 30/1992 `a1` has one version, no marker).

## 5. Analysis (`GET B/legislacion-consolidada/id/{id}/analisis`)

`data[0]` keys: `materias`, `notas` (optional), `referencias`.
* `materias`: `[ {"materia": {"codigo":"6936","texto":"Transportes marítimos"}} ]` (object instead of list when single; `materia` may be object or list, only the object form was seen).
* `referencias.anteriores`: `[ {"anterior": [ {"id_norma":"BOE-A-2014-9467","relacion":{"codigo":"210","texto":"DEROGA"},"texto":"Arts. 12 a 15 y disposición adicional 6 de la Ley 15/2014, ..."}, ... ]} ]`. `referencias.posteriores`: same with key `posterior` and `relacion.texto` in the passive form (`SE DEROGA`, `SE MODIFICA`, `SE AÑADE`...). Ley 40/2015 had 19 anteriores and 24 posteriores. The Código Civil has no `anteriores`.
* Relation tables (`relaciones-anteriores` / `relaciones-posteriores`, 50 codes each, same codes): 2xx = corrections and repeals (201-204 corrections, 210-217 DEROGA variants, 220 ANULA, 230 DEJA SIN EFECTO, 231 SUSPENDE, 235 SUPRIME, 245 SUSTITUYE, 247 ENMIENDAS, 270-272 MODIFICA), 3xx = 300-303 PUBLICA, 330 CITA, 331 EN RELACIÓN; 4xx = 400 ACEPTA, 401 PRORROGA, 402 INTERPRETA, 404 ACTUALIZA, 406 AMPLÍA, 407 AÑADE, 408 COMPLETA, 420 APRUEBA, 421 AUTORIZA, 422 RATIFICA, 426/427 TRANSPONE, 430 ADHESIÓN, 440 DE CONFORMIDAD, 470 DECLARA, 480 DECLARA la vigencia, 490 DESARROLLA; 5xx/6xx = constitutional proceedings (520 Conflicto, 530 Cuestión, 540 cumplimiento de Sentencia, 552/694 Recurso, 693 SE DICTA).
* `notas`: free text such as `Entrada en vigor: el 2 de octubre de 2016 ...`.

### Subject (`materias`) reliability
`/analisis` returns exactly ONE materia for each of 19 laws tested, and the choice looks arbitrary (mostly alphabetically late):

| Law | materia returned |
|---|---|
| Ley 40/2015 (sector público) | 6936 Transportes marítimos |
| Código Civil | 7097 Usufructo |
| Constitución | 1616 Constitución Española |
| Ley 29/1994 Arrendamientos Urbanos | 7163 Viviendas de Protección Oficial |
| Ley 12/2023 vivienda | 7163 Viviendas de Protección Oficial |
| Código Penal (LO 10/1995) | 6962 Tribunal del Jurado |
| LEC 1/2000 | 7126 Ventas a plazos |
| Ley 58/2003 LGT | 6977 Tribunales Económico Administrativos |
| Ley 35/2006 IRPF | 6924 Transparencia fiscal |
| Ley 27/2014 IS | 6896 Títulos valores |
| LO 2/2006 Educación | 5752 Profesorado |
| Ley 19/2013 Transparencia | 7896 Sistema financiero |
| Ley 24/2013 Sector Eléctrico | 7094 Urbanismo |
| Ley 39/2015 | 6499 Seguridad Social |
| RDLeg 2/2015 Estatuto Trabajadores | 6910 Trabajo |
| RDL 32/2021 reforma laboral | 6910 Trabajo |
| LO 3/2018 protección de datos | 6980 Tribunales Superiores de Justicia |
| Ley 9/2017 Contratos | 7012 Uniones Temporales de Empresas |
| Ley 7/2021 Cambio climático | 7116 Vehículos de motor |

Verdict: the `/analisis` `materias` value is NOT usable as the law's topic (inferred: the endpoint truncates a multi-valued list to one element). In contrast, the search filter sees the full multi-valued set: `materia@codigo:6936 AND numero_oficial:"40/2015"` returns Ley 40/2015; `materia@codigo:5703` (Procedimiento administrativo) also matches it; `materia@codigo:4107` (IRPF) matches Ley 35/2006, and `3188` (Energía eléctrica) matches Ley 24/2013. `materia@codigo:6910` (Trabajo) returned a sensible set (ET, RD on equal pay, Prevención de Riesgos Laborales, LO 1/2004 violence) mixed with Ley 14/1986 Sanidad. Conclusion: use `materia@codigo` in search as a membership test (law X has topic T) and do not read the single value from `/analisis`. The full list of subjects of one law cannot be read from any endpoint I tested; recovering it would need one probe per candidate code. The materias table is a big flat thesaurus (6 822 entries, also used for ministries or organisations such as "Secretaría de Estado de Igualdad...") with no hierarchy, so topic analytics needs a curated mapping from codes to topics.

## 6. Text

* `GET B/legislacion-consolidada/id/{id}/texto/indice` (JSON works): `data: [ {"bloque": [ {"id","titulo","fecha_actualizacion","url"}, ... ]} ]`. There is NO `tipo` in the index. Ley 40/2015: 267 blocks, 86 KB. The `titulo` of encabezados is `TÍTULO PRELIMINAR`, `CAPÍTULO I`, `Sección 1` (non-breaking space ` ` between the word and the number), articles `Artículo 1` (NBSP in most), `fecha_actualizacion` = date of the last version of the block.
* `GET .../texto/bloque/{id}` and `GET .../texto` are XML only (`Accept: application/json` gives HTTP 400 "No soportado ningún mime type de la cabecera Accept." with the XML error body; `/id/{id}` without suffix and `/metadata-eli` also returned that 400 with a JSON Accept, XML not retried because of the request budget).
* Bloque XML: `<response><status/><data><bloque id="a48" tipo="precepto" titulo="Artículo 48"><version id_norma="BOE-A-2015-10566" fecha_publicacion="20151002" fecha_vigencia="20161002"><p class="articulo">...</p><p class="parrafo">...</p><blockquote><p class="nota_pie">...<a class="refPost">Ref. BOE-A-...</a></p></blockquote></version>...</bloque></data></response>`. `/texto` wraps them as `<data><texto><bloque>...`. `tipo` values seen in `/texto`: `nota_inicial`, `preambulo`, `encabezado`, `precepto`, `firma`. The `<p>` classes seen: `articulo`, `parrafo`, `nota_pie`, `siempreSeVe`; `<a class="refPost">` and `<strong>` occur inside.
* Versions: in chronological ascending order (oldest first), each `<version>` is the complete text of that block as amended; `id_norma` is the amending norm (the original law's id for the first version), `fecha_vigencia` is when that version starts. Example `a48` of Ley 40/2015: v1 `BOE-A-2015-10566` vigencia 20161002, v2 `BOE-A-2020-17340` vigencia 20210101. IRPF `a68`: 13 versions, 267 KB for ONE article (a whole-law `/texto` of a big law can be several MB). No `fecha_caducidad` attribute exists on versions; the end of a version is implicit (next version's `fecha_vigencia`). Odd attribute: the last three versions of IRPF `a68` carry `fpub=""` in addition to the normal attributes, so parse attributes by name, do not assume the set.
* Future version: `BOE-A-2026-24302` `a1` has a single version with `fecha_vigencia="20270701"` (after the retrieval date 2026-11-21), i.e. text published but not yet in force. A client must select the latest version with `fecha_vigencia <= today`, and treat "none" as not yet in force.
* Repealed article: LO 15/1999 `a40` has v1 (original text) and v2 (2018-07-31, `BOE-A-2018-10751`) whose body is `<p class="parrafo"><strong>(Derogado).</strong></p>` plus a `nota_pie` "Se deroga por la disposición derogatoria única del Real Decreto-ley 5/2018". The marker is the text `(Derogado).` (also `sin contenido` was not checked). 7 such blocks in that law. Whole-law repeal gives only a `nota_inicial` block in `/texto`.
* Block ids are opaque, NOT article numbers: Ley 40/2015 has `a1`..`a158` for most articles but Artículo 46 bis is `a4-2`, 55 bis is `a5-2`, 84 bis is `a8-2`, 108 bis/ter/quater/quinquies/sexies are `a1-2`..`a1-6` (the suffix `-N` is a collision counter, the stem is not reliably the article number). Other ids: `co`, `preambulo`, `tpreliminar`, `ti`, `ci`, `ci-2` (second chapter I in another title), `s1`, `ss1`, `da`, `da-2`, `daprimera`, `dacuaa`, `dfquinta`, `dd`, `df`, `no`, `pr`. Map article numbers via the index `titulo` (`Artículo 46 bis`), never by constructing `a46bis`. The id `a46bis` form was not seen anywhere.

## 7. Auxiliary tables (`GET B/datos-auxiliares/{name}`)

All return `{"status":..., "data": {"<code>": "<text>"}}`: an object, code to text, not a list. Sizes: `materias` 6 822 entries (337 KB, 1.9 s), `departamentos` 211 (10 KB), `rangos` 19, `ambitos` 2 (`1` Estatal, `2` Autonómico), `estados-consolidacion` 2 (`3` Finalizado, `4` Desactualizado), `relaciones-anteriores` 50, `relaciones-posteriores` 50. Note `rangos` is a mix of state and regional ranks. Codes are strings. Texts may have trailing spaces (`SE DEROGA tácitamente `).

## 8. Daily summaries

* `GET B/boe/sumario/20261120` (Friday): 407 KB, 1.8 s. `data.sumario.metadatos{publicacion,fecha_publicacion}`, `diario: [ {numero, sumario_diario{identificador:"BOE-S-2026-289", url_pdf{szBytes,szKBytes,texto}}, seccion:[ ... ]} ]` (`diario` is a list). Sections observed: `1`, `2A`, `2B`, `3`, `5A`, `5B`, `5C` (no sections 4 or 6 here). Nesting: `seccion > departamento{codigo,nombre} > epigrafe{nombre} > item`, or `seccion > departamento > item` directly (sections 5A/5B), or a department with only `texto` (section 5C). `item`: `identificador` (`BOE-A-2026-24291`), `control`, `titulo`, `url_pdf{szBytes,szKBytes,pagina_inicial,pagina_final,texto}`, `url_html`, `url_xml`. 282 items that day. All collection levels collapse to objects when there is one element.
* `GET B/borme/sumario/20261120`: 80 KB, 1.5 s. `diario[0].numero 223`, `sumario_diario.identificador BORME-S-2026-223`, `seccion` with codes `A` (31 items), `B` (a single item, serialised as an object), `C` (no items, probably text only). Items: `identificador` (`BORME-A-2026-223-01`), `titulo` (a province, `ARABA/ÁLAVA`), `url_pdf`, `url_html`, `url_xml`. No `departamento` level.
* Sunday and pre-1978 date behaviour: NOT verified (budget exhausted).

## 9. HTTP behaviour

* `Accept` is selected by `Vary: Accept`. `application/json` works on search, `/metadatos`, `/analisis`, `/texto/indice`, `datos-auxiliares/*`, `sumario`. `/texto` and `/texto/bloque/*` only answer XML. JSON `Accept` on those: HTTP 400 `No soportado ningún mime type de la cabecera Accept.` (XML body, `<code>400</code>`). A request with the `Accept` header absent and a 404 for an unknown id were NOT tested.
* Errors always come as XML with the same envelope, even for JSON requests (HTTP 400/500).
* Caching: responses carry no `ETag`, `Last-Modified` or `Cache-Control` (only `Date`, `Age`, `Vary: Accept`, `Accept-Ranges: bytes`, `Strict-Transport-Security`, CORS `Access-Control-*`). `Age` was 0 or small (e.g. 52 s on the sumario), so there is a front cache. Conditional requests (`If-None-Match`, `If-Modified-Since`) were not sent; with no validators offered they cannot work (inferred).
* No gzip: with `Accept-Encoding: gzip` no `Content-Encoding` came back on four large responses (13 MB search, 337 KB materias, 134 KB texto, Ley 40/2015). Large responses use `Transfer-Encoding: chunked`.
* No rate-limit headers (`X-RateLimit-*`, `Retry-After`) seen. No 429 at about 1 request per 1.2 s.
* Encoding: UTF-8 (`<?xml version="1.0" encoding="utf-8"?>`); JSON uses `\uXXXX` and `\/` escapes, and uses non-breaking spaces (` `) in `titulo` of blocks.
* JSON vs XML parity: only checked via fixtures that exist in one format. Equivalence of the two representations of the same endpoint was NOT verified.

## 10. robots.txt (`https://www.boe.es/robots.txt`, 499 KB, 12 434 Disallow lines, one `User-agent: *` group, no Crawl-delay or Sitemap)

* Nothing mentions `/datosabiertos/` or `/api/`. None of the ids `BOE-A-2015-10566`, `BOE-A-2015-10565`, `BOE-A-1889-4763`, `BOE-A-1978-31229` appears.
* Relevant lines, quoted: `Disallow: /diario_boe/xml.php?` and `Disallow: /diario_borme/xml.php?` (the `url_xml` item links of the summaries are therefore disallowed for crawlers; "# Descartar xml"), `Disallow: /datos/pdfs/` ("# No duplicar los pdfs"), `Disallow: /diario_boe/verifica.php?`, `Disallow: /buscar/legislacion.php?`, `Disallow: /buscar/legislacion_ava.php?`, `Disallow: /buscar/boe.php?`, `Disallow: /buscar/anboe.php?`, `Disallow: /buscar/personal.php?`, ... (search result pages), language variants `Disallow: /buscar/act.php?*lang=en` (also ca, va, gl, eu, fr) and `/buscar/doc.php?*lang=..`, `/diario_boe/txt.php?*lang=..`, `Disallow: /boe_n/`, `/boe_j/`, `/notificaciones/`, `/edictos_judiciales/`.
* About 12 000 further lines block individual items and days: `Disallow: /buscar/act.php?id=BOE-A-2026-4920`, `Disallow: /*BOE-A-2026-4920.pdf`, `Disallow: /boe/dias/2026/03/02/` (whole days, many of 1999 etc.), and a few `Allow:` for two specific BOE ids. The HTML link `url_html_consolidada` (`/buscar/act.php?id=...`) is not blocked as such (only with `lang=` and for the listed ids). The API itself is the supported machine route; do not fetch `/buscar/` or `xml.php` pages.

## Implications for the client

* Page size: use `limit=100` for interactive paging (127 KB, ~2 s), `limit=1000` for bulk (1.3 MB, ~5 s). Avoid `limit=-1` (13 MB, 20 s). Always pass an explicit `limit`.
* Paging: loop `offset += len(page)` until the page is empty, and also stop when `data == ""` or a short page arrives. Counting results costs a full download of the matching set (about 1.3 KB per hit); for a total, one request with a very large `limit` and `len(data)` is cheapest in round trips, bounded by 10 000 for `-1`. For "is there more" checks use `limit=1&offset=N`. Add a max-pages guard.
* Use an explicit `sort` for stable paging (`fecha_publicacion` + ids de-duplicated client-side) when crawling; unsorted order moves as laws are updated. De-duplicate by `identificador`.
* Normalise single-element collections (object vs list vs `""`/absent) in one helper. Treat the status code from the XML error body when `Content-Type` is `application/xml` even if JSON was requested.
* Retry policy: do not retry 400 (and 500 with code 101/109, which are query errors). Retry timeouts, connection errors and unexpected 5xx up to 3 times with exponential backoff (1, 2, 4 s) and jitter; honour `Retry-After` and 429 if they ever appear (never seen). Keep at least 1 s between requests; send an identifying User-Agent; always send `Accept`.
* Cache lifetimes (no HTTP validators exist, so cache client-side; these are suggestions): `datos-auxiliares/*` 7 days (code tables are static, materias is 337 KB); search results 15 to 60 min (updates appear within the day, `fecha_actualizacion` of the head record was minutes old); `/metadatos` and `/analisis` 6 to 24 h, keyed by `fecha_actualizacion` from search hits (re-fetch when the hit's `fecha_actualizacion` is newer); `/texto/indice` and `/texto/bloque` keyed by block `fecha_actualizacion` (indefinite while it is unchanged); `boe/sumario/{date}` past days immutable (cache for good; do not cache today's for more than ~1 h until published), a day with no publication should be cached shortly.
* Fields to trust: `identificador`, `titulo`, `fecha_publicacion`, `fecha_disposicion`, `rango.codigo`, `numero_oficial` (when present), `departamento.codigo`, `ambito.codigo`; `vigencia_agotada` and, from `/metadatos`, `estatus_derogacion`/`fecha_derogacion` for being repealed (a repealed flag means wholly repealed; partially superseded laws keep `N`). Do not trust: `/analisis` `materias` as a topic (single arbitrary item); `estado_consolidacion` as an in-force signal; `fecha_vigencia` as a validity end; `rango@codigo` filter as exact (anomaly with 1450); `url_xml` for fetching (robots.txt).
* Resolving references: use `numero_oficial:"N/YYYY" AND rango@codigo:C AND ambito@codigo:1` (section 3).
* Topic analytics: filter by `materia@codigo` in search, curate a topic-to-materia-code mapping, and count by the filter; do not derive topics from `/analisis`.
* Text parsing: get article numbers from index `titulo`, strip NBSP; versions chronological, pick the last with `fecha_vigencia <= date`; detect repeal by the `(Derogado).` text; parse attributes by name; expect multi-MB responses for whole-law `/texto`, so prefer per-block requests with the index.

## Verified after the probe (2026-11-21)

* Unknown id (`GET B/legislacion-consolidada/id/BOE-A-1900-99999/metadatos`, JSON `Accept`): HTTP 404, XML body `<status><code>404</code><text>La información solicitada no existe</text></status><data/>`.
* No `Accept` header at all: HTTP 400, XML body, text `No reconocido el formato de la cabecera Accept.`
* Sunday summary (`GET B/boe/sumario/20261115`): HTTP 404 with the same body as an unknown id, so "no publication that day" and "unknown resource" are indistinguishable by response alone.

## Unverified / open

* Conditional requests (`If-None-Match`, `If-Modified-Since`): not tried; no validators in responses.
* Pre-1978 date for `sumario` (status and body); BORME on a weekend.
* Whether `/metadata-eli` and `/id/{id}` serve XML with `Accept: application/xml` (JSON gave 400); JSON-vs-XML parity of the same endpoint.
* Largest honoured `limit` (20000 was accepted without error but returned only 77 records); behaviour when `limit` exceeds the number of records at offset 0.
* Whether `to` in `from`/`to` is inclusive of the named day; which other fields `range` accepts besides `fecha_publicacion` (`fecha_vigencia` range worked with `[a TO b]` syntax in `query_string`).
* Whether `texto:` search is true full text of the articles (it accepts the field and returns plausible hits only).
* Secondary ordering for ties under `sort fecha_publicacion`; whether the default order is exactly `fecha_actualizacion` desc for query results with relevance.
* A same-rank, same-number, same-year collision among `ambito=1` records.
* Whether a block with "sin contenido" or other repeal wording exists besides `(Derogado).`; a version-level `fecha_caducidad` (none seen).
* The exact set of `rango` values accepted when filtering regional ranks (1470, 1500...), and ids for non-BOE sources (`BOCL-h-`, `DOGC-f-`, `BORM-s-`): `/texto` availability untested.
