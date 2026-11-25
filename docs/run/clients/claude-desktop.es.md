# Claude Desktop

Claude Desktop es la aplicación de escritorio de Anthropic. Puede ejecutar un servidor MCP local como un programa de tu equipo. Es la forma que ha probado este proyecto.

!!! success "Probado por este proyecto"
    Se probó el uso local por stdio con Claude Desktop.

!!! warning "Sin probar"
    No se probó conectar Claude Desktop al servidor por HTTP. Lee la sección de HTTP de abajo para ver qué dice la documentación de Anthropic y qué implica para este servidor.

## En local, por stdio

Necesitas tener instalado [uv](https://docs.astral.sh/uv/).

1. Abre los ajustes de Claude Desktop (en el menú Claude de la barra de menús, **Settings...**), después **Developer** y después **Edit Config**. Se abre `claude_desktop_config.json`, que está aquí:
    - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
    - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
    - Linux: `~/.config/Claude/claude_desktop_config.json`
2. Añade el servidor dentro de `mcpServers`:

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

3. Guarda el archivo, cierra Claude Desktop por completo y vuelve a abrirlo. Cerrar la ventana no basta.
4. En una conversación, abre el menú "Add files, connectors, and more" y después **Connectors**. `boe-insights` debería aparecer con sus herramientas.

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

Desde un clon, usa la ruta absoluta de la carpeta:

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

Si la aplicación dice que no encuentra `uvx` o `uv`, indica la ruta completa del comando, por ejemplo la que devuelve `which uvx`. Las aplicaciones que se lanzan desde el escritorio no siempre ven el mismo `PATH` que tu terminal.

Para pasar ajustes, añade un bloque `env` junto a `args`, por ejemplo `"env": { "BOE_INSIGHTS_DATA_DIR": "/path/to/data" }`. Los ajustes están en [Ejecutar con uvx](../uvx.md#ajustes).

### Si no aparece

- Comprueba que el JSON no tenga una coma o unas comillas que falten.
- Revisa los logs. En macOS están en `~/Library/Logs/Claude`. `mcp.log` recoge las conexiones y `mcp-server-boe-insights.log` contiene la salida del propio servidor.
- Ejecuta el mismo comando en un terminal para ver su error.

## Por HTTP

La documentación de Anthropic describe dos cosas distintas.

- `claude_desktop_config.json`, arriba, es para servidores locales que se ejecutan en tu equipo. Las páginas de Anthropic no dicen que acepte un servidor remoto por URL, así que no pongas en él una dirección `http://`.
- Los servidores remotos se añaden como conectores personalizados (custom connectors): **Customize**, después **Connectors** y después **Add custom connector** (en los planes Team los añaden los roles Owner y Primary Owner en los ajustes de la organización; en los planes Enterprise, las personas con un rol que incluya gestionar el acceso a las bibliotecas). Consulta [la página de ayuda de Anthropic](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

La misma página de ayuda dice que los conectores remotos se conectan desde la nube de Anthropic, no desde tu dispositivo, en todas las aplicaciones de Claude, Claude Desktop incluida. El servidor debe ser accesible desde internet público y debe usar el puerto HTTPS estándar, el 443. Consecuencias para este servidor:

- Un servidor en `localhost`, en una red privada o detrás de una VPN no es accesible de esa manera. Esto se deduce de la descripción de Anthropic. No se probó.
- Para usar un conector personalizado tendrías que publicar el servidor en una dirección HTTPS pública (por ejemplo, detrás de un proxy inverso). Lee antes [Seguridad y operación](../../operating.md). No se probó si el [inicio de sesión con GitHub](../github-login.md) del servidor funciona con el inicio de sesión de los conectores de Claude. La página de ayuda de Anthropic indica `https://claude.ai/api/mcp/auth_callback` como la dirección a la que vuelve el inicio de sesión de los conectores de Claude. Si Claude usa esa dirección con este servidor, debe figurar en `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` (consulta [URI de redirección](../github-login.md#uri-de-redirección)).

Si solo lo quieres en tu propio equipo, usa la configuración por stdio de arriba.

## Véase también

- [Cliente genérico](generic.md)
- [Ejecutar con uvx](../uvx.md)
