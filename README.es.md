# boe-insights-mcp

[English](README.md) | **Español** | [Sitio de documentación](https://davidbejar.github.io/boe-insights-mcp/es/)

[![CI](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Servidor [MCP](https://modelcontextprotocol.io) (Model Context Protocol) que ofrece a asistentes de IA como Claude herramientas analíticas sobre los datos abiertos del BOE (Boletín Oficial del Estado). En lugar de una herramienta por cada endpoint de la API, las herramientas se diseñan en torno a las preguntas que la gente se hace de verdad: "¿sigue vigente esta ley?", "¿qué ha cambiado en este artículo?", "¿cómo ha evolucionado la legislación sobre X?".

**Estado:** desarrollo temprano. Hoy hay veintitrés herramientas disponibles (localizar una norma, buscar, consultar su situación de vigencia y buscar códigos; historial y cadenas de modificaciones; leer, comparar y consultar el historial de cada artículo; comprobar citas contra el texto; el índice de una norma; análisis de tendencias sobre toda la colección consolidada; el diario oficial día a día; y búsquedas guardadas), además de cinco [recursos](#recursos-y-prompts) y ocho prompts (cuatro, cada uno en español y en inglés); todo lo demás está en la [hoja de ruta](#hoja-de-ruta). Todavía no está en PyPI (la versión 0.1.0 es la siguiente): se ejecuta con `uvx` directamente desde GitHub o desde el código fuente, mira el [inicio rápido](#inicio-rápido).

## Qué puedes preguntar

Nueve ejemplos prácticos, cada uno a partir de una ejecución real del servidor (los ejemplos 1 a 4 el 2026-11-21 y los ejemplos 5 a 9 el 2026-11-22). Primero la pregunta, después las llamadas a las herramientas en orden y un extracto del resultado (los títulos largos se acortan con "…").

### 1. Leer una norma conocida

*"Muéstrame la estructura de la ley de protección de datos (Ley Orgánica 3/2018) y lee el artículo sobre el derecho de supresión."*

```text
resolve_reference(reference="Ley Orgánica 3/2018")          -> BOE-A-2018-16673
get_law_structure(law_id, detail="headings", limit=12)      -> solo títulos y capítulos
get_law_structure(law_id, limit=200)                        -> añade los epígrafes de los artículos
read_article(law_id, article="artículo 15")
```

- La vista de epígrafes lista 24 entradas (preámbulo, 10 títulos, 8 capítulos, 5 secciones), por ejemplo "TÍTULO III. Derechos de las personas". El índice completo tiene 168 entradas, entre ellas 98 artículos y 46 disposiciones.
- Entre los epígrafes de artículos figuran "Artículo 15. Derecho de supresión" y "Artículo 93. Derecho al olvido en búsquedas de Internet".
- `read_article` devuelve el artículo 15 (`state` `in_force`, en vigor desde 2018-12-07): "1. El derecho de supresión se ejercerá de acuerdo con lo establecido en el artículo 17 del Reglamento (UE) 2016/679. …"
- El artículo 93 (`read_article` con `article="artículo 93"`) empieza así: "Toda persona tiene derecho a que los motores de búsqueda en Internet eliminen de las listas de resultados …"

### 2. Qué cambió en un artículo

*"¿Qué cambió en el artículo 48 de la Ley 40/2015 desde 2017?"*

```text
resolve_reference(reference="Ley 40/2015")                  -> BOE-A-2015-10566
compare_article_versions(law_id, article="artículo 48", from_date="2017-01-01")
```

- El texto vigente el 2017-01-01 es la versión que empezó el 2016-10-02; la más reciente empezó el 2021-01-01 y la introdujo `BOE-A-2020-17340`. No hay versiones intermedias (`intermediate_versions` 0).
- Cambió un párrafo (`changed` 1, `unchanged` 11, `added` 0, `removed` 0).
- Comparación en línea del párrafo sobre los convenios: "resultarán eficaces una vez [-inscritos-] {+inscritos, en el plazo de 5 días hábiles desde su formalización,+} en el Registro Electrónico estatal …" y, más adelante, "{+en el plazo de 10 días hábiles desde su formalización+} en el «Boletín Oficial del Estado»".

### 3. Una tendencia y su explicación

*"¿Por qué hubo tantos decretos-leyes en 2020?"*

```text
legislation_over_time(rank="real_decreto_ley", period="year", from_date="2010-01-01", to_date="2025-12-31")
explain_spike(rank="real_decreto_ley", period_key="2020", from_date="2010-01-01", to_date="2025-12-31", max_laws=8)
```

| Año | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 |
|---|---|---|---|---|---|---|
| Reales decretos-leyes en la colección (`count`) | 19 | 12 | **38** | 28 | 20 | 8 |
| Número oficial más alto visto (`highest_number`) | 28 | 18 | **39** | 32 | 20 | 8 |

- De 2010 a 2025 hay 230; 2020 es el pico más fuerte: 38 frente a una referencia de 11.0 (ratio 3.45).
- La colección no contiene todas las normas de un año, por eso este resultado incluye también `highest_number`: el número oficial más alto visto ese año, una cota inferior de cuántas se aprobaron. 2020 contiene 38 de al menos 39. Los años vecinos están más incompletos (2019: 12 de al menos 18; 2018: 19 de 28; 2021: 28 de 32), mientras que 2022 y 2023 parecen completos (20 de 20, 8 de 8).
- La conclusión se sostiene con esa cobertura: 2020 tiene más decretos-leyes que cualquier otro año de 2010 a 2025, tanto por las normas que contiene (38) como por el número más alto visto (39; el siguiente es 32, en 2021). Los años recientes están incompletos (2025 contiene 4 de al menos 16), así que no conviene leer el descenso desde 2023 como una tendencia.
- `explain_spike` para 2020 fija la referencia en 10.2 (los 5 años anteriores). Su desglose (`contributions`) está vacío aquí: las 38 normas son leyes estatales de la Jefatura del Estado, y una dimensión con un único valor no se informa.
- `laws` es una muestra equiespaciada de las 38 por orden de fecha (`max_laws=8`, con la primera y la última): Real Decreto-ley 1/2020 (pensiones), 7/2020 (impacto económico del COVID-19), 13/2020 (empleo agrario), 18/2020 (medidas sociales en defensa del empleo), 23/2020 (energía y reactivación económica), 28/2020 (trabajo a distancia), 34/2020 (solvencia empresarial, sector energético y materia tributaria) y 36/2020 (modernización de la Administración Pública y Plan de Recuperación).
- La herramienta cuenta y lista normas; la relación entre el pico y la pandemia la lee el usuario en los títulos.

### 4. Normas más modificadas

*"¿Qué normas de protección de datos se han modificado más?"*

```text
most_amended_laws(title="protección de datos", from_date="2007-01-01", limit=5, max_laws=10)   -> complete false
most_amended_laws(title="protección de datos", from_date="2007-01-01", limit=5, max_laws=10)   -> complete true
```

Cada llamada tiene un presupuesto de 40 segundos (contados desde el inicio de la llamada, así que la primera incluye la descarga del catálogo) y consulta los datos de modificaciones de como máximo `max_laws` normas, una petición por norma. Esta herramienta no tiene límite inferior de fechas por defecto: aquí se añadió `from_date="2007-01-01"` a las llamadas anteriores, con lo que coinciden 14 normas (sin él, coinciden 28). Con la caché vacía, la primera llamada examinó 9 (`complete` false, con una nota que pide repetir la llamada) y la segunda continuó desde la caché y examinó las 14 (`complete` true). Resultado de la segunda llamada, normas publicadas desde 2007:

| Modificaciones | Norma |
|---|---|
| 5 | Ley Orgánica 3/2018, de Protección de Datos Personales y garantía de los derechos digitales |
| 3 | Ley 32/2010, de la Autoridad Catalana de Protección de Datos |
| 1 | Real Decreto 1720/2007, … Reglamento de desarrollo de la Ley Orgánica 15/1999 … |
| 1 | Ley 2/2004, de Ficheros de Datos de Carácter Personal de Titularidad Pública y de Creación de la Agencia Vasca de Protección de Datos |
| 1 | Real Decreto-ley 5/2018, … adaptación del Derecho español a la normativa de la Unión Europea en materia de protección de datos |

Las derogaciones cuentan como modificaciones: la Ley 32/2010 tiene 2 modificaciones y 1 derogación.

### 5. Comparar las comunidades autónomas

*"¿Qué comunidades autónomas han legislado más sobre vivienda desde 2015?"*

```text
top_departments(scope="regional", title="vivienda", from_date="2015-01-01")
search_legislation(scope="regional", title="vivienda", department_code="8121", published_from="2015-01-01")
get_law_overview(law_id="BOE-A-2018-9774")
```

Alrededor del 29 % de la colección (3.623 de 12.477 normas cuando se ejecutó) es autonómica. Para "vivienda", 48 normas desde 2015 repartidas entre 12 comunidades (el filtro de título también encuentra el plural "viviendas"):

| Comunidad | Normas |
|---|---|
| Illes Balears | 8 |
| Canarias, Cataluña | 7 cada una |
| Región de Murcia | 6 |
| Andalucía, Extremadura, Navarra, Madrid, Comunitat Valenciana | 3 cada una |
| Aragón | 2 |

Las otras 2 comunidades (3 normas) se agrupan como "other". (El paso con `search_legislation` solo sirve para encontrar un identificador entre las normas de Baleares; devuelve 7 normas, entre ellas la Ley 5/2018, "de la vivienda de las Illes Balears". Su filtro `title` lo resuelve el propio BOE con palabras exactas, así que no encuentra la octava norma balear, cuyo título dice "viviendas".) `get_law_overview` de la Ley 5/2018 muestra dos fechas:

- `first_published`: boletín BOIB, número 78, 2018-06-26. Es donde se publicó por primera vez y determina la entrada en vigor (`effective_from` 2018-06-27).
- `published_on`: 2018-07-13, la fecha de su reproducción en el BOE. `enacted_on` es 2018-06-19.
- `status` `partially_repealed`; la afectan 20 normas posteriores (10 modificaciones, 3 derogaciones, entre otras).

### 6. Comprobar citas

*"Comprueba estas citas de un informe."*

```text
read_article(law_id="BOE-A-2015-10566", article="artículo 48")                      -> texto vigente
read_article(law_id="BOE-A-2015-10566", article="artículo 48", as_of="2017-01-01")  -> la redacción de 2016
verify_citation(citations=[cinco elementos: law, article y, en cuatro de ellos, una cita textual])
```

Las cinco citas se comprobaron en una sola llamada (`checked` 5, `not_checked` 0, ejecutada el 2026-11-22):

| Cita | `status` | Qué dice el resultado |
|---|---|---|
| Ley 40/2015, art. 48, la redacción vigente del plazo de inscripción ("resultarán eficaces una vez inscritos, en el plazo de 5 días hábiles desde su formalización, en el Registro Electrónico …") | `matches` | `quote_match.level` `exact`. |
| La misma frase con la redacción de 2016 ("… una vez inscritos en el Registro Electrónico …", sin los 5 días) | `matches_older_version` | `other_version`: coincidencia `exact` con una versión vigente del 2016-10-02 al 2020-12-31. |
| La redacción vigente con "5 días" cambiado por "15 días" | `text_differs` | `level` `close`, `similarity` 0.97, `flags` `["number"]`, `differences`: "… en el plazo de [-15-] {+5+} días hábiles …". |
| "Las aportaciones financieras que se comprometan a realizar los firmantes (...) podrán ser superiores a los gastos derivados de la ejecución del convenio." | `matches` | `level` `exact`, pero `omitted` es `["no"]` y `flags` es `["omitted_negation"]`. |
| Ley 30/1992, art. 1, sin cita | `not_in_force` | `state` `repealed`; nota: "The whole law was repealed on 2016-10-02, so this article is no longer in force; the text shown is its last consolidated version." |

La cuarta fila explica para qué existe `omitted`. Todas las palabras de la cita están en el artículo, así que coincide, pero la cita se salta un "no": el artículo 48.6 dice que las aportaciones financieras "no podrán ser superiores a los gastos", y la cita se lee como si pudieran serlo. La herramienta compara palabras, no significado, y comunica hechos, no corrección jurídica.

### 7. Seguir las modificaciones

*"¿Qué modificó el Real Decreto-ley 8/2020 y se ha modificado desde entonces?"*

```text
explore_amendment_chain(law_id="BOE-A-2020-3824", direction="amends", depth=1)
explore_amendment_chain(law_id="BOE-A-2020-3824", direction="amended_by", depth=2)
```

- `amends`: el real decreto-ley actúa sobre 9 normas (`levels` [1, 9]): 6 modificaciones, 2 suspensiones y 1 derogación. Aristas, con la redacción del propio BOE en `detail`: `modifies` Real Decreto-ley 7/2020, "el art. 16 del Real Decreto-ley 7/2020, de 12 de marzo"; `suspends` `BOE-A-2015-2495`, "en la forma y con los efectos indicados la vigencia de los arts. 3.5 y 6 de la Orden IET/389/2015, de 5 de marzo".
- `amended_by`, profundidad 2: 72 normas (`levels` [1, 23, 48]) y 142 aristas, 117 `modifies` y 25 `repeals` (las derogaciones incluyen las parciales), con las 24 normas examinadas (`complete` true). Dos aristas que tocan la norma de partida: `repeals`, Ley 2/2021, "el art. 42, SE MODIFICA el art. 40.1 y 2, Ley 2/2021, de 29 de marzo"; `modifies`, `BOE-A-2023-6967`, "el art. 17.9, por Real Decreto-ley 2/2023, de 16 de marzo".
- `court_rulings_excluded` es 3: tres resoluciones judiciales sobre este real decreto-ley quedan fuera por defecto, porque solo se siguen las modificaciones; `include="all"` las lista.
- Cautela: las 48 normas del nivel 2 modificaron una de las 23 normas del nivel 1, no necesariamente las partes que afectan al Real Decreto-ley 8/2020. Por defecto no se muestra su `detail`.

### 8. El diario oficial día a día

*"¿Qué publicó el BOE el 14 de marzo de 2020?"*

```text
get_daily_summary(date="2020-03-14", limit=5)
```

- Dos números ese día: el 67 (`BOE-S-2020-67`) y el 66. 419 elementos en total: sección 1 (disposiciones generales) 5, 2A 4, 2B 7, 3 (otras disposiciones) 41, 4 (Administración de Justicia) 280, 5A 22, 5B 52, 5C 8.
- El primer elemento está en el número 67, el extraordinario: `BOE-A-2020-3692`, "Real Decreto 463/2020, de 14 de marzo, por el que se declara el estado de alarma para la gestión de la situación de crisis sanitaria ocasionada por el COVID-19."

*"¿Hay algo sobre vivienda en el BOE esta semana?"*

```text
scan_gazette(words="vivienda")      -> rango por defecto: los últimos 7 días, del 2026-11-16 al 2026-11-22
```

- 7 días examinados, 6 con número (el 22 es domingo y no hay): coinciden 11 títulos: 2026-11-16 1, 11-17 2, 11-18 1, 11-19 2, 11-20 4, 11-21 1.
- 2026-11-21, `BOE-A-2026-24487`: "Resolución de 10 de noviembre de 2026, de la Dirección General de Vivienda y Suelo, por la que se publica el Convenio con la Comunidad Autónoma de Castilla-La Mancha, …"
- 2026-11-19, `BOE-B-2026-38005`: "Anuncio del concurso de proyectos de: Secretaría General de Agenda Urbana, Vivienda y Arquitectura. …"
- 2026-11-20, `BOE-B-2026-38218`: "Resolución del Departamento de Territorio, Vivienda y Transición Ecológica TER/4133/2026, por la que se otorga …"

### 9. Seguir un tema

*"Sígueme lo que salga sobre vivienda en el BOE y dime qué hay de nuevo."* y después *"Avísame también si cambia la Ley 40/2015."* (véase [Cómo funcionan las búsquedas guardadas](#cómo-funcionan-las-búsquedas-guardadas))

```text
save_search(words="vivienda")                                -> guardada como "vivienda"
list_saved_searches()
check_saved_searches(since="2026-11-15", limit=3)
save_search(law_id="BOE-A-2015-10566")                       -> guardada como "ley-40-2015"
check_saved_searches(name="ley-40-2015")
```

Ejecutado un domingo, 2026-11-22, con el almacén vacío. Guardar una búsqueda del diario lee el número de hoy una vez (un domingo: todavía no hay) y anota "revisada hasta ayer" (`checked_through` 2026-11-21); la nota dice "The first check reports what is published from now on." Así, la primera comprobación de abajo, justo después de guardar, no tiene novedades: leyó solo hoy (`matched` 0, con la nota de que hoy aún no hay número). Para mirar la última semana, la siguiente comprobación pasa `since` siete días atrás (2026-11-15). `since` vuelve a leer sin marcar nada como visto, de modo que nada guardado cambió (`last_checked_at` sigue en `null`).

- `list_saved_searches` devuelve una búsqueda: `name` "vivienda", `kind` `gazette`, `words` "vivienda", `checked_through` 2026-11-21, `total` 1 de un `limit` de 20.
- La comprobación con `since` leyó del 2026-11-15 al 2026-11-22 (`days_pending` 0; el día 22 no tenía número, y el resultado lo indica) y encontró 11 coincidencias (`matched`), las mismas que el `scan_gazette` del ejemplo 8. Los elementos salen del día más antiguo al más reciente: con `limit=3` muestra los 3 primeros (`has_more` true) y dice "Showing 3 of 11; scan_gazette with the same filters lists them all (at most 31 days per call)." El más antiguo, del 2026-11-16, `BOE-B-2026-37684`: "Anuncio de la Demarcación de Costas en Murcia sobre solicitud de prórroga de la concesión administrativa de ocupación de dominio público marítimo-terrestre con destino a vivienda …" Sin `since`, una comprobación muestra lo nuevo y lo recuerda, de modo que volver a preguntar no lo repite.
- Guardar una búsqueda de norma lee la norma una vez y recuerda las modificaciones y resoluciones judiciales que ya tiene, para que no cuenten como novedad; la nota dice "The first check reports changes recorded from now on (the BOE data read can be up to 6 hours old)." El nombre se propuso a partir del rango y el número: `ley-40-2015` (`name_was_proposed` true, `total` 2).
- Su comprobación es el honesto "nada nuevo": `status` `checked`, `changes` vacío, `changes_total` 0. La misma comprobación con `since="2026-08-25"` no lee la norma y dice "since applies to gazette searches; get_amendment_history lists a law's past amendments."

### Conviene saber

- **Los filtros de título coinciden con palabras completas y la formulación importa.** Las herramientas de tendencias y del diario oficial aceptan singular y plural ("vivienda" encuentra también "viviendas"); la búsqueda del propio BOE (los filtros `title` y `text` de `search_legislation`) coincide con palabras exactas. En un título, "fumar" no encuentra nada desde 2020, mientras que "tabaco" encuentra 8 (verificado el 2026-11-21). Un filtro de texto o de materia que coincida con más de 5.000 normas se rechaza por demasiado amplio.
- **El diario oficial es más amplio que la colección consolidada.** `get_daily_summary` y `scan_gazette` listan todo lo que imprime el BOE, y la mayoría de esos identificadores (nombramientos, anuncios, muchas resoluciones) no están en la colección consolidada, así que las herramientas de normas y artículos no los encontrarán.
- **La comprobación de citas compara palabras, no significado.** Una paráfrasis no coincide, y una cita que se salta una palabra con "(...)" coincide, pero `omitted` y `flags` dicen qué se saltó. El resultado son hechos sobre la redacción, no una opinión sobre la corrección jurídica.
- **La cobertura es la colección de legislación consolidada del BOE**, no todo lo publicado en el BOE, y contiene solo una parte de las normas de cada año, una parte variable (más escasa antes de 2010 aproximadamente y en el último año o dos). Un cambio en un recuento no es por sí mismo un cambio en cuánto se legisló. Al contar un rango (Ley con `scope="state"`, Ley Orgánica, Real Decreto-ley o Real Decreto Legislativo) por años, `legislation_over_time` añade `highest_number` para que puedas valorar la cobertura. Los recuentos lo indican en sus `caveats`.
- **La cobertura autonómica son leyes y decretos-leyes con rango de ley**, no decretos ni órdenes autonómicos, que están en el diario oficial de cada comunidad.
- **Las leyes autonómicas se fechan por su reproducción en el BOE** (que puede llegar semanas o años después de la publicación autonómica), salvo que pases `date_basis="enacted"`; con `enacted` los últimos tres meses están incompletos, porque las normas se publican semanas después de aprobarse.
- **Las búsquedas guardadas no avisan a nadie.** El servidor solo responde cuando se le pregunta y no puede enviar mensajes ni ejecutarse por su cuenta. Consulta [Cómo funcionan las búsquedas guardadas](#cómo-funcionan-las-búsquedas-guardadas) para saber qué cubre una comprobación y dónde se guardan los datos. Algunos límites: un elemento se entrega una sola vez (`since` lo vuelve a leer si un resultado no llegó a ti); una referencia de una norma cuya redacción cambie el BOE después se notifica de nuevo entera; `section` y `department_code` no se comprueban contra las tablas del BOE, así que un código erróneo simplemente no encuentra nada; y los mismos filtros se pueden guardar dos veces.
- **Los textos consolidados son informativos y carecen de valor jurídico oficial.** Consulta el [aviso legal](#fuente-de-los-datos-atribución-y-aviso-legal).

## Herramientas

### Localizar normas

| Herramienta | Pregunta a la que responde | Parámetros principales |
|---|---|---|
| `resolve_reference` | ¿Cuál es el identificador del BOE de la "Ley 40/2015"? | `reference` (una denominación como `Ley Orgánica 3/2018` o `RDL 6/2019`, un identificador o parte del título) |
| `search_legislation` | ¿Qué normas tratan de X o se han publicado recientemente? | `title`, `text`, `rank`, `department_code`, `scope`, `subject_code`, `published_from`, `published_to`, `sort_by`, `descending`, `limit` (1-50), `offset` |
| `get_law_overview` | ¿Está vigente esta norma y cuánto ha cambiado? | `law_id` (p. ej. `BOE-A-2015-10566`) |
| `find_codes` | ¿Cuál es el código de este departamento, materia o rango (para los filtros de `search_legislation`)? | `kind` (`department`, `subject` o `rank`), `text`, `limit` (1-50) |

### Leer artículos y seguir sus cambios

| Herramienta | Pregunta a la que responde | Parámetros principales |
|---|---|---|
| `get_law_structure` | ¿Cuál es el índice de esta norma y qué artículo trata de un asunto? | `law_id`, `as_of`, `detail` (`headings` o `all`), `offset`, `limit` |
| `read_article` | ¿Qué dice este artículo, hoy o en una fecha pasada? | `law_id`, `article` o `block_id`, `as_of`, `max_chars` (500-20000), `offset` |
| `compare_article_versions` | ¿Qué cambió en este artículo entre dos fechas? | `law_id`, `article` o `block_id`, `from_date`, `to_date`, `max_changes` (1-200), `max_chars` (1000-30000) |
| `get_article_history` | ¿Cuándo cambió este artículo y qué norma lo modificó? | `law_id`, `article` o `block_id` |
| `get_amendment_history` | ¿Qué normas han afectado a esta norma, por tipo y por año? | `law_id`, `group` (`all`, `repeals`, `modifies`, `adds`, `corrections` u `other`), `limit` (1-100), `offset` |
| `explore_amendment_chain` | ¿Qué normas modificó esta norma y qué normas la modificaron a ella, hasta tres niveles? | `law_id`, `direction` (`amended_by` o `amends`), `depth` (1-3), `include` (`amendments` o `all`), `details` (`root`, `all` o `none`), `max_laws` (1-50) |

### Comprobar citas

| Herramienta | Pregunta a la que responde | Parámetros principales |
|---|---|---|
| `verify_citation` | ¿Existen estas citas (norma, artículo, palabras citadas), están vigentes y las palabras coinciden con el texto, con la versión vigente o con una anterior? | `citations` (de 1 a 10, cada una con `law`, `article` o `block_id`, `quote` opcional de como máximo 5.000 caracteres y `as_of` opcional) |

### El diario oficial día a día

Estas dos herramientas cubren todo lo que imprime el BOE (nombramientos, subvenciones, anuncios, cualquier disposición) desde el 1960-09-01, a diferencia de la colección consolidada que usan las demás.

| Herramienta | Pregunta a la que responde | Parámetros principales |
|---|---|---|
| `get_daily_summary` | ¿Qué publicó el BOE este día? | `date` (por defecto, el último número), `section`, `department_code`, `words`, `offset`, `limit` (1-100) |
| `scan_gazette` | ¿Qué elementos de los últimos días tienen estas palabras en el título? | `words`, `section`, `department_code`, `from_date` (por defecto, 6 días antes de `to_date`), `to_date` (por defecto, hoy), `offset`, `limit` (1-100); como máximo 31 días |

Las dieciocho primeras herramientas son de solo lectura; las cinco [herramientas de búsquedas guardadas](#cómo-funcionan-las-búsquedas-guardadas) son las únicas que cambian algo, y solo en el almacén del propio servidor, nunca en el BOE. `resolve_reference` devuelve `resolved`, `ambiguous` (con candidatas y una pista) o `not_found`. `search_legislation` pagina con `next_offset`, porque la API no ofrece el número total de resultados. `find_codes` devuelve los pares `{code, text}` que coinciden.

**Situación de vigencia de la norma.** `get_law_overview` devuelve un `status` (`in_force`, `partially_repealed`, `repealed`, `annulled`, `validity_exhausted` o `not_yet_in_force`) con una nota explicativa, la nota inicial del BOE si el texto la tiene, y recuentos y muestras de las normas que modifican la norma consultada o que esta modifica. El `status` se deriva de los indicadores del BOE y de las referencias de modificación, por lo que es una indicación, no una determinación jurídica. La derogación total también se detecta a partir de la nota inicial del texto consolidado ("Norma derogada..."), porque algunas normas derogadas por completo solo llevan esa nota y no el indicador de los metadatos.

**Estados de un artículo.** `read_article` indica el `state` del artículo en la fecha pedida y `get_article_history` su `current_state`: `in_force` (en vigor), `repealed` (derogado), `not_yet_in_force` (texto publicado, pero todavía sin vigencia) o `did_not_exist` (la fecha es anterior a la primera versión del artículo). Un artículo de una norma derogada por completo no lleva ninguna marca de derogación, así que las herramientas de artículos comprueban también la norma (sus metadatos y, si el texto la tiene, su nota inicial): desde la fecha de derogación el estado es `repealed` (se sigue devolviendo el último texto), `law_repealed` y `law_repealed_on` lo indican y `note` lo explica. Una derogación con fecha futura solo está anunciada, y si no se puede hacer la comprobación `law_repealed` es `null` y `note` lo dice.

**Cómo nombrar un artículo.** El argumento `article` (como máximo 200 caracteres) acepta las formas habituales: `46 bis`, `art. 12`, `art. 1.902`, `artículo 48`, `disposición adicional tercera` (o `DA 3`), `anexo I`, `preámbulo`. Lo que siga a la designación, como un número de apartado (`46.2`, `149.1.18ª`) o un contexto (`14 de la Constitución`), se ignora, porque las herramientas devuelven artículos completos; `note` indica qué se ignoró. Para elegir un bloque exacto, pasa su `block_id` de `candidates` en lugar de `article` (hace falta exactamente uno de los dos). Si la denominación coincide con varios bloques (`ambiguous`) o con ninguno (`not_found`), no es un error: `outcome` lo indica y `candidates` lista bloques para reintentar con su `block_id`.

### Tendencias y clasificaciones

| Herramienta | Pregunta a la que responde | Parámetros principales |
|---|---|---|
| `legislation_over_time` | ¿Cuántas normas sobre X hay por año, trimestre o mes? | `title`, `text`, `subject_code`, `rank`, `scope`, `department_code`, `from_date`, `to_date`, `period`, `date_basis`, `group_by` (`none`, `rank`, `department` o `scope`), `top_groups` |
| `top_departments` | ¿Qué departamentos o comunidades autónomas han dictado más normas sobre X? | los mismos filtros, más `limit` |
| `compare_topics` | ¿Cómo se comparan entre sí de 2 a 5 temas a lo largo del tiempo? | `topics` (cada uno con `name` y `title`, `text` o `subject_code`), `rank`, `scope`, `department_code`, `from_date`, `to_date`, `period`, `date_basis` |
| `explain_spike` | ¿Por qué este periodo tiene tantas normas? | los mismos filtros, más `period_key` (`2020`, `2020-Q2` o `2020-03`), `period`, `baseline_periods`, `max_laws` |
| `most_amended_laws` | ¿Qué normas sobre X han sido modificadas más veces por normas posteriores? | los mismos filtros, más `limit`, `max_laws` (normas examinadas por llamada) |

Las herramientas de tendencias cuentan sobre una copia local de toda la colección consolidada (véase [Cómo funciona](#cómo-funciona)); la primera tarda unos treinta segundos. `from_date` es por defecto el 1 de enero del año 19 años anterior a `to_date` (`most_amended_laws` no tiene límite inferior por defecto) y `to_date` hoy. Se rechaza un resultado de más de 1.500 puntos (series por periodos) con el consejo de usar un periodo más grueso, un rango más corto o menos grupos. `date_basis` es `published` (publicación en el BOE, por defecto) o `enacted`. `most_amended_laws` consulta los datos de modificaciones de una norma por petición, así que cada llamada examina como máximo `max_laws` normas: mientras `complete` sea false, repite la llamada con los mismos argumentos para continuar desde la caché.

Cada resultado incluye un bloque `provenance`. Salida abreviada de `compare_article_versions` para el artículo 48 de `BOE-A-2015-10566` entre el 2016-10-02 y el 2026-11-21 (fixtures de pruebas grabados, sin peticiones reales). Las cadenas largas (`inline`, `unified_diff`) se acortan aquí con `…`. `[-antes-] {+después+}` marca una reformulación dentro de un párrafo: un párrafo reformulado se devuelve solo como `inline` (`old_text` y `new_text` son `null`), salvo las tablas, que devuelven ambos textos y no `inline`. Los textos de las claves y de las notas se mantienen tal como los devuelve la herramienta:

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

### Búsquedas guardadas

| Herramienta | Pregunta a la que responde | Parámetros principales |
|---|---|---|
| `save_search` | Seguir este tema o esta norma. | `name` (opcional), `words`, `section`, `department_code` (búsqueda del diario) o `law_id` (búsqueda de una norma) |
| `list_saved_searches` | ¿Qué temas sigo y cuándo se revisó cada uno por última vez? | ninguno |
| `check_saved_searches` | ¿Hay algo nuevo en lo que sigo? | `name` (por defecto, todas), `since` (hasta 90 días atrás), `limit` (1-50, por defecto 20) |
| `edit_saved_search` | Cambiar el nombre de una búsqueda guardada o los filtros de una búsqueda del diario. | `name`, `new_name`, `words`, `section`, `department_code` (una cadena vacía quita el filtro) |
| `delete_saved_search` | Dejar de seguir esto. | `name` |

## Cómo funcionan las búsquedas guardadas

Una búsqueda guardada es una búsqueda almacenada por el servidor (en tu equipo cuando lo ejecutas tú) más un marcador de hasta dónde se revisó por última vez. Sirve para preguntar, una semana después, "¿hay algo nuevo?" sin repetir los filtros y sin ver dos veces lo mismo.

**El servidor no puede avisar a nadie.** Solo funciona mientras tu aplicación lo está usando y no tiene manera de enviar un mensaje. No ocurre nada hasta que preguntas ("¿hay algo nuevo?"); entonces el asistente llama a `check_saved_searches`. Una búsqueda que nunca revisas simplemente espera.

**Dos tipos de búsqueda.**

| Tipo | Tú indicas | Una comprobación informa de |
|---|---|---|
| Diario | `words` (palabras completas del título; el singular y el plural coinciden), `section`, `department_code`: al menos uno | Los elementos del diario publicados desde la última comprobación que cumplen los filtros, del día más antiguo al más reciente. |
| Norma | el `law_id` de una norma consolidada (usa `resolve_reference` para encontrarlo) | Nuevas modificaciones, adiciones, suspensiones, derogaciones, correcciones y resoluciones judiciales (sentencias y procedimientos del Tribunal Constitucional sobre la norma) de esa norma, con la redacción del propio BOE; el texto de la relación distingue una sentencia de un procedimiento que se abre. Las citas que mencionan la norma (y cualquier otro tipo de referencia) solo se cuentan (`other_new`: `citations`, `other`), no se listan; `explore_amendment_chain` con `include="all"` las muestra. |

Guardar una búsqueda del diario lee el número de hoy una vez y recuerda lo que ya contiene, de modo que la primera comprobación informa de lo que se publique a partir de ese momento (un elemento añadido más tarde al número de hoy cuenta como novedad). Guardar una búsqueda de norma lee la norma una vez y recuerda las modificaciones y resoluciones judiciales que ya tiene, de modo que solo las posteriores cuentan como novedad. Una búsqueda de norma no puede cambiar de norma: para seguir otra, guarda una búsqueda nueva.

**Nombres.** Puedes dar uno (`name`) o dejar que el servidor lo proponga a partir de los filtros, como `vivienda`, `seccion-1` o `ley-40-2015`. El nombre se guarda en minúsculas, con letras, cifras y guiones, de hasta 60 caracteres, así que "Ley 40/2015" pasa a ser `ley-40-2015`; si ya existe, se añade `-2`, `-3` y así sucesivamente. `edit_saved_search` cambia el nombre y, en una búsqueda del diario, también sus filtros, conservando el marcador.

**Qué cubre una comprobación.** La de una búsqueda del diario lee los días desde la última comprobación hasta hoy, como máximo los últimos 31 días; si se han quedado más días sin revisar, lo dice e indica cuántos se dejaron fuera. Muestra lo nuevo, del día más antiguo al más reciente, y recuerda lo que ha mostrado: volver a preguntar nunca repite un elemento, y uno añadido al número de hoy después de una comprobación lo muestra la siguiente. El número de hoy, y un día laborable reciente que respondió "sin número" (el BOE puede publicar tarde), se vuelven a leer en la siguiente comprobación. Una comprobación muestra como máximo `limit` elementos por búsqueda y 60 en total por llamada, atendiendo las búsquedas en el orden de tu lista; si queda más, `complete` es false y la siguiente llamada trae el resto. Una búsqueda de norma informa de las referencias nuevas del mismo modo. Pasa `since` para volver a leer las búsquedas del diario desde una fecha anterior, hasta 90 días atrás: no se marca nada como visto, así que sirve para mirar atrás, para probar una búsqueda guardada hoy o para recuperar un resultado que no llegó a ti (la entrega es como máximo una vez: un elemento mostrado al asistente no se vuelve a mostrar, aunque la aplicación haya perdido la respuesta). `since` no se aplica a las búsquedas de norma; `get_amendment_history` lista las modificaciones pasadas de una norma. Una comprobación tiene un presupuesto de unos 40 segundos: si `complete` es false, vuelve a pedirla y continúa donde se quedó. Los días que el BOE no pudo servir se reintentan en cada comprobación durante una semana antes de omitirse, y el resultado lo indica. La mayoría de los domingos no hay número, y una búsqueda que no se pudo leer (una norma que el BOE ya no tiene) lo dice en su propia nota sin mantener la llamada abierta.

**Límite.** 20 búsquedas guardadas. Guardar la 21.ª falla con un mensaje que pide borrar una antes.

**Dónde se guardan los datos.** En un pequeño archivo SQLite, `saved_searches.sqlite3`, dentro del directorio de datos de tu usuario (en macOS `~/Library/Application Support/boe-insights-mcp/`, en Linux `~/.local/share/boe-insights-mcp/`). El archivo es privado para tu usuario, se queda en tu equipo y no se envía a ninguna parte; una comprobación solo envía las peticiones al BOE que necesita. Para llevarte tus búsquedas a otro equipo, copia el archivo a la misma ubicación allí, o define `BOE_INSIGHTS_DATA_DIR` ([Configuración](#configuración)). Para borrarlo todo, borra el archivo. Es independiente de la caché de respuestas, que se puede borrar en cualquier momento: vaciar la caché nunca hace perder una búsqueda guardada. Con `BOE_INSIGHTS_CACHE=memory` y sin `BOE_INSIGHTS_DATA_DIR`, las búsquedas solo se guardan en memoria y se pierden al detener el servidor.

En un servidor compartido por HTTP con inicio de sesión de GitHub, el archivo está en la máquina del servidor y cada búsqueda pertenece a la cuenta que la guardó (se almacena como `github:<id>`; en local, el propietario es `local`): nadie más puede verla, revisarla, editarla ni borrarla, y el límite de 20 búsquedas se aplica por cuenta. Sin inicio de sesión, un servidor abierto por HTTP no ofrece búsquedas guardadas.

**Son las únicas herramientas que cambian algo**, y solo en ese archivo. `edit_saved_search` y `delete_saved_search` están marcadas como destructivas, así que algunas aplicaciones piden confirmación antes de ejecutarlas; una búsqueda borrada no se puede recuperar. `check_saved_searches` también cambia algo (anota lo que ha mostrado), salvo que pases `since`.

Frases habituales y qué ocurre:

| Tú dices | Qué ocurre |
|---|---|
| "Sígueme las ayudas a la vivienda en el BOE" | `save_search` con `words`, y se te comunica el nombre elegido. |
| "¿Qué temas estamos siguiendo?" | `list_saved_searches`. |
| "¿Hay algo nuevo?" | `check_saved_searches` para todas. |
| "¿Hay algo nuevo sobre vivienda?" | `check_saved_searches` con ese `name`. |
| "¿Qué encontró la búsqueda de vivienda en el último mes?" | `check_saved_searches` con `since`, sin marcar nada como visto. |
| "Avísame también si cambia la Ley 40/2015" | `resolve_reference` y después `save_search` con su `law_id`. |
| "Cambia el nombre de la búsqueda de vivienda a ayudas" | `edit_saved_search` con `new_name`. |
| "A partir de ahora, solo la sección 3" | `edit_saved_search` con `section`. |
| "Deja de seguir lo de vivienda" | `delete_saved_search`. |

## Recursos y prompts

Ambos son extras opcionales que reflejan las herramientas: no ofrecen nada que estas no puedan hacer. La compatibilidad depende de la aplicación, y muchas permiten a la persona elegir un recurso o un prompt pero no dejan que el modelo abra uno por sí mismo.

**Los recursos** son elementos que una persona adjunta a la conversación; cada uno devuelve exactamente lo que devuelve la herramienta equivalente (en JSON, con la atribución y el aviso informativo).

| URI | Qué devuelve | Equivale a |
|---|---|---|
| `boe://law/{law_id}` | Resumen de una norma: vigencia, fechas, departamento, recuento de modificaciones. | `get_law_overview` |
| `boe://law/{law_id}/article/{block_id}` | Texto vigente de un artículo, por su `block_id`. | `read_article` |
| `boe://gazette/{day}` | Lo que publicó el BOE un día (`AAAA-MM-DD`), con hasta 100 elementos. | `get_daily_summary` |
| `boe://codes/{kind}` | La tabla completa de departamentos (`department`) o de rangos (`rank`). La de materias es demasiado grande; usa `find_codes`. | `find_codes` |
| `boe://saved-searches` | Tus búsquedas guardadas y cuándo se revisó cada una. | `list_saved_searches` |

En Claude Code se adjunta un recurso escribiendo `@` y eligiéndolo, por ejemplo `@boe-insights:boe://law/BOE-A-2015-10566`.

**Los prompts** son peticiones ya preparadas que indican al asistente qué herramientas llamar y cómo presentar el resultado. Cada uno existe en español y en inglés, con los mismos pasos; los argumentos se llaman en el idioma del prompt.

| Español | English | Argumentos | Qué pide |
|---|---|---|---|
| `explicar_ley` | `explain_law` | `ley` / `law` | Qué regula una norma, su estado, su estructura y cuántas veces se ha modificado. |
| `cambios_articulo` | `article_changes` | `ley`, `articulo`, `desde` (opcional) / `law`, `article`, `since` (optional) | Cómo ha cambiado un artículo y qué normas lo modificaron. |
| `comprobar_citas` | `check_citations` | `texto` / `text` | Comprobar las citas de leyes de un texto con los textos consolidados. |
| `resumen_boe` | `gazette_briefing` | `tema`, `dias` (1-31, por defecto 7) / `topic`, `days` | Qué publicó el BOE sobre un tema en los últimos días. |

En Claude Code los prompts aparecen como comandos con barra, por ejemplo `/mcp__boe-insights__explicar_ley`.

## Inicio rápido

Requisitos previos: Python 3.11 o superior y [uv](https://docs.astral.sh/uv/). No hace falta clave de API: la API de datos abiertos del BOE es pública.

El servidor habla MCP por stdio, así que normalmente no lo arrancas tú: lo hace tu cliente MCP. El comando que ejecuta es:

```bash
uvx boe-insights-mcp
```

Eso funcionará en cuanto la versión 0.1.0 esté en PyPI. Mientras tanto, uv puede descargarlo y ejecutarlo directamente desde GitHub:

```bash
uvx --from git+https://github.com/davidbejar/boe-insights-mcp boe-insights-mcp
```

Por ejemplo, para Claude Code:

```bash
claude mcp add boe-insights -- uvx --from git+https://github.com/davidbejar/boe-insights-mcp boe-insights-mcp
```

Otras formas de ejecutarlo (sitio de documentación):

- [Con uvx](https://davidbejar.github.io/boe-insights-mcp/es/run/uvx/): sin clonar ni instalar.
- [Desde el código fuente](https://davidbejar.github.io/boe-insights-mcp/es/run/from-source/): con un clon del repositorio.
- [Por HTTP](https://davidbejar.github.io/boe-insights-mcp/es/run/http/): un servidor para varias personas o equipos.
- [Como contenedor](https://davidbejar.github.io/boe-insights-mcp/es/run/container/): la imagen de contenedor.
- [Con inicio de sesión de GitHub](https://davidbejar.github.io/boe-insights-mcp/es/run/github-login/): un servidor HTTP abierto solo a las cuentas de GitHub que indiques.
- Configuración del cliente: [Claude Code](https://davidbejar.github.io/boe-insights-mcp/es/run/clients/claude-code/), [Claude Desktop](https://davidbejar.github.io/boe-insights-mcp/es/run/clients/claude-desktop/), [Cursor](https://davidbejar.github.io/boe-insights-mcp/es/run/clients/cursor/), [VS Code](https://davidbejar.github.io/boe-insights-mcp/es/run/clients/vscode/) y [cualquier otro cliente](https://davidbejar.github.io/boe-insights-mcp/es/run/clients/generic/).
- [Operación y seguridad](https://davidbejar.github.io/boe-insights-mcp/es/operating/): límites, control de acceso y qué vigilar si lo alojas tú.
- [Referencia de herramientas](https://davidbejar.github.io/boe-insights-mcp/es/reference/tools/): todas las herramientas, con sus parámetros.

## Configuración

Variables de entorno opcionales. Las variables sin definir o vacías conservan su valor por defecto, y un valor no válido detiene el servidor al arrancar con un mensaje que nombra la variable. La [página de operación](https://davidbejar.github.io/boe-insights-mcp/es/operating/) explica cada una con detalle.

**General**

| Variable | Valor por defecto | Significado |
|---|---|---|
| `BOE_INSIGHTS_CACHE` | `disk` | Tipo de caché: `disk` (persistente) o `memory` (solo durante el proceso). |
| `BOE_INSIGHTS_CACHE_DIR` | directorio de caché de la plataforma | Dónde se guarda la caché en disco. |
| `BOE_INSIGHTS_DATA_DIR` | directorio de datos de usuario de la plataforma | Dónde se guardan las búsquedas guardadas (`saved_searches.sqlite3`); está separado de la caché a propósito, porque la caché se puede borrar y tus búsquedas guardadas no deberían perderse. |
| `BOE_INSIGHTS_MIN_INTERVAL` | `1.0` | Segundos mínimos entre peticiones a boe.es (número >= 0). |
| `BOE_INSIGHTS_MAX_CONCURRENCY` | `2` | Máximo de peticiones simultáneas a boe.es (entero >= 1). |

**HTTP** (solo con `--transport http`)

| Variable | Valor por defecto | Significado |
|---|---|---|
| `BOE_INSIGHTS_TRANSPORT` | `stdio` | `stdio` (tu aplicación arranca el servidor) o `http` (escucha en un puerto); la opción `--transport` lo sustituye. |
| `BOE_INSIGHTS_HOST` | `127.0.0.1` | Dirección en la que escucha; la opción `--host` lo sustituye. |
| `BOE_INSIGHTS_PORT` | `8000` | Puerto en el que escucha (1-65535); la opción `--port` lo sustituye. |
| `BOE_INSIGHTS_ALLOWED_HOSTS` | vacío | Nombres de host públicos aceptados en la cabecera `Host`, separados por comas; se suman a los de loopback, que siempre se aceptan. |
| `BOE_INSIGHTS_ALLOWED_ORIGINS` | vacío | Orígenes de navegador (`https://host[:puerto]`) aceptados, separados por comas y sin comodines. |
| `BOE_INSIGHTS_CALLS_PER_MINUTE` | `60` | Llamadas que puede hacer cada usuario por minuto; `0` desactiva el límite. |
| `BOE_INSIGHTS_CALLS_BURST` | `20` | Llamadas que puede hacer cada usuario de golpe antes de tener que esperar (entero >= 1). |

**Inicio de sesión con GitHub** (las cuatro primeras van juntas: o se definen todas o ninguna)

| Variable | Valor por defecto | Significado |
|---|---|---|
| `BOE_INSIGHTS_GITHUB_CLIENT_ID` | sin definir | Client ID de la aplicación OAuth de GitHub con la que se inicia sesión. |
| `BOE_INSIGHTS_GITHUB_CLIENT_SECRET` | sin definir | El client secret de esa aplicación. |
| `BOE_INSIGHTS_AUTH_SIGNING_KEY` | sin definir | Secreto para los tokens que emite el servidor; al menos 32 caracteres. |
| `BOE_INSIGHTS_BASE_URL` | sin definir | URL pública del servidor (`https://host[:puerto]`, sin ruta; `http` solo para loopback). |
| `BOE_INSIGHTS_ALLOWED_USERS` | vacío | Quién puede iniciar sesión: nombres de usuario de GitHub, `id:<número>` o `*` (todo el mundo), separados por comas. Vacío significa nadie. |
| `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` | vacío | URL de redirección exactas adicionales para clientes OAuth, además de las de loopback, que siempre se aceptan. |

Por HTTP y sin inicio de sesión no se ofrecen las herramientas de búsquedas guardadas, porque no hay nadie a quien asignárselas. Los ajustes de inicio de sesión se ignoran por stdio.

## Cómo funciona

```text
Herramientas MCP     server.py        capa fina: parámetros, descripciones, traducción de errores
   |
casos de uso         legislation.py   normas, búsqueda, modificaciones: funciones simples que devuelven modelos pydantic tipados
                     articles.py      texto, comparación e historial de artículos, sobre locator.py y versions.py
                     trends.py        recuentos y clasificaciones sobre la copia del catálogo (catalogue.py, analytics.py)
   |
cliente del BOE      client.py        llamadas a la API del BOE, reintentos; textparse.py analiza el XML del texto
   |
caché + limitador                     caché en disco o en memoria; intervalo mínimo y tope de concurrencia
   |
API de datos         https://www.boe.es/datosabiertos/
abiertos del BOE
```

Las herramientas de artículos añaden tres piezas: un analizador del XML de los bloques de texto del BOE (`textparse.py`), un localizador de artículos (`locator.py`) y la lógica de versiones (`versions.py`: selección, detección de derogación, comparación e historial). Las dos últimas son funciones puras, sin entrada/salida.

- **Las tendencias se calculan sobre una copia local.** La API no tiene un endpoint de agregados, así que `catalogue.py` descarga toda la colección (unas 12.500 normas) en 3 peticiones, la conserva un día y cuenta en local. `most_amended_laws` es la excepción: necesita los datos de modificaciones de cada norma, que cuestan una petición por norma, de ahí el presupuesto `max_laws` por llamada.
- **Caché primero.** Las respuestas se guardan en caché (las búsquedas 30 minutos; los metadatos de cada norma, su análisis de modificaciones y el índice del texto, 6 horas; el texto de los artículos, 6 horas, o 30 días cuando se conoce la fecha de actualización del índice), de modo que las preguntas repetidas no llegan al BOE.
- **Los artículos se localizan por su título.** Los identificadores de bloque del BOE son opacos (el artículo 46 bis de la Ley 40/2015 es `a4-2`), así que el servidor lee el índice del texto de la norma y compara la denominación con los títulos de los bloques en lugar de construir un identificador.
- **Las versiones se seleccionan por su fecha de inicio.** Cada versión de un artículo empieza en su fecha de vigencia (la de publicación si falta), y el texto vigente en una fecha es la última versión que empezó en esa fecha o antes.
- **Los artículos largos se devuelven por partes.** `read_article` devuelve como máximo `max_chars` caracteres, con `truncated` y `next_offset` para pedir el resto; `compare_article_versions` y `get_article_history` limitan su salida y lo indican con `changes_truncated`, `unified_truncated` y `truncated`.
- **La comprobación de citas compara palabras y versiones.** La cita y cada versión del artículo se reducen a palabras comparables (se ignoran mayúsculas, puntuación y comillas; se conservan los acentos); el mejor pasaje alineado se clasifica como `exact`, `close`, `partial` o `none`, y se comparan también las demás versiones del artículo, que es como se reconoce una redacción anterior.
- **Los sumarios diarios se leen como XML y los días pasados se guardan en caché.** El sumario del BOE de un día se descarga como XML; un día pasado no vuelve a cambiar, así que se conserva 30 días (el de hoy, una hora).
- **Respetuoso por defecto.** El BOE no publica ningún límite de peticiones, así que el servidor se impone el suyo: al menos un segundo entre peticiones y un máximo de dos a la vez.
- **Resultados tipados y estructurados.** Cada herramienta devuelve un modelo pydantic con esquema de salida declarado, no texto libre.
- **Errores accionables.** Los fallos explican qué ha ocurrido, si tiene sentido reintentar y qué hacer a continuación (por ejemplo, "usa `resolve_reference` para encontrar el identificador correcto").
- **Procedencia en todo resultado.** Cada resultado indica su fuente, la atribución exigida, cuándo se elaboró el resultado (`generated_at`; los datos pueden proceder de la caché), la fecha de última actualización de la API y el aviso de carácter meramente informativo.

## Fuente de los datos, atribución y aviso legal

Los datos proceden de la API de datos abiertos del BOE: <https://www.boe.es/datosabiertos/>.

Basado en datos de la Agencia Estatal Boletín Oficial del Estado ([www.boe.es](https://www.boe.es)).

- Los textos consolidados tienen carácter meramente informativo y carecen de valor jurídico oficial. Solo son auténticos los textos publicados en el diario oficial. Los sumarios del diario son un listado, y solo son auténticos los textos publicados en el propio BOE.
- Este proyecto es independiente. No está afiliado a la Agencia Estatal Boletín Oficial del Estado, ni patrocinado ni respaldado por ella.
- Nada de lo aquí expuesto constituye asesoramiento jurídico.

## Hoja de ruta

- [x] 1. Primera versión mínima: cliente, caché, limitador de peticiones y cuatro herramientas
- [x] 2. Lectura de artículos e historial de versiones: leer un artículo a una fecha dada, comparar versiones de un artículo, cronología de cambios, historial de modificaciones
- [x] 3. Análisis de tendencias: legislación a lo largo del tiempo, departamentos con más normas, comparar temas, explicar un pico, normas más modificadas, y el índice de una norma
- [x] 4. Verificación de citas, explorador de la cadena de modificaciones, rastreo del diario oficial
- [x] 5. Recursos y prompts MCP y búsquedas guardadas
- [x] 6. Modo remoto: Streamable HTTP, inicio de sesión con GitHub con lista de acceso, límite de llamadas por usuario, imagen de contenedor y sitio de documentación
- [ ] 7. Versión 0.1.0: publicación en PyPI, con la imagen de contenedor y el sitio de documentación
- [ ] Inclusión en el MCP Registry
- [ ] Batería de evaluación
- [ ] Pasar el sitio de documentación de Material for MkDocs a Zensical antes del fin de vida de Material, en mayo de 2027

## Desarrollo

```bash
uv sync --all-groups                         # instalar, incluidas las herramientas de desarrollo y de documentación
uv run pytest                                # pruebas; sin red
uv run pytest -m live                        # pruebas contra la API real del BOE
uv run ruff check . && uv run ruff format .  # análisis estático y formato
uv run pyright                               # comprobación de tipos
NO_MKDOCS_2_WARNING=1 uv run mkdocs serve    # el sitio de documentación, con recarga automática
NO_MKDOCS_2_WARNING=1 uv run mkdocs build --strict  # la misma compilación que ejecutan las comprobaciones
```

Las pruebas live consultan la API real del BOE: úsalas con moderación. La lógica se escribe primero con pruebas, y las pruebas unitarias nunca acceden a la red: usan [respx](https://lundberg.github.io/respx/) con respuestas reales de la API grabadas en `tests/fixtures/boe/`. El comportamiento verificado de la API está documentado en [`docs/api-notes.md`](docs/api-notes.md) (en inglés).

Las páginas de referencia del sitio se generan a partir del propio servidor. Sus resúmenes en español de las herramientas se escriben a mano en `docs_build/summaries.es.toml`; cuando cambia el texto en inglés de una herramienta, vuelve a leer su resumen en español y después actualiza su hash con `uv run python -m docs_build --refresh tools.NAME` (`--hash tools.NAME` solo muestra el actual).

## Licencia

MIT. Consulta [LICENSE](LICENSE).
