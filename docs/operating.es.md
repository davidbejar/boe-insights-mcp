# Seguridad y operación

Esta página es para quien ejecuta `boe-insights-mcp` para otras personas. El autor del proyecto no
aloja nada: cada administrador del servidor ejecuta su propia copia y es responsable de ella. Si solo ejecutas el
servidor en tu equipo por stdio, puedes saltarte esta página.

La página explica qué protege el servidor, qué no protege, qué ajustes importan y qué no se ha
probado. Cuando algo no se ha probado, lo dice. Cómo crear la aplicación OAuth y arrancar el
servidor se explica en otras páginas: [HTTP](run/http.md), [contenedor](run/container.md) e
[inicio de sesión con GitHub](run/github-login.md).

## Los tres modos

El modo se decide una sola vez, al arrancar el servidor.

| Modo | Cómo se obtiene | Qué expone |
| --- | --- | --- |
| Local (stdio) | Es el predeterminado. Sin opciones. | Todas las herramientas de análisis, para una sola persona. Ningún puerto de red. Las búsquedas guardadas pertenecen al propietario `local`. |
| HTTP abierto | `BOE_INSIGHTS_TRANSPORT=http` (o `--transport http`), sin ajustes de inicio de sesión. | Las herramientas de análisis, para cualquiera que llegue al puerto. Las cinco herramientas de búsquedas guardadas y el recurso de búsquedas guardadas no se registran en absoluto, porque nadie puede ser propietario de una búsqueda, y los prompts de resumen del diario oficial omiten su sugerencia de guardar la búsqueda. |
| HTTP con inicio de sesión con GitHub | HTTP más los cuatro ajustes de inicio de sesión. Una lista de acceso vacía no deja entrar a nadie. | Todo, pero solo para las cuentas de GitHub de la lista de acceso. Las búsquedas guardadas pertenecen a `github:<id numérico>`. |

Una configuración de inicio de sesión completa se ignora por stdio, y el servidor escribe una
advertencia en el log. Los valores se comprueban igualmente, así que una configuración de inicio de
sesión incompleta o no válida también detiene un servidor stdio.

El HTTP abierto no tiene autenticación de ningún tipo. Es aceptable para datos públicos del BOE en
una red privada, o detrás de algo que autentique. No es buena idea en internet abierto: cualquiera
puede gastar tu presupuesto de peticiones al BOE y tu ancho de banda.

## Modelo de amenazas

Lo que el servidor intenta proteger:

- La máquina y la cuota del administrador del servidor: límites de tamaño de las peticiones, un
  límite de llamadas por usuario, una lista de acceso cerrada y una comprobación de Host y Origin.
- Las búsquedas guardadas de cada usuario: cada fila tiene un propietario tomado del inicio de
  sesión verificado, nunca de un argumento de una herramienta, y ninguna petición por HTTP puede
  obtener el propietario `local`.
- La cortesía con boe.es: todas las peticiones salientes comparten un único limitador y una única
  caché.

Lo que no hace:

- No está reforzado para un uso multiinquilino hostil. La lista de acceso es el límite de confianza:
  a quien está en ella se le confía el uso de tu cuota y el almacenamiento de hasta 20 búsquedas
  guardadas pequeñas.
- Los datos del BOE son públicos. El servidor no los oculta y no necesita hacerlo.
- No termina TLS y no filtra por dirección IP.
- Con el inicio de sesión activado, cualquiera que llegue al servidor puede hacer que descargue una
  dirección `https` de su elección: un cliente puede dar como identificador de cliente la dirección
  de un documento de metadatos, y el servidor la descarga (consulta
  [Registros de clientes y documentos de metadatos](#registros-de-clientes-y-documentos-de-metadatos)).
  FastMCP aplica a esa descarga protección contra la falsificación de peticiones del lado del
  servidor (SSRF). Pon en el proxy un límite de peticiones para `/authorize` y `/register`.
- Ha pasado por dos revisiones de seguridad independientes y una comprobación por mutación del
  código de inicio de sesión. Eso son indicios, no una garantía.

## Ajustes

Todos los ajustes son variables de entorno. Las opciones de línea de comandos `--transport`,
`--host` y `--port` tienen prioridad sobre sus variables. Un valor no válido detiene el servidor al
arrancar con un mensaje que nombra la variable. `BOE_INSIGHTS_HOST` es la excepción: no se comprueba
de antemano.

### Cuándo se niega a arrancar el servidor

- El inicio de sesión es todo o nada: `BOE_INSIGHTS_GITHUB_CLIENT_ID`,
  `BOE_INSIGHTS_GITHUB_CLIENT_SECRET`, `BOE_INSIGHTS_AUTH_SIGNING_KEY` y `BOE_INSIGHTS_BASE_URL`. Si
  se definen unos y otros no, se niega a arrancar, para que un inicio de sesión a medio configurar
  nunca se convierta en un servidor abierto.
- La clave de firma debe tener al menos 32 caracteres.
- `BOE_INSIGHTS_BASE_URL` debe ser `https`, salvo que su host sea loopback.
- `BOE_INSIGHTS_ALLOWED_USERS` o `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` sin los ajustes de inicio de
  sesión es un error, porque la lista no haría nada sin avisar.
- Una entrada mal formada en cualquier lista, un comodín en un host o un origen, o un puerto fuera
  del rango 1-65535.

Hay una cosa que no rechaza: el inicio de sesión configurado con la lista de acceso vacía. Arranca,
escribe una advertencia en el log y nadie puede iniciar sesión. La lista está cerrada por defecto.

### Tabla

| Variable | Valor por defecto | Qué hace |
| --- | --- | --- |
| `BOE_INSIGHTS_TRANSPORT` | `stdio` | `stdio` o `http`. |
| `BOE_INSIGHTS_HOST` | `127.0.0.1` | Dirección en la que escucha el servidor HTTP. La imagen de contenedor usa `0.0.0.0`. |
| `BOE_INSIGHTS_PORT` | `8000` | Puerto HTTP. MCP se sirve en `/mcp`. `GET /health` devuelve un `{"status":"ok"}` constante sin necesidad de iniciar sesión. |
| `BOE_INSIGHTS_BASE_URL` | ninguno | La URL pública a la que llegan los clientes, como `https://host[:puerto]`, sin ruta, consulta ni credenciales. Obligatoria para el inicio de sesión. |
| `BOE_INSIGHTS_ALLOWED_HOSTS` | ninguno | Nombres de host separados por comas que se aceptan en la cabecera `Host`. Solo nombres o direcciones IP: sin esquema, puerto, ruta ni comodín. Las direcciones de loopback se aceptan siempre; con inicio de sesión se añade el host de la URL base. |
| `BOE_INSIGHTS_ALLOWED_ORIGINS` | ninguno | Orígenes de navegador separados por comas, cada uno con la forma `http(s)://host[:puerto]`. Con inicio de sesión se añade el origen de la URL base. |
| `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` | ninguno | URI de redirección adicionales que los clientes OAuth pueden registrar. URL absolutas exactas, sin comodines; `https`, o `http` solo para loopback. Las redirecciones a loopback en cualquier puerto se permiten siempre. Un registro se rechaza por completo si alguna de sus URI de redirección no está permitida. |
| `BOE_INSIGHTS_GITHUB_CLIENT_ID` | ninguno | ID de cliente de tu aplicación OAuth de GitHub. |
| `BOE_INSIGHTS_GITHUB_CLIENT_SECRET` | ninguno | Su secreto. Es un secreto: consulta [Secretos](#secretos). |
| `BOE_INSIGHTS_AUTH_SIGNING_KEY` | ninguno | Firma los tokens que emite el servidor y cifra su almacén de tokens. Al menos 32 caracteres. Es un secreto. |
| `BOE_INSIGHTS_ALLOWED_USERS` | ninguno (nadie) | Separados por comas: logins de GitHub, `id:<número>` o `*` para todas las cuentas de GitHub. Se lee una sola vez, al arrancar. |
| `BOE_INSIGHTS_CALLS_PER_MINUTE` | `60` | Llamadas sostenidas por usuario y minuto por HTTP. `0` desactiva el límite. |
| `BOE_INSIGHTS_CALLS_BURST` | `20` | Cuántas llamadas seguidas puede hacer un usuario antes de tener que esperar. |
| `BOE_INSIGHTS_MIN_INTERVAL` | `1.0` | Mínimo de segundos entre el inicio de una petición a boe.es y el de la siguiente, para todo el servidor. |
| `BOE_INSIGHTS_MAX_CONCURRENCY` | `2` | Máximo de peticiones simultáneas a boe.es, para todo el servidor. |
| `BOE_INSIGHTS_DATA_DIR` | directorio de datos de la plataforma; el contenedor usa `/data/saved-searches` | Dónde está la base de datos de búsquedas guardadas. No es seguro borrarla. |
| `BOE_INSIGHTS_CACHE` | `disk` | `disk` o `memory`. Con `memory` y sin directorio de datos, las búsquedas guardadas también se conservan solo en memoria. |
| `BOE_INSIGHTS_CACHE_DIR` | directorio de caché de la plataforma; el contenedor usa `/data/cache` | Dónde está la caché de respuestas. Es seguro borrarla. |
| `FASTMCP_HOME` | directorio de datos de la plataforma de FastMCP; el contenedor usa `/data/fastmcp` | Lo lee FastMCP, no este proyecto. Contiene el almacén de tokens de inicio de sesión. Consulta [Almacén de tokens](#almacén-de-tokens). |

`*` en la lista de acceso significa que cualquier cuenta de GitHub puede iniciar sesión. Úsalo solo
si de verdad quieres eso.

## Red

El servidor escucha en `127.0.0.1` por defecto. Por HTTP habla HTTP sin cifrar: no tiene TLS
propio.

Para cualquier uso más allá de tu propio equipo, pon delante un proxy inverso que termine TLS, y
deja el servidor en loopback o en una red privada. Después:

- Define `BOE_INSIGHTS_BASE_URL` con la dirección pública `https` que usan los clientes. El
  servidor rechaza una URL base `http` salvo que sea loopback, porque los códigos de inicio de
  sesión y los tokens viajan por esas redirecciones.
- Si el proxy pasa la cabecera `Host` original (el nombre público), incluye ese nombre en
  `BOE_INSIGHTS_ALLOWED_HOSTS`; con inicio de sesión, el host de la URL base se acepta
  automáticamente. Un proxy que envía como `Host` la dirección de loopback del servidor (por
  ejemplo, `127.0.0.1:8000`) se acepta tal cual.
- No publiques el puerto directamente. Dentro del contenedor, la imagen escucha en `0.0.0.0`; lo
  que importa es cómo publicas el puerto (por ejemplo, `-p 127.0.0.1:8000:8000`). Por escuchar en
  esa dirección, un contenedor en modo HTTP abierto escribe una advertencia en el log en cada
  arranque; consulta [Comprobación de Host y Origin](#comprobación-de-host-y-origin).
- Pon un límite de peticiones a nivel de red en el proxy. El límite del propio servidor cuenta solo
  las llamadas MCP que hacen trabajo (consulta [El límite de llamadas](#el-límite-de-llamadas)). No
  cubre las rutas que no requieren iniciar sesión (`/register`, `/authorize`, `/token`, `/consent`,
  `/auth/callback`, `/.well-known/oauth-authorization-server`,
  `/.well-known/oauth-protected-resource/mcp`), `/health` ni las avalanchas de conexiones.

### Comprobación de Host y Origin

Se comprueba cada petición HTTP, en todas las rutas, incluida `/health`, y no hay ningún ajuste que
lo desactive. Una petición cuyo `Host` no está en la lista recibe `421`. La lista la forman los
nombres de loopback, `BOE_INSIGHTS_ALLOWED_HOSTS`, el host de la URL base si hay inicio de sesión y
la dirección en la que escucha el servidor, salvo que sea `0.0.0.0` o `::`. Es una protección contra
el DNS rebinding: una página web que engaña a un navegador para que hable con un servidor de tu
red. Una petición con cabecera `Origin` debe proceder de un origen permitido, de la propia dirección
del servidor o, cuando se llega al servidor con un nombre de loopback, de una dirección de loopback;
cualquier otro origen recibe `403`.

En modo HTTP abierto, el servidor escribe una advertencia en el log al arrancar si escucha en una
dirección que no es de loopback. Aun así arranca: eres responsable de no exponerlo. La imagen de
contenedor siempre escucha en `0.0.0.0`, así que en modo abierto la advertencia aparece en cada
arranque; con el puerto publicado en `127.0.0.1` es la esperada.

### Límites de tamaño y de tiempo

- El cuerpo de una petición a `/mcp` puede ser de 1 MiB como máximo. Cualquier otra ruta
  (formularios de inicio de sesión, registro de clientes), de 64 KiB como máximo. Los cuerpos más
  grandes reciben `413`. Una cabecera `Content-Length` que declara más del límite se rechaza en el
  acto; si no, se cuentan los bytes realmente recibidos, diga lo que diga la cabecera.
- Las llamadas largas a herramientas (exploraciones del diario oficial, comprobaciones de búsquedas
  guardadas, el explorador de cadenas de modificaciones y la clasificación de normas más
  modificadas) tienen un presupuesto de unos 40 segundos. En los últimos 5 segundos no inician
  ninguna petición al BOE que no esté en caché y devuelven lo que tienen con `complete: false`; las
  instrucciones del servidor indican al modelo que vuelva a llamar.
- No hay ningún tiempo de espera de petición delante del servidor. Defínelo en el proxy.

## Inicio de sesión

El servidor usa el proveedor de GitHub de FastMCP. Pide a GitHub como mucho el ámbito `read:user`,
y ningún ámbito cuando el cliente no pide ninguno: ni acceso de escritura ni acceso a repositorios.
Solo necesita el id numérico y el login que
GitHub devuelve para cualquier token válido. Se rechazan los clientes que piden `user` o `repo`.

### Entradas de la lista de acceso

Dos formas, que se pueden mezclar:

- Un login de GitHub, como `octocat`. Se compara sin distinguir mayúsculas de minúsculas con el
  login que devuelve GitHub.
- `id:<número>`, como `id:583231`, el id numérico de la cuenta de GitHub.

Es preferible usar `id:`. Un login se puede cambiar, y un nombre que queda libre puede ser ocupado
después por otra persona, que entonces coincidiría con tu entrada. El id nunca cambia. Si una entrada
de login admite a alguien, el servidor escribe una vez en el log, con nivel INFO, que `id:<n>` es la
forma más segura (la línea contiene el login y el id).

Una persona a la que has rechazado ve qué debe pedir: la página de rechazo indica su `id:<n>` y su
login, para que puedas copiarlos en la lista. La página no dice nada sobre quién está en la lista.

### Dónde se detiene a una cuenta que no figura en la lista

Hay cuatro puntos, de modo que un error en uno no deje entrar a nadie:

1. En la devolución de llamada de GitHub (callback). El navegador recibe una página 403 con el
   título "Access not allowed". El código de un solo uso se elimina.
2. En el intercambio del código. Si de algún modo se superó el callback, el intercambio falla con
   `invalid_grant` y no guarda nada.
3. En la renovación del token. Una renovación de una cuenta que ya no está admitida falla con
   `invalid_grant`, y se borran el token de renovación presentado, su correspondencia de tokens y el
   token de GitHub que hay detrás. Los demás clientes de la misma cuenta se rechazan de la misma
   forma cuando intenten renovar. Si GitHub no puede decir a quién pertenece un token (porque el
   token está revocado o GitHub no responde), la renovación se rechaza pero los datos guardados se
   conservan, porque eso no es una decisión sobre la lista.
4. En cada petición. Cada petición a `/mcp` vuelve a cotejar la identidad con la lista, y cada
   herramienta, recurso y prompt se comprueba de nuevo al listarlo y al usarlo.

### Cómo surte efecto una baja

La lista de acceso se lee una sola vez, al arrancar. Cambiar `BOE_INSIGHTS_ALLOWED_USERS` no tiene
efecto en un servidor en marcha: **reinícialo**. Tras el reinicio, una cuenta que hayas quitado de la
lista se rechaza en su siguiente petición (`401`) y en su siguiente renovación de token. Sus
búsquedas guardadas siguen en la base de datos hasta que las borres (consulta [Datos](#datos)).

El servidor recuerda durante un máximo de 5 minutos que un token de GitHub superó la comprobación,
para que la carga sobre GitHub sea baja. Esa caché solo afecta a la pregunta "¿sigue GitHub
aceptando este token?"; la lista de acceso en sí se comprueba en cada petición.

### URI de redirección

Los clientes OAuth registran adónde debe devolverse el navegador. Por defecto solo se aceptan
direcciones de loopback (`http://localhost:*`, `http://127.0.0.1:*`, `http://[::1]:*`, cualquier
puerto), que es lo que usan los clientes de línea de comandos como Claude Code. Un cliente web
necesita su URL de callback exacta en `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS`. Sin una restricción,
podría entregarse un código de inicio de sesión a una página elegida por un atacante; por eso existe
la restricción.

Un registro se rechaza por completo si alguna de las URI de redirección que enumera no está
permitida, aunque las demás sean direcciones de loopback. Según su documentación, Cursor y VS Code
registran una dirección web además de una de loopback (`https://www.cursor.com/agents/mcp/oauth/callback`,
`https://vscode.dev/redirect`), así que solo pueden registrarse cuando esa dirección figura en la
lista. Incluirla significa que los códigos de inicio de sesión de ese cliente pueden entregarse a
través de ese sitio. Consulta [URI de redirección](run/github-login.md#uri-de-redirección).

### Registros de clientes y documentos de metadatos

Un cliente se identifica de una de estas dos formas, y ninguna requiere una cuenta: la lista de
acceso se aplica al iniciar sesión, no aquí.

- **Registro.** El cliente se registra en `/register`. El registro está abierto a cualquiera que
  llegue al servidor.
- **Documento de metadatos.** El cliente da como identificador de cliente la dirección `https` de
  un documento que lo describe, y el servidor descarga ese documento. Claude Code funciona así: en
  la prueba real de inicio de sesión, su identificador de cliente era
  `https://claude.ai/oauth/claude-code-client-metadata`. La dirección la elige el cliente, así que
  cualquiera que llegue al servidor puede hacer que descargue una dirección `https` de su elección.
  FastMCP aplica a esa descarga protección contra la falsificación de peticiones del lado del
  servidor (SSRF).

Pon en el proxy un límite de peticiones para `/authorize` y `/register`.

### Secretos

- La clave de firma firma los tokens que emite el servidor y es la clave con la que se cifra el
  almacén de tokens. Genera una aleatoria, por ejemplo con `openssl rand -hex 32`.
- El secreto de cliente de GitHub se obtiene en la página de tu aplicación OAuth.
- Guarda ambos fuera de la imagen y del repositorio: un gestor de secretos, o un archivo de entorno
  con modo 0600 que no se suba al repositorio. El servidor nunca imprime ninguno de los dos, y
  ningún error de arranque los repite.
- Cambiar la clave de firma cierra la sesión de todo el mundo. FastMCP pasa entonces a usar otra
  carpeta del almacén de tokens, de modo que los clientes se registran e inician sesión de nuevo. La
  carpeta antigua se conserva; puedes borrarla.

### Almacén de tokens

FastMCP guarda los registros de clientes, las transacciones de inicio de sesión y los tokens
emitidos como archivos cifrados en `$FASTMCP_HOME/oauth-proxy/<huella de la clave>/`. Se cifran con
una clave derivada de la clave de firma. Si no defines `FASTMCP_HOME`, es el directorio de datos
por usuario de FastMCP, una mala elección para un servicio. La carpeta debe:

- conservarse entre reinicios (si no, todo el mundo inicia sesión de nuevo después de cada
  despliegue);
- ser privada para la cuenta que ejecuta el servidor. FastMCP no la restringe: crea la carpeta con
  los permisos por defecto (se ha visto `drwxr-xr-x`, legible por otras cuentas), así que hazla
  privada tú, por ejemplo con `chmod 700` sobre `$FASTMCP_HOME`. La imagen de contenedor crea
  `/data` con modo 0700 para el usuario del servidor; una carpeta montada con bind mount conserva
  los permisos que le des;
- tener copia de seguridad solo si aceptas que la copia contiene tokens de GitHub cifrados (son
  inútiles sin la clave de firma).

La imagen de contenedor establece `FASTMCP_HOME=/data/fastmcp`, en el volumen.

#### Los registros de clientes se conservan sin caducidad

FastMCP guarda cada registro hecho en `/register` sin caducidad y nunca lo elimina, ni siquiera al
reiniciar. Cada uno es pequeño, pero se acumulan, y registrarse no requiere una cuenta. Para
limpiarlos: detén el servidor, borra la carpeta `$FASTMCP_HOME/oauth-proxy/<huella de la clave>/` y
vuelve a arrancarlo. Esto también cierra la sesión de todo el mundo. Se espera entonces que cada
cliente se registre de nuevo, o vuelva a descargar su documento de metadatos, y pida a la persona
que inicie sesión; no se ha probado con ningún cliente, tampoco con Claude Code. No hay ningún
comando para eliminar un registro concreto. Poner un límite de peticiones a `/register` en el proxy mantiene bajo su número.

## Límites y abusos

### El límite de llamadas

Cada usuario puede hacer `BOE_INSIGHTS_CALLS_PER_MINUTE` llamadas por minuto, con una ráfaga de
`BOE_INSIGHTS_CALLS_BURST`. Es un token bucket (cubo de tokens). Un usuario que supera el límite
recibe un mensaje que indica cuántos segundos debe esperar. Solo se cuentan las llamadas a
herramientas, las lecturas de recursos y las peticiones de prompts. No se cuentan los listados de
herramientas, el handshake ni los pings.

Quién cuenta como "un usuario" a efectos del límite:

- Con sesión iniciada: el id numérico de GitHub. Un cubo por persona, sea cual sea el dispositivo o
  la dirección que use.
- HTTP abierto: todos los usuarios cuentan como uno solo, con un único cubo compartido. Sin inicio
  de sesión no hay nada en una petición en lo que se pueda confiar para distinguir a los usuarios (una dirección es lo que diga
  el cliente o el proxy), así que el límite protege al servidor en su conjunto, y un cliente
  ruidoso puede consumir la parte de todos los demás.

El servidor no usa direcciones IP en absoluto, así que ponerlo detrás de un proxy no cambia en qué
cubo cae una llamada. Para límites por dirección, usa el proxy.

El límite de llamadas no limita el coste de una llamada. Una sola llamada a una exploración del
diario oficial o a una herramienta de tendencias puede provocar muchas peticiones a boe.es. Eso lo
contiene el limitador compartido que se describe en
[El presupuesto compartido del BOE](#el-presupuesto-compartido-del-boe).

### El presupuesto compartido del BOE

Todas las peticiones a boe.es, de todos los usuarios, pasan por un único limitador: como máximo un
inicio de petición cada `BOE_INSIGHTS_MIN_INTERVAL` segundos (1 por defecto) y como máximo
`BOE_INSIGHTS_MAX_CONCURRENCY` (2 por defecto) a la vez. Así, todos tus usuarios comparten un único
presupuesto respetuoso con el BOE, de aproximadamente una petición por segundo. Los resultados se
guardan en caché (los resultados de búsqueda durante 30 minutos, los metadatos de las normas y el análisis de
modificaciones durante 6 horas, las tablas de códigos durante 7 días), así que la mayoría de las
preguntas repetidas no cuestan nada. Muchos usuarios con preguntas distintas se ralentizarán entre
sí. No reduzcas el intervalo para arreglarlo: es la cortesía que se pide a quien usa la API del BOE,
y de ella nunca se ha visto ninguna señal de limitación de peticiones (véase `api-notes.md` en el
repositorio).

### Búsquedas guardadas

Como máximo 20 por propietario. Un nombre es único por propietario. El filtro de palabras tiene como
máximo 200 caracteres. Una comprobación muestra como máximo `limit` elementos por búsqueda (20 por
defecto, 50 como máximo) y como máximo 60 elementos en total por llamada, y puede mirar atrás como
máximo 90 días. No hay límite al número de propietarios aparte de tu
lista de acceso.

## Datos

| Qué | Dónde | Datos personales | ¿Se puede borrar? |
| --- | --- | --- | --- |
| Búsquedas guardadas | `saved_searches.sqlite3` en `BOE_INSIGHTS_DATA_DIR` (archivo con modo 0600, carpetas 0700) | El propietario `github:<id numérico>` en cada fila, más los filtros que eligió la persona. El login no se guarda ahí. | No: borrarlo elimina las búsquedas de la gente. |
| Caché de respuestas | `BOE_INSIGHTS_CACHE_DIR` | Ninguno. Respuestas públicas del BOE. | Sí. |
| Almacén de tokens | `$FASTMCP_HOME/oauth-proxy/...` | Tokens de acceso de GitHub cifrados (y tokens de renovación, si GitHub los emitió), cada uno con el id del cliente MCP al que se emitió. Ahí no se guarda ningún id ni login de GitHub. | Sí, pero todo el mundo inicia sesión de nuevo. |
| Logs | stderr y el log de accesos | Consulta [Logs](#logs). | Sí: el servidor nunca los vuelve a leer. Consérvalos solo el tiempo que indique tu norma de conservación. |

Por HTTP abierto y por stdio no se guardan cuentas ni tokens. Por HTTP abierto, el log de accesos
sigue conteniendo las direcciones de los clientes (consulta [Logs](#logs)). Por stdio el
propietario es `local`.

**Copia de seguridad.** Haz copia de la carpeta de búsquedas guardadas. SQLite es un único archivo;
para una copia coherente, detén el servidor o usa el comando `sqlite3 .backup`. La caché no
necesita copia.

**Borrado de los datos de una persona.** No hay un comando para ello. Detén el servidor y borra de la
tabla `saved_searches` las filas cuyo `owner` sea `github:<id>`, o pide a la persona que borre sus
búsquedas con la herramienta `delete_saved_search`. Quitar a alguien de la lista de acceso no borra
sus filas. Si una persona te pide que borres sus datos, limpia también el almacén de tokens
(consulta [Almacén de tokens](#almacén-de-tokens)), que contiene su token de GitHub. El almacén de
tokens no indica de quién es cada token, así que limpiarlo supone limpiarlo para todo el mundo.

### Logs

El servidor escribe dos logs, ambos en la salida del proceso.

**El log de accesos** (uvicorn) muestra el método, la ruta y el estado de cada petición y la
dirección del cliente. El servidor elimina la cadena de consulta de cada línea antes de escribirla,
porque las rutas de inicio de sesión llevan ahí códigos de un solo uso e identificadores de
transacción. Los tokens viajan en cabeceras y cuerpos, que el log de accesos no muestra. La
dirección del cliente es la que ve el servidor, que detrás de un proxy es la del proxy salvo que
configures el reenvío.

**El log del servidor** (stderr) contiene las advertencias de arranque, una línea INFO por cada
usuario admitido por una entrada de login de la lista de acceso (con su login y su id, una vez por
proceso) y los mensajes del propio FastMCP. El código propio del servidor no escribe en el log
ningún token ni secreto. Los mensajes de FastMCP pueden nombrar un identificador de transacción de
inicio de sesión, que dura poco (visto: `IdP callback with invalid transaction ID: …`). No se ha
visto en ellos ningún código de un solo uso ni ningún token.

## Cosas que verás y que no deben preocuparte

- Al iniciar sesión, la comprobación de tokens de FastMCP llama a `/user` de GitHub y también a
  `/user/repos`, aunque solo se concede `read:user`. Con la segunda llamada, FastMCP lee los
  ámbitos de un token. El servidor no usa la respuesta y no pide acceso a repositorios; esta página
  no puede decir exactamente qué devuelve GitHub a la segunda llamada. Estas comprobaciones se
  guardan en caché 5 minutos.
- La página de consentimiento, que aparece en cada inicio de sesión, viene de FastMCP. Muestra el
  logotipo de este proyecto y el nombre del servidor, `boe-insights`, enlazado al repositorio del
  proyecto. Su nota "Why am I seeing this?" sigue mencionando FastMCP y enlaza a gofastmcp.com. Las
  páginas de error de FastMCP, incluida la página 403 "Access not allowed", conservan el logotipo de
  FastMCP, que el navegador carga desde gofastmcp.com.
- Los registros de clientes que se acumulan (consulta
  [Los registros de clientes se conservan sin caducidad](#los-registros-de-clientes-se-conservan-sin-caducidad)).
- Con una entrada de login en la lista de acceso, una línea INFO por persona que sugiere `id:<n>`.
- En el contenedor en modo HTTP abierto, una advertencia en cada arranque de que se puede llegar al
  servidor desde otros equipos. Se debe a que dentro del contenedor el servidor escucha en
  `0.0.0.0`; con el puerto publicado en `127.0.0.1` es la esperada.
- Con una URL base `http://localhost`, la advertencia de FastMCP "Using non-secure cookies for
  development; deploy with HTTPS for production." en cada arranque.

## Actualizaciones

`login.py` sobrescribe partes privadas de FastMCP: `_handle_idp_callback`,
`exchange_authorization_code` y `exchange_refresh_token`, y lee los almacenes privados
`_code_store`, `_refresh_token_store`, `_jti_mapping_store`, `_upstream_token_store` y el
`_token_validator`, e importa la función privada `_hash_token`. FastMCP no promete que se mantengan
igual.

Por eso la dependencia está limitada a `fastmcp>=4.2.4,<4.3` y la versión exacta la fija `uv.lock`; el
contenedor se construye a partir de ese archivo de bloqueo. No actualices FastMCP a mano en tu
propio despliegue. Cuando una versión nueva de este proyecto adopta una versión nueva de FastMCP, ya
ha superado las pruebas de inicio de sesión (`tests/test_login_flow.py` y los demás archivos
`test_login_*` y `test_launch_login.py`). Si actualizas FastMCP por tu cuenta, ejecútalas antes: una
discrepancia puede dejar la lista de acceso sin aplicar en uno de los cuatro puntos anteriores, y son
las pruebas las que lo detectan.

## Notas sobre la aplicación OAuth de GitHub

- El campo del formulario se llama "Redirect URIs" (antes era "Authorization callback URL"). Su
  valor es tu URL base seguida de `/auth/callback`; consulta
  [inicio de sesión con GitHub](run/github-login.md) para ver los pasos exactos.
- "Expire user access tokens" está activado por defecto en las aplicaciones nuevas. El inicio de
  sesión funciona con él activado. La renovación del token tras una caducidad real no se ha probado
  (consulta [Sin probar](#sin-probar)).
- Crea la aplicación OAuth con la cuenta que deba ser su propietaria, y guarda su secreto como se
  describe en [Secretos](#secretos).

## Sin probar

Lo decimos claramente: esto no se ha probado, y puede que seas la primera persona en hacerlo.

- La renovación del token después de que un token de GitHub haya caducado de verdad. La lógica de
  renovación se prueba con GitHub sustituido por un simulacro.
- Ejecutarlo detrás de un proxy inverso HTTPS real. La gestión de Host, Origin y URL base está
  probada, pero no con un proxy real delante.
- Clientes distintos de Claude Code en el flujo de inicio de sesión, incluido cómo muestran la
  página 403 "Access not allowed".
- El inicio de sesión con la imagen de contenedor. La imagen se construyó para linux/amd64 y
  linux/arm64; solo se ejecutó la de arm64, y en modo HTTP abierto. La de amd64 solo se construyó y
  se comprobó que se importa.
- Que un cliente, cualquiera que sea y Claude Code incluido, vuelva a registrarse después de borrar
  el almacén de tokens.
- La carga con muchos usuarios. Los límites de arriba son de diseño, no están medidos.

El autor hizo un inicio de sesión real con GitHub durante el desarrollo (así encontró los dos
fallos corregidos en el commit `e1dbc6d`). Se hizo solo con Claude Code, en `localhost`, sin proxy
delante y no con la imagen de contenedor.

## Tus obligaciones con el BOE

Los datos son del BOE, y sus condiciones de reutilización se aplican a tu instancia:

- Muestra la atribución "Basado en datos de la Agencia Estatal Boletín Oficial del Estado" con un
  enlace a <https://www.boe.es>. Las instrucciones del servidor y los prompts indican al modelo que
  la transmita; no la quites de lo que muestras a las personas.
- Los textos consolidados son informativos y no tienen valor jurídico oficial. No quites esa
  etiqueta.
- No presentes tu instancia como un servicio oficial ni como el del BOE. No alteres el sentido de
  los textos jurídicos.
- Conserva el User-Agent identificativo (`boe-insights-mcp/<versión>` con la URL del proyecto) y el
  ritmo de peticiones moderado. No reduzcas `BOE_INSIGHTS_MIN_INTERVAL` por debajo de 1 segundo ni
  subas la concurrencia sin una razón.

## Antes de ponerlo en producción

- [ ] El servidor está detrás de un proxy inverso con TLS, y el puerto no se publica directamente.
- [ ] `BOE_INSIGHTS_BASE_URL` es la dirección pública `https`.
- [ ] Si el proxy pasa la cabecera `Host` pública y el inicio de sesión está desactivado, el nombre
  público está en `BOE_INSIGHTS_ALLOWED_HOSTS`.
- [ ] El proxy tiene un límite de peticiones y un tiempo de espera de petición, también para
  `/register` y `/authorize`.
- [ ] Si van a iniciar sesión clientes como Cursor o VS Code, sus URI de redirección web están en
  `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS`.
- [ ] El inicio de sesión está activado, salvo que la instancia sea privada. La lista de acceso usa
  entradas `id:<n>`.
- [ ] La clave de firma es aleatoria, de al menos 32 caracteres, y se guarda fuera de la imagen y
  del repositorio.
- [ ] El secreto de cliente de GitHub se guarda de la misma manera.
- [ ] `FASTMCP_HOME` y `BOE_INSIGHTS_DATA_DIR` están en almacenamiento persistente, privado para la
  cuenta del servidor (FastMCP no hace privado `FASTMCP_HOME`: aplícale `chmod 700`). Se hace copia
  de seguridad de la carpeta de búsquedas guardadas.
- [ ] Sabes que los cambios en la lista de acceso requieren un reinicio.
- [ ] Los logs se conservan durante un tiempo limitado. Has decidido quién puede leerlos.
- [ ] La atribución y la etiqueta "informativo, no oficial" están intactas, y tu instancia no
  afirma ser oficial.
- [ ] Has leído la lista "Sin probar" y la aceptas.
