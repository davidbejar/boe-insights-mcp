# Ejecutar en un contenedor

La imagen de contenedor ejecuta el servidor por HTTP. Se construye a partir del `Containerfile` de la raíz del repositorio. Los comandos de abajo usan Podman. Docker funciona igual: sustituye `podman` por `docker`, salvo donde se indique lo contrario.

!!! note "Estado de la versión"
    La imagen `ghcr.io/davidbejar/boe-insights-mcp` se publica junto con la versión 0.1.0. Hasta entonces, construye tú la imagen a partir de un clon (siguiente sección).

## Obtener la imagen

Cuando salga la versión:

```bash
podman pull ghcr.io/davidbejar/boe-insights-mcp:0.1.0
```

Hoy, desde un clon del repositorio:

```bash
git clone https://github.com/davidbejar/boe-insights-mcp.git
cd boe-insights-mcp
podman build --format docker -t localhost/boe-insights-mcp:dev -f Containerfile .
```

Con Docker, quita `--format docker`: `docker build -t localhost/boe-insights-mcp:dev -f Containerfile .`. Con Podman la opción es obligatoria: sin ella, Podman descarta en silencio la comprobación de estado de la imagen.

Los comandos de abajo usan el nombre `localhost/boe-insights-mcp:dev`. Cuando salga la versión, usa en su lugar `ghcr.io/davidbejar/boe-insights-mcp:0.1.0`.

## Ejecutarlo

```bash
podman run --rm --name boe-insights -p 127.0.0.1:8000:8000 \
    --read-only --tmpfs /tmp --cap-drop=all --security-opt no-new-privileges \
    -v boe-insights-data:/data localhost/boe-insights-mcp:dev
```

Qué hace cada parte:

- `-p 127.0.0.1:8000:8000` publica el puerto solo en tu propio equipo. Esta es la línea que decide quién puede llegar al servidor. Dentro del contenedor el servidor escucha en `0.0.0.0`, porque de lo contrario el puerto publicado no funcionaría.
- `--read-only --tmpfs /tmp --cap-drop=all --security-opt no-new-privileges` blindan el contenedor. El servidor no necesita escribir en nada más que en `/data`.
- `-v boe-insights-data:/data` guarda tus datos en un volumen con nombre que sobrevive al contenedor.

Compruébalo:

```bash
curl http://127.0.0.1:8000/health
```

La respuesta es `{"status":"ok"}`. La dirección MCP es `http://127.0.0.1:8000/mcp`. Conecta un cliente como se explica en las [páginas de cada cliente](clients/generic.md).

La imagen define una comprobación de estado que llama a `/health` desde dentro del contenedor. `podman ps` muestra el estado.

!!! warning "Sin inicio de sesión por defecto"
    Tal como se ha arrancado arriba, cualquiera que pueda llegar al puerto publicado puede usar el servidor. Mantén el `127.0.0.1:` en `-p` o activa el [inicio de sesión con GitHub](github-login.md). Consulta [Seguridad y operación](../operating.md) antes de publicarlo más ampliamente.

Por HTTP abierto no se ofrecen las herramientas de búsquedas guardadas. Consulta [Ejecutar por HTTP](http.md#qué-hay-disponible-por-http-abierto).

## El volumen

La imagen guarda todo su estado bajo `/data`, el único sitio en el que escribe:

| Ruta en el contenedor | Contiene | Se define con |
|---|---|---|
| `/data/saved-searches` | Las búsquedas guardadas (`saved_searches.sqlite3`). No se debe borrar. | `BOE_INSIGHTS_DATA_DIR` |
| `/data/cache` | La caché de respuestas. Se puede borrar sin riesgo. | `BOE_INSIGHTS_CACHE_DIR` |
| `/data/fastmcp` | Con inicio de sesión con GitHub, los registros de inicio de sesión del servidor (clientes registrados y tokens, almacenados cifrados). Sin inicio de sesión se queda vacío. | `FASTMCP_HOME` |

El servidor se ejecuta como usuario y grupo `10001`, no como root. Un volumen con nombre hereda el propietario correcto la primera vez que se usa. Si en su lugar usas un bind mount, la carpeta debe poder escribirla `10001`:

```bash
mkdir -p ./boe-data && podman unshare chown 10001:10001 ./boe-data
podman run --rm -p 127.0.0.1:8000:8000 -v ./boe-data:/data localhost/boe-insights-mcp:dev
```

`podman unshare` es para Podman sin privilegios de root (rootless). Con Docker o con Podman como root, usa `sudo chown 10001:10001 ./boe-data`. Los comandos del bind mount no se ejecutaron para esta página.

## Variables de entorno

La imagen define las siguientes. Puedes sustituir cualquiera con `-e NOMBRE=valor`.

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

Pon los secretos (el client secret de GitHub y la clave de firma) en un archivo de entorno en lugar de en la línea de comandos, para que no queden en el historial de tu shell ni se vean en `ps`:

```bash
podman run --rm -p 127.0.0.1:8000:8000 --env-file boe.env -v boe-insights-data:/data localhost/boe-insights-mcp:dev
```

Mantén ese archivo privado (`chmod 600 boe.env`).

## Parar, logs y actualizar

```bash
podman logs boe-insights     # log del servidor
podman stop boe-insights     # apagado limpio; el servidor es el proceso principal y recibe la señal
```

Para actualizar, descarga o construye la imagen nueva y arranca un contenedor nuevo con el mismo volumen.

## Sin probar

- El inicio de sesión (con GitHub) contra la imagen de contenedor.
- Los comandos del bind mount de arriba.
- Ejecutar la imagen publicada: todavía no existe.

## Véase también

- [Ejecutar por HTTP](http.md)
- [Inicio de sesión con GitHub](github-login.md)
- [Seguridad y operación](../operating.md)
