# Claude Desktop

Claude Desktop es la aplicación de escritorio de Anthropic. Puede ejecutar un servidor MCP local como un programa de tu equipo. Es la forma que ha probado este proyecto.

!!! success "Probado por este proyecto"
    Se probó el uso local por stdio con Claude Desktop en macOS el 2026-11-26, con el comando `uvx --from git+https://github.com/davidbejar/boe-insights-mcp boe-insights-mcp`, que ejecutaba el paquete directamente desde el repositorio antes de la versión 0.1.0. El servidor arrancó y se ejecutaron con éxito estas herramientas: resolver una referencia a una ley, leer un artículo, y guardar y listar una búsqueda guardada. No se registró la versión de Claude Desktop. Windows y Linux no se probaron; sus rutas de configuración de abajo vienen de la documentación de Anthropic.

!!! warning "Sin probar"
    No se probó conectar Claude Desktop al servidor por HTTP. Lee la sección de HTTP de abajo para ver qué dice la documentación de Anthropic y qué implica para este servidor.

## En local, por stdio

Necesitas tener instalado [uv](https://docs.astral.sh/uv/).

1. Abre `claude_desktop_config.json` directamente en un editor de texto. Está aquí:
    - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
    - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
    - Linux: `~/.config/Claude/claude_desktop_config.json`

    Haz una copia de seguridad del archivo antes de editarlo. Puede que ya exista con otros ajustes. No lo sustituyas: añade la clave `mcpServers` en el nivel superior, junto a las claves que ya hay. Las versiones antiguas de la aplicación ofrecían **Settings**, después **Developer** y después **Edit Config** para abrir este archivo; la versión actual no tiene esa entrada.
2. Añade el servidor dentro de `mcpServers`. Si el archivo ya tiene una clave `mcpServers`, añade solo la entrada `boe-insights` dentro de ella:

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
4. En la barra lateral izquierda, abre **Plugins** (en la interfaz en español, "Plugins"; la aplicación muestra un aviso de que los conectores y las skills ahora son plugins). `boe-insights` aparece en **Your plugins** ("Tus plugins" en español), con las etiquetas "Local development" y "Local MCP server" ("Desarrollo local" y "Servidor MCP local"). Abre la entrada para ver sus herramientas: 23 cuando se probó, divididas en herramientas de solo lectura y herramientas de escritura o borrado. El desarrollador aparece como desconocido, que es lo que muestra la aplicación para cualquier servidor añadido mediante el archivo de configuración.

El primer arranque puede tardar: uvx tiene que descargar antes el paquete (unos 16 segundos en la prueba, que lo construyó desde GitHub). Espera hasta un minuto. Si el servidor aparece como fallido, reinicia la aplicación una vez.

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
- Los servidores remotos se añaden como conectores personalizados (custom connectors): **Customize**, después **Connectors** y después **Add custom connector** (en los planes Team los añaden los roles Owner y Primary Owner en los ajustes de la organización; en los planes Enterprise, los roles Owner y Primary Owner y las personas con un rol personalizado que incluya gestionar el acceso a las bibliotecas). Consulta [la página de ayuda de Anthropic](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp), que seguía indicando estos pasos cuando se comprobó el 2026-11-26. En la aplicación probada ese día, los conectores aparecían en **Plugins**, en la barra lateral, con una pestaña **Conectores** y un botón **Añadir**; no se probó a añadir un conector personalizado desde ahí.

La misma página de ayuda dice que los conectores remotos se conectan desde la nube de Anthropic, no desde tu dispositivo, en todas las aplicaciones de Claude, Claude Desktop incluida. El servidor debe ser accesible desde internet público y debe usar el puerto HTTPS estándar, el 443. Consecuencias para este servidor:

- Un servidor en `localhost`, en una red privada o detrás de una VPN no es accesible de esa manera. Esto se deduce de la descripción de Anthropic. No se probó.
- Para usar un conector personalizado tendrías que publicar el servidor en una dirección HTTPS pública (por ejemplo, detrás de un proxy inverso). Lee antes [Seguridad y operación](../../operating.md). No se probó si el [inicio de sesión con GitHub](../github-login.md) del servidor funciona con el inicio de sesión de los conectores de Claude. La página de ayuda de Anthropic indica `https://claude.ai/api/mcp/auth_callback` como la dirección a la que vuelve el inicio de sesión de los conectores de Claude. Si Claude usa esa dirección con este servidor, debe figurar en `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` (consulta [URI de redirección](../github-login.md#uri-de-redirección)).

Si solo lo quieres en tu propio equipo, usa la configuración por stdio de arriba.

## Véase también

- [Cliente genérico](generic.md)
- [Ejecutar con uvx](../uvx.md)
