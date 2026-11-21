# boe-insights-mcp

[English](README.md) | **Español**

[![CI](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Servidor [MCP](https://modelcontextprotocol.io) (Model Context Protocol) que ofrece a asistentes de IA como Claude herramientas analíticas sobre los datos abiertos del BOE (Boletín Oficial del Estado). En lugar de una herramienta por cada endpoint de la API, las herramientas se diseñan en torno a las preguntas que la gente se hace de verdad: "¿sigue vigente esta ley?", "¿qué ha cambiado en este artículo?", "¿cómo ha evolucionado la legislación sobre X?".

**Estado:** desarrollo temprano. Hoy hay ocho herramientas disponibles (localizar una norma, buscar, consultar su situación de vigencia y buscar códigos; historial de modificaciones; y leer, comparar y consultar el historial de cada artículo); todo lo demás está en la [hoja de ruta](#hoja-de-ruta). Todavía no está publicado en PyPI: se ejecuta desde el código fuente.

## Qué puedes preguntar

| Pregunta | Herramienta |
|---|---|
| "¿Cuál es el identificador del BOE del Real Decreto-ley 6/2019?" | `resolve_reference` |
| "¿Sigue vigente la Ley 40/2015 y cuánto se ha modificado?" | `resolve_reference` y después `get_law_overview` |
| "¿Fue derogada la Ley 30/1992 y en qué fecha?" | `resolve_reference` y después `get_law_overview` |
| "¿Qué leyes orgánicas sobre protección de datos hay en la legislación consolidada?" | `search_legislation` |
| "¿Qué código de departamento tiene la Agencia Española de Protección de Datos?" | `find_codes` y después `search_legislation` |
| "¿Qué dice el artículo 46 bis de la Ley 40/2015?" | `resolve_reference` y después `read_article` |
| "¿Qué decía el artículo 48 de la Ley 40/2015 el 1 de enero de 2017?" | `read_article` con `as_of` |
| "¿Qué cambió en el artículo 48 de la Ley 40/2015 entre 2016 y hoy?" | `compare_article_versions` |
| "¿Cuándo se modificó por última vez el artículo 48 de la Ley 40/2015 y qué norma lo hizo?" | `get_article_history` |
| "¿Qué normas han modificado la Ley 40/2015 y en qué años?" | `get_amendment_history` |

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
| `read_article` | ¿Qué dice este artículo, hoy o en una fecha pasada? | `law_id`, `article`, `as_of`, `max_chars` (500-20000), `offset` |
| `compare_article_versions` | ¿Qué cambió en este artículo entre dos fechas? | `law_id`, `article`, `from_date`, `to_date`, `max_changes` (1-200) |
| `get_article_history` | ¿Cuándo cambió este artículo y qué norma lo modificó? | `law_id`, `article` |
| `get_amendment_history` | ¿Qué normas han afectado a esta norma, por tipo y por año? | `law_id`, `group` (`all`, `repeals`, `modifies`, `adds`, `corrections` u `other`), `limit` (1-100), `offset` |

Las ocho herramientas son de solo lectura. `resolve_reference` devuelve `resolved`, `ambiguous` (con candidatas y una pista) o `not_found`. `search_legislation` pagina con `next_offset`, porque la API no ofrece el número total de resultados. `find_codes` devuelve los pares `{code, text}` que coinciden.

**Situación de vigencia de la norma.** `get_law_overview` devuelve un `status` (`in_force`, `partially_repealed`, `repealed`, `annulled`, `validity_exhausted` o `not_yet_in_force`) con una nota explicativa, la nota inicial del BOE si el texto la tiene, y recuentos y muestras de las normas que modifican la norma consultada o que esta modifica. El `status` se deriva de los indicadores del BOE y de las referencias de modificación, por lo que es una indicación, no una determinación jurídica. La derogación total también se detecta a partir de la nota inicial del texto consolidado ("Norma derogada..."), porque algunas normas derogadas por completo solo llevan esa nota y no el indicador de los metadatos.

**Estados de un artículo.** `read_article` indica el `state` del artículo en la fecha pedida y `get_article_history` su `current_state`: `in_force` (en vigor), `repealed` (derogado), `not_yet_in_force` (texto publicado, pero todavía sin vigencia) o `did_not_exist` (la fecha es anterior a la primera versión del artículo).

**Cómo nombrar un artículo.** El argumento `article` acepta las formas habituales: `46 bis`, `art. 12`, `artículo 48`, `disposición adicional tercera` (o `DA 3`), `anexo I`, `preámbulo`, o un `block_id` tomado de `candidates`. Si la denominación coincide con varios bloques (`ambiguous`) o con ninguno (`not_found`), no es un error: `outcome` lo indica y `candidates` lista bloques para reintentar con su `block_id`.

Cada resultado incluye un bloque `provenance`. Salida abreviada de `compare_article_versions` para el artículo 48 de `BOE-A-2015-10566` entre el 2016-10-02 y el 2026-11-21 (fixtures de pruebas grabados, sin peticiones reales). Además de los campos mostrados, el resultado real incluye el texto anterior y el nuevo de cada cambio, un `unified_diff` y los indicadores de truncamiento; `[-antes-] {+después+}` marca una reformulación dentro de un párrafo. Los textos de las claves y de las notas se mantienen tal como los devuelve la herramienta:

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
   |
cliente del BOE      client.py        llamadas a la API del BOE, reintentos; textparse.py analiza el XML del texto
   |
caché + limitador                     caché en disco o en memoria; intervalo mínimo y tope de concurrencia
   |
API de datos         https://www.boe.es/datosabiertos/
abiertos del BOE
```

Las herramientas de artículos añaden tres piezas: un analizador del XML de los bloques de texto del BOE (`textparse.py`), un localizador de artículos (`locator.py`) y la lógica de versiones (`versions.py`: selección, detección de derogación, comparación e historial). Las dos últimas son funciones puras, sin entrada/salida.

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
- [ ] 3. Análisis de tendencias: legislación a lo largo del tiempo, departamentos con más normas, comparar temas, explicar un pico, normas más modificadas
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
