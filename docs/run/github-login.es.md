# Inicio de sesión con GitHub

El inicio de sesión con GitHub es opcional. Está desactivado por defecto y por stdio no hace nada. Cuando ofreces el servidor [por HTTP](http.md), obliga a las personas a iniciar sesión con una cuenta de GitHub, y solo entran las cuentas que figuran en una lista que escribes tú. La lista es cerrada: una lista vacía no deja entrar a nadie.

Activarlo también da a cada persona sus propias búsquedas guardadas. Sin inicio de sesión, las herramientas de búsquedas guardadas no se ofrecen por HTTP.

Esta página trata de cómo configurarlo. Las amenazas, el refuerzo de la seguridad y los consejos sobre proxies están en [Seguridad y operación](../operating.md).

## Qué necesitas

- Una cuenta de GitHub que pueda crear una aplicación OAuth.
- El servidor accesible en una URL que tus usuarios escriban en su cliente. Para una primera prueba en tu propio equipo basta con `http://localhost:8000`.
- Para cualquier cosa que no sea localhost, una dirección `https`. El servidor se niega a arrancar con una URL base `http` salvo que su host sea `localhost` u otra dirección de loopback.

## 1. Crear una aplicación OAuth de GitHub

1. En GitHub, abre **Settings**, después **Developer settings**, después **OAuth Apps** y después **New OAuth App**.
2. Rellena el formulario:
    - **Application name**: lo que quieras, siempre que tus usuarios lo reconozcan.
    - **Homepage URL**: cualquier dirección, por ejemplo la URL base de tu servidor.
    - **Redirect URIs**: la URL base del servidor seguida de `/auth/callback`. Para una prueba local: `http://localhost:8000/auth/callback`. Para un servidor público: `https://boe.example.org/auth/callback`.
3. Registra la aplicación y después genera un **client secret**. Copia el **Client ID** y el secreto.

El formulario de GitHub antes llamaba al campo de redirección "Authorization callback URL". Ahora se llama "Redirect URIs". Es lo mismo. La ruta `/auth/callback` es la que FastMCP usa por defecto y este servidor no la cambia. La dirección debe coincidir exactamente con la URL base del servidor, incluidos `http` o `https` y el puerto.

GitHub tiene un ajuste, "Expire user access tokens", activado por defecto. El inicio de sesión funciona con él activado. Déjalo así.

## 2. Configurar el servidor

El inicio de sesión necesita cuatro variables, todas o ninguna. Si solo se definen algunas, el servidor se niega a arrancar y nombra las que faltan. Añade la lista de acceso como quinta.

```bash
export BOE_INSIGHTS_GITHUB_CLIENT_ID=<ID de cliente de GitHub>
export BOE_INSIGHTS_GITHUB_CLIENT_SECRET=<secreto de cliente de GitHub>
export BOE_INSIGHTS_AUTH_SIGNING_KEY=$(openssl rand -hex 32)
export BOE_INSIGHTS_BASE_URL=http://localhost:8000
export BOE_INSIGHTS_ALLOWED_USERS=octocat,id:1234567

uvx boe-insights-mcp --transport http
```

(Desde un clon, usa `uv run boe-insights-mcp --transport http`.)

Conserva la clave de firma. Si generas una nueva cada vez que arrancas, las personas tienen que volver a iniciar sesión cada vez. Guarda el secreto y la clave en un archivo privado o en un gestor de secretos, nunca en el repositorio.

### Ajustes

| Variable | Significado |
|---|---|
| `BOE_INSIGHTS_GITHUB_CLIENT_ID` | El ID de cliente de la aplicación OAuth. |
| `BOE_INSIGHTS_GITHUB_CLIENT_SECRET` | El secreto de cliente de la aplicación OAuth. |
| `BOE_INSIGHTS_AUTH_SIGNING_KEY` | Un secreto de al menos 32 caracteres. El servidor lo usa para firmar los tokens que entrega a los clientes. Cambiarlo cierra la sesión de todo el mundo, y los datos de inicio de sesión que el servidor guardó con la clave anterior dejan de usarse. |
| `BOE_INSIGHTS_BASE_URL` | La dirección pública del servidor tal como la usan los usuarios: `http(s)://host[:puerto]`, sin ruta, sin cadena de consulta y sin credenciales. La dirección MCP del servidor es esta más `/mcp`. Se exige `https` salvo que el host sea de loopback. |
| `BOE_INSIGHTS_ALLOWED_USERS` | Lista de acceso separada por comas. Consulta [La lista de acceso](#la-lista-de-acceso). Vacía significa que nadie puede iniciar sesión, y el servidor escribe una advertencia en el log. |
| `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` | Opcional. URI de redirección adicionales, a las que se puede devolver a un cliente tras el inicio de sesión, cada una de ellas una URL `https` exacta (`http` solo para loopback), sin comodines. Las direcciones de loopback (`localhost`, `127.0.0.1`, `[::1]`, cualquier puerto) se permiten siempre, y es lo que usan los clientes de línea de comandos como Claude Code. El registro de un cliente se rechaza por completo si alguna de las direcciones que registra no está permitida: consulta [URI de redirección](#uri-de-redirección). |

Reglas que el servidor aplica al arrancar:

- Definir `BOE_INSIGHTS_ALLOWED_USERS` o `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS` sin las cuatro variables de inicio de sesión es un error. De lo contrario podrías creer que una lista protege el servidor cuando no es así.
- Por stdio una configuración de inicio de sesión completa se ignora, con una advertencia. El inicio de sesión solo se aplica con `--transport http`. Los valores se comprueban igualmente, así que una configuración de inicio de sesión incompleta o no válida también detiene un servidor stdio.
- El host de la URL base se añade automáticamente a los hosts y orígenes permitidos. Consulta [Ejecutar por HTTP](http.md#hosts-y-orígenes-permitidos).

### URI de redirección

Un cliente indica al servidor adónde se puede devolver el navegador tras el inicio de sesión, de una de estas dos formas:

- **Registro.** El cliente se registra en `/register` y enumera sus direcciones. El servidor rechaza el registro completo si alguna de ellas no está permitida, aunque las demás sean direcciones de loopback.
- **Documento de metadatos.** El cliente da como identificador de cliente la dirección `https` de un documento que lo describe, y el servidor descarga ese documento. Claude Code funciona así: en la prueba real de inicio de sesión, su identificador de cliente era `https://claude.ai/oauth/claude-code-client-metadata`. La dirección que se usa al iniciar sesión debe estar permitida.

A Claude Code se le devuelve a una dirección de loopback, así que no necesita ningún ajuste. Según su documentación, algunos editores registran una dirección web además de una de loopback: Cursor registra `https://www.cursor.com/agents/mcp/oauth/callback` y VS Code usa `https://vscode.dev/redirect`. Un cliente así solo puede registrarse cuando su dirección web figura en `BOE_INSIGHTS_ALLOWED_REDIRECT_URIS`. Incluirla significa que los códigos de inicio de sesión de ese cliente pueden entregarse a través de ese sitio. Consulta las páginas de [Cursor](clients/cursor.md#con-inicio-de-sesión-con-github) y [VS Code](clients/vscode.md#con-inicio-de-sesión-con-github).

¿Usas un contenedor? Pasa las mismas variables con `-e` o `--env-file`. Consulta [Ejecutar en un contenedor](container.md). El inicio de sesión con la imagen de contenedor no se probó.

## La lista de acceso

`BOE_INSIGHTS_ALLOWED_USERS` admite entradas separadas por comas. Cada entrada es una de estas:

| Entrada | Significado |
|---|---|
| `octocat` | Un nombre de usuario de GitHub. No distingue mayúsculas de minúsculas. |
| `id:1234567` | Una cuenta de GitHub por su id numérico. |
| `*` | Todas las cuentas de GitHub. Esto ya no es una lista, así que úsalo solo si de verdad es lo que quieres. |

Una entrada incorrecta detiene el servidor al arrancar con un mensaje que muestra la entrada.

**Usa preferentemente entradas `id:`.** Una entrada con nombre de usuario deja de coincidir si la persona cambia el nombre de su cuenta, y otra persona puede quedarse después con el nombre antiguo y coincidir entonces con la entrada. El id numérico no cambia nunca. Cuando una entrada con nombre de usuario deja entrar a alguien, el servidor escribe una sola vez en el log que `id:<n>` es la forma que sobrevive a un cambio de nombre.

Para averiguar el id de una persona, pídele que intente iniciar sesión. Verá la página "Access not allowed" que se describe más abajo, en la que aparece el id. También puedes consultarlo en la API pública de GitHub: `curl https://api.github.com/users/<login>` devuelve un campo `id`.

### Añadir o quitar a una persona

1. Edita el valor de `BOE_INSIGHTS_ALLOWED_USERS`.
2. **Reinicia el servidor.** La lista se lee una sola vez, al arrancar. Un cambio no se aplica a un servidor en marcha.

Después de reiniciar:

- Una persona que hayas añadido puede iniciar sesión.
- A una persona que hayas quitado se le rechaza en su siguiente petición. Su cliente recibe "401 Unauthorized" y no puede renovar su token, aunque tenga uno que era válido antes. Un nuevo intento de inicio de sesión le muestra la página "Access not allowed".
- Quitar a una persona no borra sus búsquedas guardadas. Nada en el servidor lo hace.

## Qué ve un usuario

La primera vez que un cliente se conecta a `http://localhost:8000/mcp` (o a tu dirección pública):

1. El servidor responde que hace falta iniciar sesión y el cliente abre una ventana del navegador. Cómo se ve depende del cliente. Consulta la [página de tu cliente](clients/claude-code.md).
2. La página de consentimiento del propio servidor pregunta si se permite el cliente. Esta página procede del servidor, no de GitHub. Muestra el logotipo de este proyecto y aparece en cada inicio de sesión.
3. El navegador va a GitHub. Solo la primera vez, GitHub muestra su página de autorización para tu aplicación OAuth.
4. GitHub devuelve el navegador al servidor y el cliente queda con la sesión iniciada.

El servidor pide a GitHub como mucho el ámbito (scope) `read:user`, que es acceso de solo lectura al perfil público, y ningún ámbito cuando el cliente no pide ninguno. El servidor no necesita más que el id numérico de la cuenta y su nombre de usuario. No pide acceso a repositorios ni a nada más.

A partir de ahí el cliente conserva su token y lo renueva por sí mismo. Las búsquedas guardadas de cada persona se almacenan bajo su id de GitHub y las demás personas no pueden verlas.

### Alguien que no está en la lista

Una cuenta de GitHub que no está en la lista de acceso no pasa. El navegador muestra una página titulada "Access not allowed" con el mensaje de que la cuenta de GitHub no tiene permiso para usar este servidor y de que la persona debe pedir al administrador del servidor que añada `id:<n>` (seguido de su nombre de usuario de GitHub). La página no dice quién está en la lista. El estado es 403.

Envía el texto `id:<n>` al administrador, que lo añade a la lista como se explica en [Añadir o quitar a una persona](#añadir-o-quitar-a-una-persona).

## Renovación del token

Con "Expire user access tokens" activado, los tokens propios de GitHub caducan. El diseño del servidor permite que un cliente renueve su token sin volver a iniciar sesión y rechaza la renovación a una persona que se ha quitado de la lista. Todavía no se ha observado una renovación real después de que un token de GitHub haya caducado de verdad. Si un cliente te pide que inicies sesión de nuevo, hazlo.

## Comprobarlo sin un cliente

Con el servidor en marcha como se ha explicado, una petición sin autenticar se rechaza:

```bash
curl -i -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}'
```

La respuesta es `401 Unauthorized` con una cabecera `WWW-Authenticate` que apunta a los metadatos de inicio de sesión. `/health` sigue respondiendo sin iniciar sesión.

## Qué se probó

- El inicio de sesión desde Claude Code por HTTP en el mismo equipo (`localhost`), con una aplicación OAuth de GitHub, con "Expire user access tokens" activado, con una cuenta permitida y otra rechazada.
- Las comprobaciones de arranque del servidor, la respuesta 401 y los metadatos de inicio de sesión, con credenciales ficticias.

No se probó:

- Detrás de un proxy inverso HTTPS real.
- Clientes distintos de Claude Code.
- El inicio de sesión con la imagen de contenedor.
- La renovación después de que el token de GitHub haya caducado de verdad.
- Que un cliente, cualquiera que sea, vuelva a registrarse después de borrar los datos de inicio de sesión del servidor.

## Véase también

- [Ejecutar por HTTP](http.md)
- [Seguridad y operación](../operating.md)
- [Conectar Claude Code](clients/claude-code.md)
