# Ejecutar en un contenedor

La imagen de contenedor ejecuta el servidor por HTTP. Se construye a partir del `Containerfile` de la raíz del repositorio. Los comandos de abajo usan Podman. Docker funciona igual: sustituye `podman` por `docker`, salvo donde se indique lo contrario.

## Obtener la imagen

La imagen es pública en GitHub Container Registry, para linux/amd64 y linux/arm64. No hace falta iniciar sesión para descargarla:

```bash
podman pull ghcr.io/davidbejar/boe-insights-mcp:0.1.0
```

También existen las etiquetas `0.1` y `latest`. `0.1` sigue la última versión 0.1.x y `latest` sigue la versión más reciente. Usa la etiqueta de versión exacta, como en esta página, para que una actualización no te sorprenda.

### Construirla tú

Para ejecutar código que aún no está publicado en una versión, construye la imagen a partir de un clon del repositorio:

```bash
git clone https://github.com/davidbejar/boe-insights-mcp.git
cd boe-insights-mcp
podman build --format docker -t localhost/boe-insights-mcp:dev -f Containerfile .
```

Con Docker, quita `--format docker`: `docker build -t localhost/boe-insights-mcp:dev -f Containerfile .`. Con Podman la opción es obligatoria: el formato de imagen por defecto de Podman (OCI) no conserva la comprobación de estado.

Los comandos `podman run` de esta página usan la imagen publicada. Si construiste la imagen tú, usa en ellos `localhost/boe-insights-mcp:dev`.

## Ejecutarlo

```bash
podman run --rm --name boe-insights -p 127.0.0.1:8000:8000 \
    --read-only --tmpfs /tmp --cap-drop=all --security-opt no-new-privileges \
    -v boe-insights-data:/data ghcr.io/davidbejar/boe-insights-mcp:0.1.0
```

Qué hace cada parte:

- `-p 127.0.0.1:8000:8000` publica el puerto solo en tu propio equipo. Esta es la línea que decide quién puede llegar al servidor. Dentro del contenedor el servidor escucha en `0.0.0.0`, porque de lo contrario el puerto publicado no funcionaría. Por eso, sin inicio de sesión con GitHub, el servidor escribe en el log en cada arranque una advertencia de que se puede llegar a él desde otros equipos. Con el puerto publicado en `127.0.0.1`, como aquí, esa advertencia es la esperada.
- `--read-only --tmpfs /tmp --cap-drop=all --security-opt no-new-privileges` blindan el contenedor. El servidor no necesita escribir en nada más que en `/data`.
- `-v boe-insights-data:/data` guarda tus datos en un volumen con nombre que sobrevive al contenedor.

Compruébalo:

```bash
curl http://127.0.0.1:8000/health
```

La respuesta es `{"status":"ok"}`. La dirección MCP es `http://127.0.0.1:8000/mcp`. Conecta un cliente como se explica en las [páginas de cada cliente](clients/generic.md).

La imagen define una comprobación de estado que llama a `/health` desde dentro del contenedor. Docker la aplica, y `docker ps` muestra el estado. Podman no: ignora una comprobación de estado guardada en una imagen en formato OCI, como la publicada, así que tras descargarla `podman ps` no muestra ningún estado. (Una imagen que construyas tú con `--format docker` sí la conserva, como se explica arriba.) Para tener la misma comprobación con la imagen publicada en Podman, añade estas opciones al comando `podman run`:

```bash
    --health-cmd "python -c \"import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('BOE_INSIGHTS_PORT', '8000'), timeout=3)\"" \
    --health-interval 30s --health-timeout 5s --health-start-period 15s --health-retries 3 \
```

Reproducen el `HEALTHCHECK` del `Containerfile`: el mismo comando y los mismos tiempos. Si cambias `BOE_INSIGHTS_PORT`, el comando lo sigue.

!!! warning "Sin inicio de sesión por defecto"
    Tal como se ha arrancado arriba, cualquiera que pueda llegar al puerto publicado puede usar el servidor. Mantén el `127.0.0.1:` en `-p` o activa el [inicio de sesión con GitHub](github-login.md). Consulta [Seguridad y operación](../operating.md) antes de publicarlo más ampliamente.

Por HTTP abierto no se ofrecen las herramientas de búsquedas guardadas. Consulta [Ejecutar por HTTP](http.md#qué-hay-disponible-por-http-abierto).

## El volumen

La imagen guarda todo su estado bajo `/data`, el único sitio en el que escribe:

| Ruta en el contenedor | Contiene | Se define con |
|---|---|---|
| `/data/saved-searches` | Las búsquedas guardadas (`saved_searches.sqlite3`). No se debe borrar. | `BOE_INSIGHTS_DATA_DIR` |
| `/data/cache` | La caché de respuestas. Se puede borrar sin riesgo. | `BOE_INSIGHTS_CACHE_DIR` |
| `/data/fastmcp` | Con inicio de sesión con GitHub, los datos de inicio de sesión del servidor (clientes registrados y tokens, almacenados cifrados). Sin inicio de sesión se queda vacío. | `FASTMCP_HOME` |

El servidor se ejecuta como usuario y grupo `10001`, no como root. Un volumen con nombre hereda el propietario correcto la primera vez que se usa. Si en su lugar usas un bind mount, el usuario `10001` debe poder escribir en la carpeta:

```bash
mkdir -p "$PWD/boe-data" && podman unshare chown 10001:10001 "$PWD/boe-data"
podman run --rm --name boe-insights -p 127.0.0.1:8000:8000 -v "$PWD/boe-data":/data ghcr.io/davidbejar/boe-insights-mcp:0.1.0
```

Indica una ruta absoluta del host para el bind mount, como hace `"$PWD/boe-data"`: algunas versiones de Docker rechazan una relativa. `podman unshare` es para Podman sin privilegios de root (rootless). Con Docker o con Podman como root, usa `sudo chown 10001:10001 "$PWD/boe-data"`. Los comandos del bind mount no se ejecutaron para esta página.

## Variables de entorno

La imagen define las siguientes. Puedes cambiar cualquiera de ellas con `-e NOMBRE=valor`.

| Variable | Valor en la imagen |
|---|---|
| `BOE_INSIGHTS_TRANSPORT` | `http` |
| `BOE_INSIGHTS_HOST` | `0.0.0.0` |
| `BOE_INSIGHTS_PORT` | `8000` |
| `BOE_INSIGHTS_DATA_DIR` | `/data/saved-searches` |
| `BOE_INSIGHTS_CACHE_DIR` | `/data/cache` |
| `FASTMCP_HOME` | `/data/fastmcp` |

Todos los demás ajustes funcionan como en las otras páginas, pasados con `-e`: los [ajustes de HTTP](http.md#todos-los-ajustes-de-http) (hosts y orígenes permitidos, límite de llamadas), los ajustes de ritmo (`BOE_INSIGHTS_MIN_INTERVAL`, `BOE_INSIGHTS_MAX_CONCURRENCY`) y los [ajustes del inicio de sesión con GitHub](github-login.md#ajustes).

Si cambias `BOE_INSIGHTS_PORT`, cambia también la parte derecha de `-p` para que coincida. La comprobación de estado sigue a la variable.

Pon los secretos (el secreto de cliente de GitHub y la clave de firma) en un archivo de entorno en lugar de en la línea de comandos, para que no queden en el historial de tu shell ni se vean en `ps`:

```bash
podman run --rm --name boe-insights -p 127.0.0.1:8000:8000 --env-file boe.env -v boe-insights-data:/data ghcr.io/davidbejar/boe-insights-mcp:0.1.0
```

Mantén ese archivo privado (`chmod 600 boe.env`).

## Parar, logs y actualizar

```bash
podman logs boe-insights     # log del servidor
podman stop boe-insights     # apagado limpio; el servidor es el proceso principal y recibe la señal
```

Estos comandos usan el nombre indicado con `--name boe-insights`, como en todos los comandos `podman run` de esta página.

Para actualizar, descarga o construye la imagen nueva y arranca un contenedor nuevo con el mismo volumen.

## Qué se ha probado

- La imagen se construyó para linux/arm64 y linux/amd64. La imagen arm64 se ejecutó en modo HTTP abierto.
- La imagen publicada `0.1.0rc1` se descargó y se ejecutó con Podman en macOS arm64 el 2026-11-26, con las opciones de blindaje de arriba, y `/health` respondió `{"status":"ok"}`. Podman no mostró ninguna comprobación de estado para ella, como se explica arriba.
- La misma imagen ejecutada con las opciones `--health-cmd` de arriba en Podman, el mismo día: `podman ps` la mostró como sana (`healthy`).

Sin probar:

- La imagen amd64 solo se construyó y se comprobó que se importa; nunca se ejecutó.
- La comprobación de estado con Docker. Que Docker la aplica se basa en que la configuración de la imagen la contiene.
- El inicio de sesión (con GitHub) con la imagen de contenedor.
- Los comandos del bind mount de arriba.

## Véase también

- [Ejecutar por HTTP](http.md)
- [Inicio de sesión con GitHub](github-login.md)
- [Seguridad y operación](../operating.md)
