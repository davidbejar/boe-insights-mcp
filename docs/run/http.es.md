# Ejecutar por HTTP

Por defecto el servidor habla por stdio con un único cliente de tu propio equipo. Por HTTP escucha en un puerto de red, de modo que varios clientes pueden usarlo y también pueden llegar a él los clientes que no son capaces de arrancar un programa. Esta página usa el transporte MCP "streamable HTTP".

!!! warning "El HTTP abierto no tiene inicio de sesión"
    Sin el inicio de sesión con GitHub descrito en [Inicio de sesión con GitHub](github-login.md), cualquiera que pueda llegar al puerto puede usar el servidor. La dirección por defecto, `127.0.0.1`, solo es accesible desde tu propio equipo. Lee [Seguridad y operación](../operating.md) antes de asociar el servidor a cualquier otra dirección.

## Arrancarlo

```bash
uvx boe-insights-mcp --transport http
```

(Antes de la versión 0.1.0, usa `uvx --from git+https://github.com/davidbejar/boe-insights-mcp boe-insights-mcp --transport http`. Desde un clon, usa `uv run boe-insights-mcp --transport http`.)

El servidor registra en el log dónde escucha. Con los valores por defecto, la dirección MCP es:

```text
http://127.0.0.1:8000/mcp
```

La ruta `/mcp` es fija. Comprueba que el proceso está en marcha:

```bash
curl http://127.0.0.1:8000/health
```

La respuesta es siempre `{"status":"ok"}`. No llama a boe.es y no revela nada más.

Conecta un cliente a la dirección MCP: consulta las [páginas de cada cliente](clients/generic.md).

## Línea de comandos y entorno

Cada opción sustituye a su variable de entorno.

| Opción | Variable | Valor por defecto | Significado |
|---|---|---|---|
| `--transport` | `BOE_INSIGHTS_TRANSPORT` | `stdio` | `stdio` o `http`. |
| `--host` | `BOE_INSIGHTS_HOST` | `127.0.0.1` | Dirección en la que escucha. |
| `--port` | `BOE_INSIGHTS_PORT` | `8000` | Puerto, de 1 a 65535. |

Por ejemplo, `BOE_INSIGHTS_TRANSPORT=http BOE_INSIGHTS_PORT=9000 uvx boe-insights-mcp` equivale a pasar `--transport http --port 9000`.

## Todos los ajustes de HTTP

| Variable | Valor por defecto | Significado |
|---|---|---|
| `BOE_INSIGHTS_ALLOWED_HOSTS` | vacío | Nombres de host, separados por comas, que el servidor acepta en la cabecera `Host`, además de los nombres de loopback. Solo nombres de host o direcciones IP: sin `https://`, sin puerto, sin ruta y sin comodines. |
| `BOE_INSIGHTS_ALLOWED_ORIGINS` | vacío | Orígenes de navegador aceptados, separados por comas, cada uno exactamente como `http://host[:puerto]` o `https://host[:puerto]`. Sin comodines. |
| `BOE_INSIGHTS_CALLS_PER_MINUTE` | `60` | Llamadas sostenidas por minuto que puede hacer cada usuario. `0` desactiva el límite. |
| `BOE_INSIGHTS_CALLS_BURST` | `20` | Cuántas llamadas se pueden hacer de golpe antes de que el usuario tenga que esperar. Como mínimo 1. |

Las variables del inicio de sesión con GitHub están en [su propia página](github-login.md). Las variables de caché y de datos son las mismas que en [uvx](uvx.md#ajustes).

Un valor no válido detiene el servidor al arrancar con un mensaje que nombra la variable. Los ajustes de HTTP se ignoran por stdio.

## Dirección de escucha y quién puede llegar al servidor

- `127.0.0.1` (el valor por defecto) acepta conexiones solo desde el mismo equipo.
- `0.0.0.0` acepta conexiones por todas las interfaces de red. Úsala solo dentro de un contenedor o detrás de algo que controle el acceso. Un servidor que escucha así y no tiene inicio de sesión está abierto a todos los equipos que puedan llegar a él.
- Detrás de un proxy inverso, deja el servidor en `127.0.0.1` y haz que sea el proxy quien dé la cara a la red.

## Hosts y orígenes permitidos

En cada petición HTTP se hacen dos comprobaciones, siempre, y no se pueden desactivar.

- **Comprobación del host.** La cabecera `Host` debe ser un nombre de loopback (`localhost`, `127.0.0.1`, `[::1]`) o una entrada de `BOE_INSIGHTS_ALLOWED_HOSTS`. Cualquier otro host se rechaza. En las pruebas, una petición con `Host: evil.example` recibió el estado 421. La comprobación protege frente al DNS rebinding, en el que una página web engaña al navegador para que hable con un servidor local.
- **Comprobación del origen.** Una petición que lleve una cabecera `Origin` (los navegadores la envían) debe proceder de un origen permitido. Los clientes MCP de línea de comandos no envían ninguna y no se ven afectados.

Si accedes al servidor con un nombre distinto de `localhost` (por ejemplo `boe.example.org`), añádelo:

```bash
BOE_INSIGHTS_ALLOWED_HOSTS=boe.example.org uvx boe-insights-mcp --transport http
```

Con el inicio de sesión con GitHub activado, el host y el origen de `BOE_INSIGHTS_BASE_URL` se aceptan automáticamente.

## Qué hay disponible por HTTP abierto

HTTP abierto significa HTTP sin inicio de sesión con GitHub. Están todas las herramientas analíticas: búsqueda de leyes, lectura e historial de artículos, modificaciones, comprobación de citas, el diario oficial y las herramientas de tendencias. Son 18 herramientas, algo que se confirmó listándolas en un servidor abierto en marcha.

Las cinco herramientas de búsquedas guardadas **no** están: `save_search`, `list_saved_searches`, `check_saved_searches`, `edit_saved_search` y `delete_saved_search`. Tampoco están el recurso y los prompts correspondientes ni la frase sobre las búsquedas guardadas de las instrucciones del servidor. El motivo es que una búsqueda guardada pertenece a una persona y, sin inicio de sesión, el servidor no puede distinguir a unas personas de otras. En lugar de dejar que desconocidos compartan una misma lista, no ofrece ninguna.

Para tener búsquedas guardadas por HTTP, activa el [inicio de sesión con GitHub](github-login.md). Por stdio siempre funcionan, porque hay una sola persona.

## Límite de llamadas

Cada usuario puede hacer `BOE_INSIGHTS_CALLS_PER_MINUTE` llamadas por minuto (60 por defecto), con ráfagas de hasta `BOE_INSIGHTS_CALLS_BURST` (20 por defecto). Quien se pasa recibe un mensaje que dice que se ha alcanzado el límite de llamadas y cuántos segundos debe esperar.

- Solo cuentan las llamadas que hacen trabajo: llamadas a herramientas, lecturas de recursos y peticiones de prompts. Las peticiones de arranque y de listado no cuentan.
- Por HTTP abierto hay un único límite compartido por todo el mundo, porque sin inicio de sesión el servidor no puede distinguir a los usuarios.
- Con inicio de sesión con GitHub, cada cuenta de GitHub que ha iniciado sesión tiene su propio límite.
- El límite existe para proteger tu servidor. Es independiente del ritmo de las peticiones del propio servidor a boe.es (`BOE_INSIGHTS_MIN_INTERVAL` y `BOE_INSIGHTS_MAX_CONCURRENCY`).
- Por stdio no hay límite de llamadas.

## Tamaño de las peticiones

La dirección MCP acepta cuerpos de petición de hasta 1 MiB. Todas las demás direcciones, incluidas las rutas de inicio de sesión, aceptan hasta 64 KiB. Las peticiones mayores reciben el estado 413. El log de acceso del servidor omite las cadenas de consulta (query strings), de modo que los códigos de inicio de sesión de un solo uso nunca llegan a él.

## Sesiones

El servidor guarda en memoria una sesión por cada cliente conectado. Ejecuta un único proceso del servidor o asegúrate de que un balanceador de carga envíe siempre cada cliente al mismo proceso. (Esto se deduce del diseño de sesiones del servidor; no se probó ejecutar varios procesos detrás de un balanceador.)

## Véase también

- [Inicio de sesión con GitHub](github-login.md), para cerrar el servidor a una lista de personas.
- [Ejecutar en un contenedor](container.md), que ejecuta este transporte por ti.
- [Seguridad y operación](../operating.md).
