# boe-insights-mcp

[English](README.md) | **Español**

[![CI](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/davidbejar/boe-insights-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Servidor [MCP](https://modelcontextprotocol.io) (Model Context Protocol) que ofrece a asistentes de IA como Claude herramientas analíticas sobre los datos abiertos del BOE (Boletín Oficial del Estado). En lugar de una herramienta por cada endpoint de la API, las herramientas se diseñan en torno a las preguntas que la gente se hace de verdad: "¿sigue vigente esta ley?", "¿qué ha cambiado en este artículo?", "¿cómo ha evolucionado la legislación sobre X?".

**Estado:** desarrollo temprano. Hoy hay cuatro herramientas disponibles (localizar una norma, buscar, consultar su situación de vigencia y buscar códigos); todo lo demás está en la [hoja de ruta](#hoja-de-ruta). Todavía no está publicado en PyPI: se ejecuta desde el código fuente.

## Qué puedes preguntar

| Pregunta | Herramienta |
|---|---|
| "¿Cuál es el identificador del BOE del Real Decreto-ley 6/2019?" | `resolve_reference` |
| "¿Sigue vigente la Ley 40/2015 y cuánto se ha modificado?" | `resolve_reference` y después `get_law_overview` |
| "¿Fue derogada la Ley 30/1992 y en qué fecha?" | `resolve_reference` y después `get_law_overview` |
| "¿Qué leyes orgánicas sobre protección de datos hay en la legislación consolidada?" | `search_legislation` |
| "Dame las leyes estatales sobre energía publicadas en 2020." | `search_legislation` |
| "¿Cuáles son las últimas leyes publicadas?" | `search_legislation` |
| "¿Qué código de departamento tiene la Agencia Española de Protección de Datos?" | `find_codes` y después `search_legislation` |

## Herramientas

| Herramienta | Pregunta a la que responde | Parámetros principales |
|---|---|---|
| `resolve_reference` | ¿Cuál es el identificador del BOE de la "Ley 40/2015"? | `reference` (una denominación como `Ley Orgánica 3/2018` o `RDL 6/2019`, un identificador o parte del título) |
| `search_legislation` | ¿Qué normas tratan de X o se han publicado recientemente? | `title`, `text`, `rank`, `department_code`, `scope`, `subject_code`, `published_from`, `published_to`, `sort_by`, `descending`, `limit` (1-50), `offset` |
| `get_law_overview` | ¿Está vigente esta norma y cuánto ha cambiado? | `law_id` (p. ej. `BOE-A-2015-10566`) |
| `find_codes` | ¿Cuál es el código de este departamento, materia o rango (para los filtros de `search_legislation`)? | `kind` (`department`, `subject` o `rank`), `text`, `limit` (1-50) |

Las cuatro herramientas son de solo lectura. `resolve_reference` devuelve `resolved`, `ambiguous` (con candidatas y una pista) o `not_found`. `search_legislation` pagina con `next_offset`, porque la API no ofrece el número total de resultados. `get_law_overview` devuelve un `status` (`in_force`, `partially_repealed`, `repealed`, `annulled`, `validity_exhausted` o `not_yet_in_force`) derivado de los indicadores del BOE y de las referencias de modificación (por tanto, es una indicación, no una determinación jurídica), con una nota explicativa, además de recuentos y muestras de las normas que modifican la norma consultada o que esta modifica.

`find_codes` devuelve los pares `{code, text}` que coinciden. Cada resultado incluye un bloque `provenance`. Salida abreviada de `get_law_overview` para `BOE-A-2015-10566` (fixtures de pruebas grabados, sin peticiones reales). Los textos de las claves y de las notas se mantienen tal como los devuelve la herramienta, en inglés:

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
casos de uso         legislation.py   funciones simples que devuelven modelos pydantic tipados
   |
cliente del BOE      client.py        llamadas a la API del BOE, análisis de respuestas, reintentos
   |
caché + limitador                     caché en disco o en memoria; intervalo mínimo y tope de concurrencia
   |
API de datos         https://www.boe.es/datosabiertos/
abiertos del BOE
```

- **Caché primero.** Las respuestas se guardan en caché (las búsquedas 30 minutos; los metadatos de cada norma y su análisis de modificaciones, 6 horas), de modo que las preguntas repetidas no llegan al BOE.
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

- [x] 1. Primera versión mínima: cliente, caché, limitador de peticiones y tres herramientas
- [ ] 2. Lectura de artículos e historial de versiones: leer un artículo a una fecha dada, comparar versiones de un artículo, cronología de cambios, historial de modificaciones
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
