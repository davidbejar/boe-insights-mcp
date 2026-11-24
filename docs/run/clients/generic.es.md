# Cliente MCP genérico

La mayoría de los clientes MCP aceptan las mismas dos formas de configuración. Úsalas si tu cliente no tiene página propia, y consulta la documentación de tu cliente para saber el nombre del archivo y el de la clave raíz (a menudo es `mcpServers`).

!!! warning "No probado por este proyecto"
    Los fragmentos de aquí son la forma habitual. Se probaron Claude Code y Claude Desktop (stdio). En cualquier otro cliente, el formato exacto depende de ese cliente, así que lee su documentación.

## En local, por stdio

El cliente arranca el servidor como un programa y habla con él por entrada y salida estándar. Necesitas [uv](https://docs.astral.sh/uv/).

```json
{
  "mcpServers": {
    "boe-insights": {
      "command": "uvx",
      "args": ["boe-insights-mcp"]
    }
  }
}
```

Antes de la versión 0.1.0, usa la forma con el repositorio, que funciona hoy:

```json
{
  "mcpServers": {
    "boe-insights": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/davidbejar/boe-insights-mcp", "boe-insights-mcp"]
    }
  }
}
```

Desde un clon:

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

Los ajustes van en un objeto `env` junto a `args`:

```json
"env": { "BOE_INSIGHTS_DATA_DIR": "/path/to/data" }
```

Están en [Ejecutar con uvx](../uvx.md#ajustes). Si el cliente no encuentra `uvx`, indica su ruta completa (`which uvx`).

## Por HTTP

Arranca primero el servidor: [Ejecutar por HTTP](../http.md) o [Ejecutar en un contenedor](../container.md). La dirección que necesita un cliente es:

```text
http://localhost:8000/mcp
```

El transporte es "streamable HTTP", a veces llamado simplemente "HTTP". No elijas "SSE": el servidor no ofrece el antiguo transporte SSE. Muchos clientes toman la dirección en un campo llamado `url`, a veces con `"type": "http"`:

```json
{
  "mcpServers": {
    "boe-insights": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Algunos clientes no pueden hablar HTTP por sí mismos. Consulta la documentación del cliente o usa stdio en su lugar.

### Con inicio de sesión con GitHub

Si el servidor tiene activado el [inicio de sesión con GitHub](../github-login.md), el cliente debe admitir el inicio de sesión OAuth de MCP: encuentra los datos de inicio de sesión en las direcciones well-known del servidor, se registra y abre un navegador. Un cliente que no puede hacerlo recibe `401 Unauthorized` y no puede usar el servidor. Por defecto los clientes redirigen de vuelta a una dirección de loopback (`localhost`, `127.0.0.1` o `[::1]`), que el servidor acepta. Un cliente que use otra dirección de redirección necesita que figure en `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` en el servidor.

### Comprobar la dirección a mano

```bash
curl http://localhost:8000/health
```

Responde `{"status":"ok"}` cuando el servidor está en marcha. Si el cliente llega al servidor con un nombre distinto de `localhost`, añade ese nombre a `BOE_INSIGHTS_ALLOWED_HOSTS`, o el servidor rechazará la petición. Consulta [Ejecutar por HTTP](../http.md#hosts-y-orígenes-permitidos).

## Véase también

- [Claude Code](claude-code.md), [Claude Desktop](claude-desktop.md), [Cursor](cursor.md), [VS Code](vscode.md)
- [Seguridad y operación](../../operating.md)
