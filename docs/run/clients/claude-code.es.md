# Claude Code

[Claude Code](https://code.claude.com/docs/en/mcp) es el asistente de programación de Anthropic para la línea de comandos. Esta página lo conecta a `boe-insights-mcp`, ya sea como programa local (stdio) o como servidor HTTP.

!!! success "Probado por este proyecto"
    Se probaron las dos formas con Claude Code: stdio, y HTTP con [inicio de sesión con GitHub](../github-login.md). El HTTP abierto sin inicio de sesión usa los mismos comandos de Claude Code y no se probó por separado en un cliente.

## En local, por stdio

Claude Code arranca el servidor por sí mismo. Necesitas [uv](https://docs.astral.sh/uv/).

```bash
claude mcp add boe-insights -- uvx boe-insights-mcp
```

Todo lo que va después de `--` es el comando que ejecuta Claude Code. `boe-insights` es el nombre que das al servidor.

Desde un clon, usa la ruta absoluta de la carpeta:

```bash
claude mcp add boe-insights -- uv --directory /absolute/path/to/boe-insights-mcp run boe-insights-mcp
```

Para pasar ajustes, añade `-e NOMBRE=valor` antes del nombre, por ejemplo `claude mcp add -e BOE_INSIGHTS_DATA_DIR=/path/to/data boe-insights -- uvx boe-insights-mcp`. Los ajustes están en [Ejecutar con uvx](../uvx.md#ajustes).

### Dónde se guarda

Por defecto el servidor se guarda solo para ti y para el proyecto actual (ámbito "local"). Añade `--scope user` para tenerlo en todos tus proyectos, o `--scope project` para escribir un archivo `.mcp.json` en la raíz del proyecto que puedes incluir en un commit y compartir:

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

Claude Code te pide que apruebes un servidor de ámbito de proyecto la primera vez.

## Por HTTP

Primero arranca el servidor: consulta [Ejecutar por HTTP](../http.md) o [Ejecutar en un contenedor](../container.md). Después añade su dirección:

```bash
claude mcp add --transport http boe-insights http://localhost:8000/mcp
```

Para un servidor en otro equipo, usa su dirección pública, por ejemplo `https://boe.example.org/mcp`.

Un servidor que se añade con Claude Code abierto aparece al reiniciar Claude Code.

### Con inicio de sesión con GitHub

Si el servidor tiene activado el [inicio de sesión con GitHub](../github-login.md), Claude Code debe iniciar sesión una vez:

1. En Claude Code, escribe `/mcp`.
2. Elige el servidor `boe-insights` y sigue los pasos para autenticarte. Se abre una ventana del navegador.
3. Verás la página de consentimiento del servidor y, solo la primera vez, la página de autorización de GitHub.
4. De vuelta en Claude Code, el servidor aparece como conectado y las herramientas de búsquedas guardadas están disponibles.

Si tu cuenta de GitHub no está en la lista de acceso del servidor, el navegador muestra una página "Access not allowed" con tu `id:<n>`. Envíaselo al administrador del servidor.

Claude Code guarda el token y lo renueva por sí mismo. Desde un shell, `claude mcp login boe-insights` y `claude mcp logout boe-insights` hacen lo mismo que el menú `/mcp`. Todavía no se ha observado la renovación después de que el token de GitHub caduque de verdad. Si Claude Code te pide que te autentiques de nuevo, hazlo con `/mcp`.

### Sin inicio de sesión

Por HTTP abierto no hay nada en lo que iniciar sesión. Las herramientas de búsquedas guardadas no se ofrecen. Consulta [Ejecutar por HTTP](../http.md#qué-hay-disponible-por-http-abierto).

## Comprobar que funciona

```bash
claude mcp list
```

Dentro de Claude Code, `/mcp` muestra cada servidor y su estado. Después pregunta algo que necesite los datos, por ejemplo "¿Qué dice el artículo 14 de la Constitución española?". Despliega la llamada a la herramienta para confirmar que la respuesta salió de una herramienta y no de la memoria del modelo.

## Quitarlo

```bash
claude mcp remove boe-insights
```

## Véase también

- [Cliente genérico](generic.md), para los ajustes que acepta cualquier cliente.
- [Seguridad y operación](../../operating.md).
