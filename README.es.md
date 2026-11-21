# boe-insights-mcp

[English](README.md) | **Español**

[![CI](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Servidor [MCP](https://modelcontextprotocol.io) (Model Context Protocol) que ofrece a asistentes de IA como Claude herramientas analíticas sobre los datos abiertos del BOE (Boletín Oficial del Estado). En lugar de una herramienta por cada endpoint de la API, las herramientas se diseñan en torno a las preguntas que la gente se hace de verdad: "¿sigue vigente esta ley?", "¿qué ha cambiado en este artículo?", "¿cómo ha evolucionado la legislación sobre X?".

**Estado:** desarrollo temprano. Hoy hay catorce herramientas disponibles (localizar una norma, buscar, consultar su situación de vigencia y buscar códigos; historial de modificaciones; leer, comparar y consultar el historial de cada artículo; el índice de una norma; y análisis de tendencias sobre toda la colección consolidada); todo lo demás está en la [hoja de ruta](#hoja-de-ruta). Todavía no está publicado en PyPI: se ejecuta desde el código fuente.

## Qué puedes preguntar

Cinco ejemplos prácticos, cada uno a partir de una ejecución real del servidor el 2026-11-21. Primero la pregunta, después las llamadas a las herramientas en orden y un extracto del resultado (los títulos largos se acortan con "…").

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

Alrededor del 29 % de la colección (3.623 de 12.477 normas cuando se ejecutó) es autonómica. Para "vivienda", 39 normas desde 2015 repartidas entre 12 comunidades:

| Comunidad | Normas |
|---|---|
| Illes Balears | 7 |
| Región de Murcia | 5 |
| Canarias, Cataluña | 4 cada una |
| Andalucía, Extremadura, Navarra, Madrid | 3 cada una |
| Aragón, País Vasco | 2 cada una |

Las otras 2 comunidades (3 normas) se agrupan como "other". (El paso con `search_legislation` solo sirve para encontrar un identificador entre las normas de Baleares, que incluyen la Ley 5/2018, "de la vivienda de las Illes Balears".) `get_law_overview` de esa ley muestra dos fechas:

- `first_published`: boletín BOIB, número 78, 2018-06-26. Es donde se publicó por primera vez y determina la entrada en vigor (`effective_from` 2018-06-27).
- `published_on`: 2018-07-13, la fecha de su reproducción en el BOE. `enacted_on` es 2018-06-19.
- `status` `partially_repealed`; la afectan 20 normas posteriores (10 modificaciones, 3 derogaciones, entre otras).

También se probaron "cambio climático" (9 normas, 8 comunidades) y "turismo" (12 normas, 6 comunidades, 5 de ellas de Baleares); "vivienda" dio el reparto más amplio, así que es el que se muestra.

### Conviene saber

- **Las búsquedas coinciden con palabras exactas, así que la formulación importa.** En un título, "fumar" no encuentra nada desde 2020, mientras que "tabaco" encuentra 8 (verificado el 2026-11-21).
- **La cobertura es la colección de legislación consolidada del BOE**, no todo lo publicado en el BOE, y contiene solo una parte de las normas de cada año, una parte variable (más escasa antes de 2010 aproximadamente y en el último año o dos). Un cambio en un recuento no es por sí mismo un cambio en cuánto se legisló. Al contar un rango (Ley con `scope="state"`, Ley Orgánica, Real Decreto-ley o Real Decreto Legislativo) por años, `legislation_over_time` añade `highest_number` para que puedas valorar la cobertura. Los recuentos lo indican en sus `caveats`.
- **La cobertura autonómica son leyes y decretos-leyes con rango de ley**, no decretos ni órdenes autonómicos, que están en el diario oficial de cada comunidad.
- **Las leyes autonómicas se fechan por su reproducción en el BOE** (que puede llegar semanas o años después de la publicación autonómica), salvo que pases `date_basis="enacted"`; con `enacted` los últimos tres meses están incompletos, porque las normas se publican semanas después de aprobarse.
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

Las catorce herramientas son de solo lectura. `resolve_reference` devuelve `resolved`, `ambiguous` (con candidatas y una pista) o `not_found`. `search_legislation` pagina con `next_offset`, porque la API no ofrece el número total de resultados. `find_codes` devuelve los pares `{code, text}` que coinciden.

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

## Inicio rápido

Requisitos previos: Python 3.11 o superior y [uv](https://docs.astral.sh/uv/). No hace falta clave de API: la API de datos abiertos del BOE es pública.

Ejecución desde el código fuente:

```bash
git clone https://github.com/davidbejar/boe-insights-mcp.git
cd boe-insights-mcp
uv sync
```

El servidor habla MCP por stdio, así que normalmente no lo arrancas a mano: lo hace tu cliente MCP.

**Claude Desktop.** Añade esto a `claude_desktop_config.json` y reinicia la aplicación (usa la ruta absoluta de tu copia del repositorio):

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

O bien incluye la misma definición del servidor en un `.mcp.json` a nivel de proyecto:

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

**Sin clonar el repositorio**, uv puede descargarlo y ejecutarlo directamente desde GitHub:

```bash
uvx --from git+https://github.com/davidbejar/boe-insights-mcp boe-insights-mcp
```

En la configuración de un cliente, usa `"command": "uvx"` con `"args": ["--from", "git+https://github.com/davidbejar/boe-insights-mcp", "boe-insights-mcp"]`.

## Configuración

Variables de entorno opcionales. Las variables sin definir o vacías conservan su valor por defecto, y un valor no válido detiene el servidor al arrancar con un mensaje que nombra la variable.

| Variable | Valor por defecto | Significado |
|---|---|---|
| `BOE_INSIGHTS_CACHE` | `disk` | Tipo de caché: `disk` (persistente) o `memory` (solo durante el proceso). |
| `BOE_INSIGHTS_CACHE_DIR` | directorio de caché de la plataforma | Dónde se guarda la caché en disco. |
| `BOE_INSIGHTS_MIN_INTERVAL` | `1.0` | Segundos mínimos entre peticiones a boe.es (número >= 0). |
| `BOE_INSIGHTS_MAX_CONCURRENCY` | `2` | Máximo de peticiones simultáneas a boe.es (entero >= 1). |

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
- **Respetuoso por defecto.** El BOE no publica ningún límite de peticiones, así que el servidor se impone el suyo: al menos un segundo entre peticiones y un máximo de dos a la vez.
- **Resultados tipados y estructurados.** Cada herramienta devuelve un modelo pydantic con esquema de salida declarado, no texto libre.
- **Errores accionables.** Los fallos explican qué ha ocurrido, si tiene sentido reintentar y qué hacer a continuación (por ejemplo, "usa `resolve_reference` para encontrar el identificador correcto").
- **Procedencia en todo resultado.** Cada resultado indica su fuente, la atribución exigida, cuándo se elaboró el resultado (`generated_at`; los datos pueden proceder de la caché), la fecha de última actualización de la API y el aviso de carácter meramente informativo.

## Fuente de los datos, atribución y aviso legal

Los datos proceden de la API de datos abiertos del BOE: <https://www.boe.es/datosabiertos/>.

Basado en datos de la Agencia Estatal Boletín Oficial del Estado ([www.boe.es](https://www.boe.es)).

- Los textos consolidados tienen carácter meramente informativo y carecen de valor jurídico oficial. Solo son auténticos los textos publicados en el diario oficial.
- Este proyecto es independiente. No está afiliado a la Agencia Estatal Boletín Oficial del Estado, ni patrocinado ni respaldado por ella.
- Nada de lo aquí expuesto constituye asesoramiento jurídico.

## Hoja de ruta

- [x] 1. Primera versión mínima: cliente, caché, limitador de peticiones y cuatro herramientas
- [x] 2. Lectura de artículos e historial de versiones: leer un artículo a una fecha dada, comparar versiones de un artículo, cronología de cambios, historial de modificaciones
- [x] 3. Análisis de tendencias: legislación a lo largo del tiempo, departamentos con más normas, comparar temas, explicar un pico, normas más modificadas, y el índice de una norma
- [ ] 4. Verificación de citas, explorador de la cadena de modificaciones, rastreo del diario oficial
- [ ] 5. Recursos y prompts MCP y listas de seguimiento guardadas
- [ ] 6. Modo remoto: Streamable HTTP, OAuth con GitHub, contenedor Podman
- [ ] 7. Batería de evaluación, sitio de documentación y publicación en PyPI

## Desarrollo

```bash
uv sync --all-groups                         # instalar, incluidas las herramientas de desarrollo
uv run pytest                                # pruebas; sin red
uv run pytest -m live                        # pruebas contra la API real del BOE
uv run ruff check . && uv run ruff format .  # análisis estático y formato
uv run pyright                               # comprobación de tipos
```

Las pruebas live consultan la API real del BOE: úsalas con moderación. La lógica se escribe primero con pruebas, y las pruebas unitarias nunca acceden a la red: usan [respx](https://lundberg.github.io/respx/) con respuestas reales de la API grabadas en `tests/fixtures/boe/`. El comportamiento verificado de la API está documentado en [`docs/api-notes.md`](docs/api-notes.md) (en inglés).

## Licencia

MIT. Consulta [LICENSE](LICENSE).
