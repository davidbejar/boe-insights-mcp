# VS Code

Visual Studio Code puede usar servidores MCP a través de sus funciones de chat con IA.

!!! warning "No probado por este proyecto"
    Esta página se escribió a partir de la documentación actual de VS Code y no se probó en VS Code. Las páginas usadas son [Use MCP servers](https://code.visualstudio.com/docs/copilot/customization/mcp-servers) y la [referencia de configuración de MCP](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration). Esas páginas afirman que el `mcp.json` gestionado por el editor que se describe aquí pertenece a la configuración "Local", que está obsoleta y se eliminará en febrero de 2027, y que las configuraciones más nuevas usan otros archivos. Consulta la documentación para saber qué espera tu versión.

## Dónde va la configuración

Según la referencia de configuración, los archivos que gestiona el editor son:

- `.vscode/mcp.json` en el espacio de trabajo: solo este proyecto.
- `mcp.json` en tu perfil de usuario de VS Code: todos tus proyectos.

La clave raíz es `servers` (no `mcpServers`). También puedes ejecutar **MCP: Add Server** desde la Command Palette (paleta de comandos) y dejar que VS Code escriba la entrada por ti. Revisa el comando que genera antes de aceptarlo. VS Code te pide que confíes en un servidor antes de arrancarlo.

## En local, por stdio

Necesitas [uv](https://docs.astral.sh/uv/).

```json
{
  "servers": {
    "boe-insights": {
      "type": "stdio",
      "command": "uvx",
      "args": ["boe-insights-mcp"]
    }
  }
}
```

Antes de la versión 0.1.0, usa la forma con el repositorio, que funciona hoy:

```json
{
  "servers": {
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
  "servers": {
    "boe-insights": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

VS Code prueba primero el HTTP en streaming (streamable HTTP) y recurre al antiguo transporte SSE si no funciona. Este servidor habla streamable HTTP.

### Con inicio de sesión con GitHub

La referencia de configuración dice que, para un servidor HTTP, VS Code ejecuta por sí mismo el flujo OAuth y abre una ventana del navegador la primera vez que se conecta. Con el [inicio de sesión con GitHub](../github-login.md) de este servidor verías la página de consentimiento del servidor y, la primera vez, la página de autorización de GitHub. La referencia muestra un objeto `oauth` opcional con un `clientId` para los servidores cuyos clientes están preinscritos. Este servidor registra los clientes de forma dinámica, así que no deberías necesitarlo. Nada de esto se probó.

## Véase también

- [Cliente genérico](generic.md)
- [Seguridad y operación](../../operating.md)
