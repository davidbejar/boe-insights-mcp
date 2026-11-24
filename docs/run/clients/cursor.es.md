# Cursor

Cursor es un editor de código con un asistente de IA integrado. Puede ejecutar servidores MCP.

!!! warning "No probado por este proyecto"
    Esta página se escribió a partir de la documentación actual de Cursor, [Model Context Protocol (MCP)](https://cursor.com/docs/context/mcp), y no se probó en Cursor. Los nombres de archivos y de campos de abajo proceden de esa página. Si algo difiere en tu versión de Cursor, fíate de la documentación de Cursor.

## Dónde va la configuración

Cursor lee un archivo JSON llamado `mcp.json`:

- `.cursor/mcp.json` en la carpeta de un proyecto: el servidor está disponible solo en ese proyecto.
- `~/.cursor/mcp.json` en tu carpeta personal: el servidor está disponible en todos los proyectos.

La pantalla de ajustes de los servidores MCP está en **Customize**, en la barra lateral de Cursor, donde puedes activar, desactivar y quitar servidores.

## En local, por stdio

Necesitas [uv](https://docs.astral.sh/uv/).

```json
{
  "mcpServers": {
    "boe-insights": {
      "type": "stdio",
      "command": "uvx",
      "args": ["boe-insights-mcp"]
    }
  }
}
```

La página de Cursor indica que `type` es obligatorio para los servidores stdio, aunque algunos de sus propios ejemplos lo omiten. Conservarlo no hace daño.

Antes de la versión 0.1.0, usa la forma con el repositorio, que funciona hoy:

```json
{
  "mcpServers": {
    "boe-insights": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/davidbejar/boe-insights-mcp", "boe-insights-mcp"]
    }
  }
}
```

Desde un clon, usa `"command": "uv"` y `"args": ["--directory", "/absolute/path/to/boe-insights-mcp", "run", "boe-insights-mcp"]`.

Para pasar ajustes, añade un objeto `env` junto a `args`. Están en [Ejecutar con uvx](../uvx.md#ajustes).

## Por HTTP

Arranca primero el servidor: [Ejecutar por HTTP](../http.md) o [Ejecutar en un contenedor](../container.md). Después:

```json
{
  "mcpServers": {
    "boe-insights": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Para un servidor en otro equipo, usa su dirección pública, por ejemplo `https://boe.example.org/mcp`.

### Con inicio de sesión con GitHub

Si el servidor tiene activado el [inicio de sesión con GitHub](../github-login.md), Cursor inicia sesión por OAuth. Según la página de Cursor, descubre por sí mismo los datos de inicio de sesión del servidor y se registra como cliente público, así que no añades nada al JSON. Después Cursor abre un navegador, donde ves la página de consentimiento del servidor y, la primera vez, la página de autorización de GitHub.

La página de Cursor dice que la aplicación de escritorio usa la dirección de redirección `http://localhost:8787/callback`. Este servidor acepta por defecto las direcciones de redirección de loopback (`localhost`, `127.0.0.1`, `[::1]`, cualquier puerto) de los clientes, así que no debería hacer falta ningún ajuste adicional. No se verificó. Si Cursor es rechazado con un error de redirección, añade la dirección a `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` en el servidor.

Nadie ha probado si la versión web de Cursor (Cursor Agents) puede llegar al servidor. Su inicio de sesión usa una dirección de `cursor.com` y el servidor tendría que estar en una dirección HTTPS pública.

## Véase también

- [Cliente genérico](generic.md)
- [Seguridad y operación](../../operating.md)
