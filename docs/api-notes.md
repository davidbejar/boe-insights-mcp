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
* Valid `sort` fields seen: `fecha_publicacion`, `fecha_actualizacion` and `fecha_disposicion` (asc/desc). `sort` on `titulo` is a 400 ("can not sort on multivalued field: TITULOS"); an unknown sort field is a 400 "sort param field can't be found".
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
* Escaping (verified live on 2026-11-21): backslash-escaping `query_string` reserved characters inside a field value works. `titulo:40\/2015`, `titulo:(Decreto\-ley AND medidas AND urgentes)` and escaped parentheses and colons all returned HTTP 200, with sensible hits or an empty result and no syntax error. `build_search_query` relies on this.
* Sorting (verified live on 2026-11-21): `sort` also accepts `fecha_disposicion` (the enactment date). An ascending sort of rank 1290 (Ley Orgánica) returned Ley Orgánica 1/1979 first. The valid sort fields are therefore `fecha_publicacion`, `fecha_actualizacion` and `fecha_disposicion`.

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

### 6.1 Text probe 2 (2026-11-21, 43 requests, no 429/5xx; fixtures `texto_*`, `error_404_*` in `tests/fixtures/boe/`)

Verified in this second probe (observed unless marked inferred):

* The index never carries `tipo` (checked on 13 indexes); the keys are exactly `id`, `titulo`, `fecha_actualizacion`, `url`. Block ids are unique within an index in every law checked (0 duplicate ids in 2 444 Código Civil blocks and the 12 other indexes); titles are NOT unique, so `titulo` alone does not identify a block, only `id` does.
* Sizes/times of `/texto/indice`: Constitución 210 blocks 66 KB 1.8 s; Ley 39/2015 195 blocks 62 KB 1.3 s; Código Civil 2 444 blocks 763 KB 2.0 s (about 310 B per block; the `url` is most of it); Ley 37/1992 (IVA) 307 blocks 98 KB; RD 1065/2007 365 blocks 116 KB.
* **Correction to the version-order statement above.** Versions are in the order the server lists them, which is usually but NOT always ascending by `fecha_vigencia`. Seen: IVA `an` (Ley 37/1992 annex, 20 versions) lists `fecha_vigencia` 20150101 (BOE-A-2014-12329) and then 19950101 (BOE-A-1994-28968) and then 20170629, so one version is out of order; Ley 30/1992 `no` lists the 2015 repeal note (empty `fecha_vigencia`) BEFORE the 1992 original (19930227). Clients must sort by `fecha_vigencia` themselves (empty = unknown, see below) and must not rely on the position.
* `fecha_vigencia` CAN be empty (`fecha_vigencia=""`): RD 1624/1992 `dt` (Disposición transitoria cuarta), last of 6 versions (`id_norma` BOE-A-2026-4667, published 20260228); and Ley 30/1992 `no` first version (BOE-A-2015-10565, published 20151002; the same repeal note elsewhere gives 20161002 as effective date in its text). Inferred: empty means "effective date not given / defined by the text of the amending norm"; the `fecha_publicacion` is always present in the examples. Two versions sharing the same `fecha_vigencia` were NOT seen (checked 62 versions); the same `fecha_publicacion` and `fecha_vigencia` on adjacent versions did occur (e.g. IVA `an` v18/v19 are consecutive days). Ties are therefore possible in principle but unobserved.
* `id_norma` of the first version is the law itself in every block seen (`BOE-A-1992-28740` for IVA `an` v1, `BOE-A-2015-10565` for Ley 39/2015 `ti`). Exception: the `no` block carries the id of the norm that wrote the note (the repealing law, e.g. `BOE-A-2018-16673` for LO 15/1999), and the `no` of RD 424/2005 carries `BOE-A-2026-24190`.
* Unknown block in an existing law (`GET B/legislacion-consolidada/id/BOE-A-2015-10565/texto/bloque/zzz`, `Accept: application/xml`): HTTP 404, body `<status><code>404</code><text>La información solicitada no existe</text></status><data/>`, identical to an unknown law and to a missing `no` block (`error_404_unknown_block_zzz.xml`).
* `tipo` values seen in blocks: `precepto`, `encabezado`, `preambulo`, `nota_inicial` (plus `firma` from the earlier probe). Shapes: `encabezado` (`ti` of Ley 39/2015, `titulo="TÍTULO I"`) has ONE version with `<p class="titulo_num">TÍTULO I</p><p class="titulo_tit">De los interesados en el procedimiento</p>`; `preambulo` (`id="preambulo"`, no `titulo` attribute shown in the index, 33 KB for Ley 39/2015) one version of `p class="parrafo"`/`parrafo_2` paragraphs; the annex `an` is `tipo="encabezado"` (not "anexo") with `<p class="anexo">` paragraphs; disposiciones are `tipo="precepto"` (RD 1624/1992 `dt`, 6 versions). Article-level `titulo` attribute on the bloque equals the index `titulo`.

#### Element and class inventory inside `<version>` (counted over the 13 block fixtures of this probe plus the earlier ones; IVA `an` and Ley 29/1987 `a21` contribute most)

Elements: `p` 4 309, `td` 2 280, `tr` 930, `a` 396, `th` 107, `table` 64, `tbody` 64, `blockquote` 46, `col` 24, `colgroup` 12, `thead` 12, `br` 11, `strong` 10. No `img`, no formula/MathML, no `ul/ol/li`, no `sup/em` were seen (the absence of images and formulas is observed only in these laws; other laws were not searched).
`p` classes: `parrafo`, `parrafo_2` (indented sub-paragraph), `articulo` (the heading line "Artículo N. Heading.", seen in the earlier probe), `anexo`, `titulo_num`, `titulo_tit`, `nota_pie`, `nota_pie_2`, `siempreSeVe` and `textoCompleto` (the two classes used inside `nota_inicial`), `cita_con_pleca` ("Redacción anterior:" label), `centro_redonda`, and table paragraphs `cabeza_tabla`, `cuerpo_tabla_izq`, `cuerpo_tabla_centro`, `cuerpo_tabla_der`. A `<p>` can be empty or contain only a non-breaking space (two such paragraphs follow the correction note in `no` of RD 1624/1992 and of Ley 30/1992).
`a` classes: `refPost` (385; text `Ref. BOE-A-...#anchor`); an `a` without class has `href` and `target="_blank"` and wraps a `<br/>` (the link to a corrección de errores); `a` can be nested inside `a` (`<a class="refPost"><a class="refPost">Ref. ...</a></a>` in `no` of Ley 30/1992).
Tables: `<table class="tabla" border="0" summary="Tabla de datos" [width height]>` containing `<tbody>` only, header cells as `<th>` with `<p class="cabeza_tabla">` (Ley 29/1987 `a21`: one `<tr>` of five `th`, then `tr` of `td`); or `<table class="ning_n_estilo_de_tabla"><colgroup><col class="row_column_11"/>...<thead><tr class="row_column_7"><td><p class="cabeza_tabla">..` (here the header cells are `td`, not `th`) followed by `<tbody>`. `tr` classes `row_column_N`. Every cell holds one or more `<p>` (cell text is not bare). No `colspan`, `rowspan`, or any attribute on `td`/`th` except `class` on 3 `td` was seen. Rows have different numbers of cells only across tables, not inside one (not exhaustively checked).
Blockquote: `<blockquote>` (40) or `<blockquote class="noDesde20160101">` (1; the class is a date-bound visibility switch) as a direct child of `<version>`, always holding `<p>` children (`nota_pie`, `parrafo`, `cita_con_pleca`). "Téngase en cuenta" notes appear as `<p class="parrafo">Téngase en cuenta ...</p>` inside such a blockquote (IVA `an`) or as a `p class="siempreSeVe"` in a `nota_inicial` (Ley 11/2007). Footnotes ("Se modifica ...", "Redactado el punto 2 conforme a ...") are `p class="nota_pie"`, directly under `<version>` or inside a blockquote.
Text directly inside `<version>` or `<blockquote>` without a `<p>`: NOT seen (0 occurrences in all fixtures: no non-blank `.text` or `.tail`). Inferred that the parser's fallback is only defensive.

### Index title patterns

All patterns are observed. NBSP (U+00A0) occurs in `Artículo N` titles of Ley 39/2015 (133 of 195), Ley 40/2015, RD 186/2023 and RD 784/2021 (the 2015+ laws) but in none of the other 9 indexes checked (Constitución, IVA, Código Civil, RD 887/2006, RD 424/2005...); so always normalise.

| Pattern | Example `titulo` | Example block id |
|---|---|---|
| Article, bare number, no heading, no period (all modern laws) | `Artículo 14` | `a14` |
| Article with `bis/ter/quater/quinquies/sexies/quáter/quinque` | `Artículo 8 bis`, `Artículo 140 quinque` | `a8-2` (collision form) or `a140bis` (stem form); both occur, even in one law |
| Article range | `Artículos 163 bis a 163 quáter` | `as163bisa163quater` |
| Article, ordinal word (old laws, 1960) | `Artículo primero`, `Artículo doce`, `Artículo veintidós` | `aprimero`, `adoce`, `aveintidos` |
| Sole article (RD that approves a Reglamento) | `Artículo único` | `aunico` or `au` |
| Código Civil abbreviated form (no "Artículo" at all) | `Art 1`, `Art 94 bis`, `Art. 958 bis`, `Arts 127 a 130`, `Arts. 102 a 104` (also in RD 424/2005), `Art. 1231 A 1253` | `a1`, `art2`, `a9` (= Art 94 bis!), `art.1863bis`, `arts.1863bisa1873bis.` |
| Código Civil untitled-number blocks (§, headings by name) | `§ 1`, `De la imputación de pagos`, `Disposiciones generales`, `Disposición general` | `S1`, `delaimputaciondepagos`, `disposicionesgenerales` |
| Disposición adicional / transitoria / final, ordinal | `Disposición adicional primera`, `Disposición final séptima` | `daprimera`, `dfseptima`; also `da`, `da-2`, `dd`, `df` for later ones |
| Disposición, single | `Disposición transitoria única`, `Disposición derogatoria única`, `Disposición final única` | `dtunica`, `ddunica`, `dfunica` |
| Disposición, no ordinal (Constitución) | `Disposición derogatoria`, `Disposición final` | `dd`, `df` |
| Disposición ordinal beyond twelfth | `Disposición adicional decimotercera` | `da` |
| Disposición with a 1ª-style numeral or "Disposición final 1" | NOT seen as a title (these appear in notes: `disposición final 9`) | |
| Disposición heading text after the ordinal | NOT seen; titles are never `Disposición adicional primera. Título...` | |
| Disposición, Código Civil style (bare ordinal under a group heading) | `Primera`, `Séptima`, `DISPOSICIONES TRANSITORIAS` | `primera`, `primera-2` (colliding with the adicionales), `dt`, `da` |
| Typo in source | `Dispoosición adicional ` (trailing space) | `da` (Ley 49/1960) |
| Libro / Título / Capítulo / Sección / Subsección, three casings | `LIBRO PRIMERO`, `TÍTULO PRELIMINAR`, `TÍTULO II`, `CAPÍTULO PRIMERO`, `Capítulo I`, `CAPÍTULO ÚNICO`, `Sección 1`, `SECCIÓN 1`, `Sección 1ª`, `Sección primera`, `Subsección 1ª` | `lprimero`, `tpreliminar`, `tii`, `cprimero`, `ci`, `cunico`, `s1`, `sprimera`, `s1a` |
| Heading ids repeat with a counter | `CAPÍTULO I` (5 times in Ley 39/2015, 3 in the Código Civil title preliminar region, 40 `CAPÍTULO II`) | `ci`, `ci-2`... `cii-38` |
| Law title block | `CÓDIGO CIVIL`, `REGLAMENTO` | `codigocivil`, `reglamento` |
| Annex | `ANEXO` (also the only annex title seen; `ANEXO I` was not seen) | `an` |
| Linked-information block (non-text) | `[Información relacionada]` | `informacionrelacionada` |
| Preamble / exposición de motivos | empty `''` (Ley 39/2015, Constitución), or `[preambulo]` (RD 186/2023, RD 784/2021) | `preambulo` or `pr` |
| Signature | empty `''`, or `[firma]` | `firma` or `fi` |
| Initial note | empty `''` | `no` |
| Other empty titles | `co` (Ley 40/2015; meaning inferred, probably a cover/title block) | `co` |

Findings on the questions: (1) articles are never titled with a heading after the number and never with a trailing period (only the range title `Arts. 1863 bis a 1873 bis.` ends in one); (2) `Artículo único` exists as its own title, not `Artículo 1`; (3) empty titles occur for preamble, firma, nota inicial, `co`; (4) two blocks with the SAME title "Artículo 1" exist: RD 1624/1992 (`BOE-A-1992-28925`, Reglamento del IVA) has `a1` = `Artículo 1` of the Real Decreto (index position 2) and `a1-2` = `Artículo 1` of the Reglamento (position 14); likewise `Artículo 2` (`a2`, `a2-2`), and `Disposición adicional única` / `Disposición transitoria primera` appear twice in RD 424/2005 (`daunica`, `daunica-2`). Such duplicates are distinguishable only by position and by id order (the second carries `-2`); blocks between them are an `ANEXO`/`REGLAMENTO` heading. The locator must report ambiguity and list candidates rather than pick one. (5) Ids are unique within a law; the collision counter `-N` is not tied to the article. Inferred: the original-text block keeps the unsuffixed id.

### Initial note (`no`, `tipo="nota_inicial"`)

* Listed in 5 of the 13 indexes fetched in this probe: LO 15/1999 (`no`, `fecha_actualizacion` 20181206), Ley 30/1992, RD 424/2005, RD 887/2006, RD 1624/1992; absent from the other 8 (Constitución, Ley 39/2015, Código Civil, Ley 37/1992, Ley 49/1960, RD 1065/2007, RD 186/2023, RD 784/2021). The index entry has an empty `titulo`.
* `GET .../texto/bloque/no` (Accept `application/xml`): present -> HTTP 200, 0.5 to 2 KB, `<bloque id="no" tipo="nota_inicial">` with one or more `<version>`; absent -> HTTP 404 with the standard "La información solicitada no existe" body (Ley 39/2015, Ley 40/2015, Ley 30/2007, Ley 7/2007, RDLeg 1/2008). So no index is needed: a single `GET .../texto/bloque/no` answers it, costing ONE request per law (about 1 s; one call took 9.4 s).
* A `nota_inicial` is NOT always a repeal. Non-repeal notes seen: RD 424/2005 (in force) "Con efectos de 1 de enero de 2027, esta norma pasa a denominarse ..."; RD 1624/1992 and RD 887/2006 "Incluye la corrección de errores publicada en BOE núm. ..." (class `textoCompleto`). Repeal notes use class `siempreSeVe`.
* Wording variants (first sentence, all start with `Norma derogada`):
  1. LO 15/1999: "Norma derogada, con efectos de 7 de diciembre de 2018, sin perjuicio de lo previsto en la disposición adicional 14 de la Ley Orgánica 3/2018, de 5 de diciembre, según establece su disposición derogatoria única."
  2. Ley 30/1992: "Norma derogada, con efectos de 2 de octubre de 2016, por la disposición derogatoria única.2.a) de la Ley 39/2015, de 1 de octubre." (second paragraph: "No obstante, hasta el 2 de abril de 2021, se mantuvieron en vigor ...")
  3. Ley 11/2007: "Norma derogada, con efectos de 2 de octubre de 2016, por la disposición derogatoria única.2.b) de la Ley 39/2015, de 1 de octubre." (then "Téngase en cuenta que ...")
  4. Ley 22/2011: "Norma derogada, con efectos de 10 de abril de 2022, por la disposición derogatoria primera.1 de la Ley 7/2022, de 8 de abril."
  5. Ley 7/2010: "Norma derogada, con efectos de 9 de julio de 2022, por la disposición derogatoria única 1.b) de la Ley 13/2022, de 7 de julio." (then a transitional-regime paragraph)
  Wording is stable: `Norma derogada, con efectos de <date>, ...`. Other variants (e.g. "derogada parcialmente", "vigencia agotada") were NOT seen in this sample; inferred possible.
* The note can have several versions (Ley 11/2007: 2; Ley 30/1992: 2, the repeal note first and with empty `fecha_vigencia`, the original-text note "Incluye las correcciones de errores..." second). The repeal note is repeated in each version.
* **The note is NOT a complete detector.** Ley 30/2007 de Contratos del Sector Público is wholly repealed (`/metadatos`: `estatus_derogacion` `S`, `fecha_derogacion` 20111216, `vigencia_agotada` `S`) but `bloque/no` returns 404. Ley 7/2007 (EBEP) and RDLeg 1/2008 also return 404 (their repeal status was not checked, inferred repealed/superseded for 7/2007). LO 15/1999 has `estatus_derogacion` `N` in `/metadatos` yet its note says "Norma derogada" (the note is the only signal there). The reverse (a note starting "Norma derogada" on a law whose metadata say not repealed) is LO 15/1999 again.
* Recommended rule (for wholly repealed): treat a law as wholly repealed if EITHER `/metadatos` has `estatus_derogacion == "S"` (1 request, usually already needed) OR its `bloque/no` (1 request, 404 = none) has any version whose first `<p>` text, after NBSP/whitespace normalisation and case-folding, starts with `norma derogada`. Do not use the mere existence of `no`. Report which signal fired. Cost: 1 request when metadata suffice and `S`; otherwise 2. Do not match the word "derogad" anywhere in the note (corrections and renames do not use it, but notes can mention repeals of other norms; inferred).

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
* (probe 2 additions) Not verified: images/formulas inside versions (none in 13 blocks, no search for them); `ANEXO I`-style multiple annex titles; whether any version can share the exact `fecha_vigencia` with another; `colspan`/`rowspan` (none seen); wording of notes for laws that are partially repealed or "vigencia agotada" without repeal; the actual `estatus_derogacion` of Ley 7/2007 and RDLeg 1/2008 (they have no `no` block); `Disposición final 1ª` style titles (not seen); JSON `Accept` on `bloque/no` returns 400 (assumed same as other blocks, not requested).
