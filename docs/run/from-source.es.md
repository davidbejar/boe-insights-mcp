# Ejecutar desde el código fuente

Usa esta opción cuando quieras leer o modificar el código, o ejecutar la última versión sin publicar desde un clon del repositorio.

## Qué necesitas

- [uv](https://docs.astral.sh/uv/).
- Python 3.11 o superior. uv encuentra uno o descarga uno.
- Git.
- No hace falta clave de API.

## Obtenerlo e instalarlo

```bash
git clone https://github.com/davidbejar/boe-insights-mcp.git
cd boe-insights-mcp
uv sync
```

`uv sync` crea un entorno virtual en `.venv` e instala las dependencias fijadas, incluidas las herramientas de pruebas y de lint (el grupo `dev`). `uv sync --all-groups` instala además las herramientas que generan el sitio de documentación.

## Ejecutarlo

```bash
uv run boe-insights-mcp
```

Por defecto el servidor habla MCP por entrada y salida estándar (stdio). Arranca y espera en silencio a un cliente. Pulsa Ctrl+C para detenerlo.

Normalmente no lo ejecutas a mano: lo arranca tu cliente MCP. En la configuración de un cliente, indica la ruta absoluta de tu clon:

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

`--directory` hace que uv trabaje en tu clon desde cualquier carpeta en la que el cliente lo lance. En las páginas de cada cliente verás dónde va este JSON: [Claude Code](clients/claude-code.md), [Claude Desktop](clients/claude-desktop.md), [Cursor](clients/cursor.md), [VS Code](clients/vscode.md), [otros clientes](clients/generic.md).

## Ejecutarlo por HTTP

```bash
uv run boe-insights-mcp --transport http --port 8000
```

Por defecto escucha solo en `127.0.0.1`. Los detalles, y lo que debes hacer antes de exponerlo a otras personas, están en [Ejecutar por HTTP](http.md).

## Ajustes

Se aplican las mismas variables de entorno `BOE_INSIGHTS_*` que con [uvx](uvx.md#ajustes). Por ejemplo, para guardarlo todo en una carpeta desechable mientras experimentas:

```bash
BOE_INSIGHTS_DATA_DIR=/tmp/boe/data BOE_INSIGHTS_CACHE_DIR=/tmp/boe/cache uv run boe-insights-mcp
```

## Comprobar tus cambios

```bash
uv run pytest                              # pruebas; sin red
uv run ruff check . && uv run ruff format .
uv run pyright
```

`uv run pytest -m live` ejecuta pruebas contra la API real del BOE. Úsalo con moderación.
