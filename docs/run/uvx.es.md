# Ejecutar con uvx

`uvx` ejecuta un programa de Python sin instalarlo en el sistema. La primera vez lo descarga a un entorno privado con caché y después reutiliza ese entorno. Es la forma más rápida de ejecutar `boe-insights-mcp`.

## Qué necesitas

- [uv](https://docs.astral.sh/uv/), que proporciona el comando `uvx`. Compruébalo con `uvx --version`.
- Python 3.11 o superior. uv encuentra uno en tu equipo o descarga uno por ti.
- No hace falta clave de API. La API de datos abiertos del BOE es pública.

## Ejecutarlo

```bash
uvx boe-insights-mcp
```

El servidor habla MCP por entrada y salida estándar (el transporte "stdio"). Eso significa que arranca y se queda esperando en silencio a que un cliente se comunique con él. Es normal ver una línea de arranque y ningún indicador de comandos. Pulsa Ctrl+C para detenerlo.

Normalmente no lo ejecutas a mano. Pones el mismo comando en la configuración de tu cliente MCP y es el cliente quien arranca el servidor. Consulta [Claude Code](clients/claude-code.md), [Claude Desktop](clients/claude-desktop.md), [Cursor](clients/cursor.md), [VS Code](clients/vscode.md) o [cualquier otro cliente](clients/generic.md).

Para ver las opciones:

```bash
uvx boe-insights-mcp --help
```

## Ejecutarlo desde el repositorio antes de la versión

!!! note "Estado de la versión"
    El paquete se publica en PyPI junto con la versión 0.1.0. Hasta entonces, `uvx boe-insights-mcp` no encuentra nada que ejecutar. Usa la forma con el repositorio de abajo, que funciona hoy:

```bash
uvx --from git+https://github.com/davidbejar/boe-insights-mcp boe-insights-mcp
```

En la configuración de un cliente, usa `"command": "uvx"` con estos argumentos:

```json
["--from", "git+https://github.com/davidbejar/boe-insights-mcp", "boe-insights-mcp"]
```

## Fijar una versión

Cuando salga la versión puedes pedir una versión exacta, para que un cliente no se encuentre con una nueva por sorpresa:

```bash
uvx boe-insights-mcp@0.1.0
```

## Ajustes

Los ajustes son variables de entorno que empiezan por `BOE_INSIGHTS_`. Todas son opcionales. Defínelas en el shell o en el bloque `env` de la configuración de tu cliente.

```bash
BOE_INSIGHTS_DATA_DIR=$HOME/boe-data uvx boe-insights-mcp
```

| Variable | Valor por defecto | Significado |
|---|---|---|
| `BOE_INSIGHTS_CACHE` | `disk` | `disk` conserva la caché de respuestas entre ejecuciones. `memory` la conserva solo mientras el servidor está en marcha. |
| `BOE_INSIGHTS_CACHE_DIR` | la carpeta de caché de tu plataforma | Dónde se guarda la caché en disco. Se puede borrar sin riesgo. |
| `BOE_INSIGHTS_DATA_DIR` | la carpeta de datos de usuario de tu plataforma | Dónde se guardan las búsquedas guardadas (`saved_searches.sqlite3`). No se debe borrar: contiene tus datos. |
| `BOE_INSIGHTS_MIN_INTERVAL` | `1.0` | Segundos mínimos entre peticiones a boe.es. |
| `BOE_INSIGHTS_MAX_CONCURRENCY` | `2` | Máximo de peticiones simultáneas a boe.es. |

Las variables sin definir o vacías conservan su valor por defecto. Un valor no válido detiene el servidor al arrancar con un mensaje que nombra la variable.

Con `BOE_INSIGHTS_CACHE=memory` y sin `BOE_INSIGHTS_DATA_DIR`, las búsquedas guardadas también se guardan en memoria y se pierden al detener el servidor.

Los ajustes de HTTP se describen en [Ejecutar por HTTP](http.md). No tienen efecto por stdio.

## Véase también

- [Ejecutar desde el código fuente](from-source.md), si quieres modificar el código.
- [Ejecutar por HTTP](http.md), para dar servicio a más de un cliente.
