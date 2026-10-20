# Plan de encaminamiento de Alexandria

## 1. Qué es

Biblioteca de **skills** y **grounding** para agentes de IA. Cada elemento es un paquete `.zip` versionado: `SKILL.md` si es procedimental, `INDEX.md` si es referencial, más los artefactos que haga falta. Alexandria almacena el zip tal cual e indexa solo lo relevante para descubrirlo. Se consulta por MCP y por REST, y se descarga. La autenticación es un protocolo único y sencillo: `Authorization: Bearer` con un JWT o un token de API.

La especificación detallada es `docs/PACKAGE_CONTRACT.md`. Este documento es el plan para llegar a ella.

## 2. Punto de partida (diagnóstico)

El código existente es un registro de *endpoints de servicios* (versiones con `capabilities`/`endpoints` en JSON), no una biblioteca de paquetes. Se reutiliza el esqueleto, no el dominio.

| Pieza | Estado |
|---|---|
| Paquete `.zip`, `kind`, validación, manifiesto, hash, descarga | No existe |
| Servidor MCP | Inoperante: `app/mcp/server.py` ni se monta ni registra herramientas, y además **falla al importar** con `mcp` 2.x (`FastMCP` pasó a `MCPServer`) |
| Búsqueda | Filtra en Python por nombre y aplica `limit` antes de filtrar |
| Auth | JWT + usuarios + roles funcionan; no hay tokens para agentes |
| Configuración | `LOGGING_LEVEL` no surte efecto; las `BaseSettings` anidadas leen variables de entorno por nombre de campo (un campo `path` leería el `PATH` del sistema) |
| Enums | El ORM guarda nombres en mayúsculas y la migración declara valores en minúsculas: rompería en PostgreSQL |
| Tests iniciales | 7 en rojo y 54 avisos de ruff (por versiones nuevas de FastAPI/Starlette); **ya corregidos** en M0 |

Entorno: Python 3.14, venv en `.venv` creado con `uv`, no hay repositorio git (la instantánea previa está en el scratchpad de la sesión).

## 3. Decisiones

| # | Decisión | Motivo |
|---|---|---|
| D1 | Una entidad con dos `kind` (`skill`, `grounding`), deducidos del punto de entrada | Es la distinción de "ser" que define el producto |
| D2 | Se conserva el nombre `Skill`/`skills` en código, BD y URLs | Evita un renombrado masivo; es mecánico hacerlo después |
| D3 | El zip es opaco y se guarda tal cual, direccionado por sha256; la BD guarda el índice | Principio rector del contrato |
| D4 | Versiones inmutables con estados `draft`/`published`/`deprecated` | Los agentes pueden fijar una versión sin que cambie debajo |
| D5 | Un solo esquema de credenciales: JWT o token de API `alx_…` con ámbito `read`/`write` | Un agente lector no debería poder escribir |
| D6 | MCP: solo herramientas, Streamable HTTP en `/mcp`, SDK `mcp` 2.x | Las herramientas son lo que todos los clientes soportan |
| D7 | Búsqueda portable con `LIKE` y puntuación; FTS queda para después | Debe funcionar igual en SQLite y PostgreSQL |
| D8 | Se reemplaza el esquema con la migración `002` | La BD de partida está vacía |
| D9 | Excepciones de dominio (`DomainError`) traducidas por un manejador global | Cumple la regla de AGENTS.md y quita los `try/except` de cada ruta |

## 4. Hitos

| Hito | Contenido | Criterio de salida |
|---|---|---|
| **M0** | Línea base: venv, dependencias en `pyproject.toml`, tests y ruff en verde, contrato y plan | `pytest` 80 passed, `ruff` limpio (hecho) |
| **M1** | Núcleo puro: errores de dominio, semver, inspector de paquetes, almacén de blobs, configuración | Tests de ingesta y seguridad en verde |
| **M2** | Modelo de datos: `kind`, versión-paquete, etiquetas, tokens, migración `002`, repositorios con búsqueda | Migración sube y baja en SQLite; tests de repositorio |
| **M3** | Servicios: subida, versiones y `latest`, visibilidad, lectura de archivos, descarga, tokens, resolución de principal | Tests de servicio; cobertura de `app/services` ≥ 85 % |
| **M4** | Adaptadores: REST, MCP real, CLI `alx` y paquetes de ejemplo | Cada adaptador con sus tests |
| **M5** | Integración: MCP montado en `/mcp`, paridad REST/MCP, prueba de extremo a extremo con servidor real | Suite completa en verde |
| **M6** | Interfaces: administración (Svelte) y sitio público (Jinja) | Build de `admin` y tests de páginas |
| **M7** | Endurecimiento: deudas de seguridad y despliegue de `CLAUDE.md` | Arranque falla sin secreto; seed sin clave fija |
| **M8** | Documentación al día: README, ARCHITECTURE, AGENTS, DEPLOY, Debts, CLAUDE.md | Sin referencias al modelo antiguo |
| **M9** | Revisión final multi-perspectiva y corrección | Sin hallazgos confirmados abiertos |

## 5. Cómo se ejecuta (workflows)

No hay git, así que no se puede aislar a los agentes en worktrees. Para poder paralelizar, **cada tarea tiene propiedad exclusiva de archivos**; lo que necesite de otro archivo lo declara en `needs_from_others` y lo resuelve la fase siguiente. Se ejecutan seis workflows pequeños en serie, y entre uno y otro se verifica el estado real (pytest, ruff, build) en vez de fiarse del informe de los agentes.

| Workflow | Hitos | Agentes |
|---|---|---|
| W1 Fundación | M1 y M2 en paralelo (inspector; almacén y configuración; modelo y migración) y verificación cruzada | 3 en paralelo + verificación y corrección |
| W2 Servicios | M3: catálogo y paquetes por un lado, tokens y autenticación por otro; revisión con tres perspectivas (seguridad de ingesta, autorización y visibilidad, versionado) y triaje adversarial | 2 + 3 revisores + triaje + corrección |
| W3 Adaptadores | M4: REST, MCP y CLI con ejemplos en paralelo | 3 + verificación |
| W4 Integración | M5: montaje de MCP, paridad y extremo a extremo | integrador + verificación |
| W5 Interfaces y endurecimiento | M6 y M7 en paralelo | 3 + verificación |
| W6 Cierre | M8 en paralelo con la revisión final M9 (seguridad, corrección, paridad y reglas de arquitectura), triaje adversarial y corrección | docs + 4 revisores + triaje + corrección |

Reglas comunes de todos los agentes: leer el contrato antes de empezar, no comentar el código, no tocar archivos ajenos, ejecutar de verdad tests y ruff y reportar el resultado literal.

## 6. Riesgos

| Riesgo | Mitigación |
|---|---|
| API de `mcp` 2.x distinta de lo documentado en ARCHITECTURE.md | El agente de MCP lee el código instalado; el montaje en `/mcp` se prueba con un cliente MCP real |
| Zips maliciosos (zip-slip, bombas, símbolos, YAML con alias) | Reglas del contrato §1.4 y batería de pruebas de seguridad obligatoria |
| HTML o JS del paquete servido desde el origen del registro (XSS contra la sesión de administración) | Contrato §9.2: archivos siempre como texto plano o descarga, con CSP y `nosniff` |
| Migración en SQLite (no soporta `ALTER` completo) | `batch_alter_table` y prueba de subida y bajada |
| Agentes paralelos pisándose | Propiedad exclusiva de archivos y verificación cruzada tras cada fase |
| Sin git para revertir | Instantánea previa en el scratchpad; cada workflow deja la suite en verde antes del siguiente |
| Entradas almacenadas con descriptor de datos: solo el descriptor delimita su fin para un extractor en flujo (puede simularse un descriptor falso dentro de los datos) | Se acepta en v1 porque los escritores en flujo de Python lo producen; si se sirven paquetes a extractores en flujo, rechazar descriptores falsos (firma `PK\x07\x08` seguida del CRC de los datos previos) |

## 7. Fuera de alcance de esta ejecución

FTS, rate limiting, borrado de versiones y recolección de blobs, firmas, backend S3, organizaciones, OAuth/IdentityServer, recursos MCP, rangos semver, Dockerfile, compose y CI. Siguiente iteración sugerida: Docker y CI, recolección de blobs, FTS5/PostgreSQL `tsvector`, límites de tasa.

## 8. Desviaciones y decisiones aceptadas

| # | Hito | Desviación o interpretación | Motivo |
|---|---|---|---|
| 1 | M1 | `ValidationError` puede llevar `reason` (p. ej. `INVALID_PATH`); REST lo serializa si no es nulo | El contrato §9.2 lo exigía sin que §6.1 lo permitiera |
| 2 | M1 | `server.reload` sigue en `true` hasta M7 | Era alcance de endurecimiento, no de fundación |
| 3 | M1 | `media_type` con tabla propia en lugar de `mimetypes.guess_type` global | Resultados idénticos en cualquier máquina |
| 4 | M1 | Rechazos extra del inspector: claves YAML duplicadas, anidamiento de flujo > 32, entrypoint no UTF-8 o con NUL, nombres con NUL/Unicode Path/bidi/controles/prefijo de unidad, colisiones NFC/mayúsculas, archivo usado también como directorio, cabeceras locales compartidas, datos antes de la primera entrada o tras el registro final, directorio central desproporcionado | Cierran vectores encontrados al atacar el inspector (YAML cuadrático, amplificación del directorio central, diferenciales de analizador) |
| 5 | M1 | Frontmatter vacío → `MISSING_FIELD(name)`; `NOT_A_ZIP` solo si no hay firma PK ni registro final (si no, `CORRUPT_ARCHIVE`); versión inválida pasada como argumento → `ValidationError` 422 | Python 3.14 ya no distingue ambos casos en `is_zipfile` |
| 6 | M1 | Máximo de 20 tags tras deduplicar; `display_name` por defecto con capitalizado por palabra | Coherencia con la deduplicación |
| 7 | M1 | Todas las secciones de configuración con `extra=forbid`: un `config.yml` antiguo con `registry.default_skill_status` falla al arrancar | Evita claves silenciosamente ignoradas |
| 8 | M1 | `Settings.load(config_path, env, dotenv_path)`, `BlobIntegrityError`, secretos fuera de `repr`/`model_dump` | Necesario para probar `.env` y evitar fugas en logs |
| 9 | M2 | Puntuación §9.1: niveles de nombre excluyentes, resto aditivo; etiqueta filtra por subcadena y puntúa por coincidencia exacta; `q` limitado a 10 términos | El contrato era ambiguo (ya aclarado en §9.1) |
| 10 | M2 | Repositorios sin `commit`; el servicio es dueño de la transacción | Casos de uso atómicos (ya recogido en §6) |
| 11 | M2 | `001_initial` corregido a `server_default=sa.true()` | `DEFAULT 1` en una columna booleana rompe PostgreSQL |
| 12 | M3 | Las entradas ignoradas (`__MACOSX/` …) se validan en metadatos, el zip debe estar teselado y el directorio central se recorre registro a registro hasta el registro de fin | Hallazgo de la verificación de W1; el zip se sirve tal cual (ya recogido en §1.2 y §1.4) |
| 13 | M3 | Los flujos deflate deben ocupar exactamente su extensión (válidos, con bloque final y sin bytes sobrantes). Los directorios y las entradas ignoradas, que no se leen como archivo, se inflan y descartan para comprobarlo (presupuesto fijo de 64 MiB compartido por las ignoradas; un directorio debe inflar a 0 bytes). Desviación de «nunca se leen» del contrato §1.2: ya no se acepta una entrada ignorada con un flujo roto o una bomba | Un flujo deflate con datos tras su bloque final, o sin él, oculta cabeceras locales a `zipfile` y al directorio central pero no a un extractor en flujo guiado por el final del flujo; exigirlo solo a los archivos dejaba las entradas ignoradas y los directorios como escondite (hallazgo de la revisión de W2a) |
| 14 | M3 | `AuthService` devuelve `UserResponse` (nunca la fila ORM) y `UserResponse.id` pasa de `uuid.UUID` a `str` | Regla de los DTOs (§7.1); todos los ids del sistema, `Principal.user_id` y las filas ORM son `str`. Cierra la desviación que S2 había dejado sin anotar |
| 15 | M3 | `user_repo.create_user` y `deactivate_user` hacen `flush` y `refresh`, no `commit`; el servicio confirma. `scripts/seed.py` confirma ahora de forma explícita | Decisión 10: casos de uso atómicos (p. ej. crear un usuario y su primer token) |
| 16 | M3 | Publicar, deprecar, restaurar y subir a una entrada existente empiezan bloqueando la fila de la entrada (`SELECT … FOR UPDATE` con `populate_existing`); `list_versions` relee con `populate_existing`; la guarda de la única versión publicada se evalúa dentro de la transacción, tras escribir | Dos deprecaciones concurrentes de las dos últimas versiones publicadas pasaban la guarda sobre filas obsoletas del mapa de identidad y dejaban la entrada `published` con `latest_version` apuntando a una versión `deprecated` (también en SQLite) |
| 17 | M3 | Las contraseñas locales tienen un máximo de 1024 caracteres (`MAX_PASSWORD_LENGTH`): el login las rechaza sin hashear, igual para usuarios existentes e inexistentes; `AuthService.create_user` lanza `ValidationError`; `UserCreate.password` y `LoginRequest.password` llevan `max_length`. La verificación del hash señuelo y la del usuario real comparten camino de código, con el mismo `except ValueError` | passlib lanza `PasswordSizeError` (un `ValueError`) por encima de 4096 caracteres. Con el señuelo fuera del `try`, el login de un usuario inexistente respondía 500 y el de uno existente 401: un oráculo de enumeración de usuarios que el hash señuelo debía evitar (hallazgo de la revisión de W2a, ronda 2). El contrato no fijaba ningún máximo |
| 18 | M3 | Toda conexión SQLite registra una función `lower()` con plegado Unicode (`str.lower()`) mediante `configure_sqlite_engine` (`app/db/session.py`), usada por el motor de la aplicación y por los motores de los tests que consultan el catálogo. La búsqueda sigue siendo `lower(columna) LIKE término` | El `lower()` y el `LIKE` de SQLite solo pliegan ASCII: un texto con mayúsculas no ASCII (`Árbol`, `ÉCOLE`, `Ñandú`) no se encontraba nunca (contrato §9.1). Coste medido: una llamada a Python por fila y columna; un barrido completo con 1 000 entradas y 3 KB de texto pasa de 5-35 ms a 17-88 ms, y con 5 000 entradas y 15 KB de 50-240 ms a 330-1 540 ms. Sustituirlo por una columna plegada persistida o por FTS5 queda para la siguiente iteración |
| 19 | M3 | Las lecturas truncadas (`read_file` y `entrypoint` sobre un texto mayor que `max_inline_read_bytes`) solo comprueban la longitud del prefijo leído; el sha256 del manifiesto se verifica en `open_raw`, en la descarga y en las lecturas completas | Verificar el sha256 de un archivo de hasta 50 MiB para servir 256 KiB es desproporcionado; alterar el blob exige acceso al disco |
| 20 | M3 | La gestión de usuarios pasa a un `UserService` que recibe `Principal` y exige administrador con ámbito `write`; `AuthService.create_user` queda como vía interna para el seed y los tests | La regla de autorización debe vivir en los servicios (AGENTS.md), no depender de que cada ruta recuerde comprobarla |
| 21 | M3 | `ref` y `version` con texto no codificable en UTF-8 → `NotFoundError`; `q`, `tags` y `category` → `ValidationError`; `path` ya era `ValidationError(INVALID_PATH)` | Esas cadenas no pueden existir; antes llegaban a la BD y lanzaban `UnicodeEncodeError` |
| 22 | M3 | El hash señuelo de bcrypt se calcula al importar `app.auth.local` y al construir el proveedor (0,3–0,8 s una vez); el hashing y la verificación corren en `asyncio.to_thread`, en el ejecutor por defecto compartido con el almacén y el inspector | Evita bloquear el bucle de eventos y el oráculo de enumeración del primer login desconocido |
| 23 | M3 | `UserService.list` devuelve `list[UserResponse]` sin paginar | Las cuentas son pocas; paginar queda para cuando haga falta |
| 24 | M3 | `skill_repo.list_versions` es ligero (`manifest`, `search_text` y `metadata` diferidos): para leerlos hay que pasar por `get_version`, `get_version_search_text` o `catalog.get_latest_version`, que ahora hace dos consultas | Las lecturas de listas y fichas cargaban todos los manifiestos de todas las versiones |
| 25 | M3 | El ancho del descriptor de datos (4 u 8 bytes) se deduce de la longitud del hueco y no del extra ZIP64 de la cabecera local | Exigir coherencia con el local rechazaría escritores reales (Go, Java a partir de 4 GiB) |
| 26 | M3 | Un admin no puede desactivar su propia cuenta | Evita dejar el registro sin administradores |
| 27 | M4 | `BlobStore` gana `open(key)`, que devuelve un manejador binario con búsqueda aleatoria (en `LocalBlobStore`, el archivo abierto en modo lectura) y que cierra quien lo pide. `entrypoint`, `read_file` y la lectura de un archivo (`raw_file`, que pasa a `open_raw`) dejan de cargar el zip entero: el inspector abre el miembro sobre ese manejador (`open_package_member`, `PackageMember`) con las mismas comprobaciones que `read_package_file`, que sigue existiendo para bytes en memoria; una lectura completa en línea nunca lee más bytes que el tamaño del manifiesto, aunque el zip declare más. `open_raw` devuelve `RawFile(path, media_type, text, size, stream)`: una primera pasada infla el archivo por trozos, los descarta y comprueba longitud y sha256 contra el manifiesto, de modo que un almacén dañado responde 500 `STORAGE_ERROR` antes del primer byte; el flujo reabre el paquete en su primera iteración (nada queda abierto si no se consume), vuelve a comprobar mientras emite y nunca entrega más de `size` bytes. `GET …/raw/…` responde con un flujo y `Content-Length` del manifiesto. El coste es una segunda inflada (CPU, no memoria) | `raw_file` cargaba el zip (hasta 25 MiB) e inflaba el archivo entero (hasta 50 MiB) dos veces en memoria: con `public_read` era una amplificación de memoria cercana a 2000:1 sin autenticación (24 lecturas concurrentes de un archivo de 49 MiB llevaron el proceso de 331 a 1890 MiB). Ahora cada petición usa del orden de 1 MiB sea cual sea el tamaño del archivo (24 lecturas concurrentes: de 100 a 140 MiB) y leer el punto de entrada de un paquete de 20 MiB lee menos de 256 KiB. No se añadió el semáforo ni el tope de tamaño que se proponían como medida provisional |
| 28 | M4 | `AuthService.get_account(principal)` devuelve el `UserResponse` de la cuenta (`UnauthorizedError` sin principal o si la fila no existe o está inactiva) y `GET /auth/me` solo lo llama. `ClosingStreamingResponse` (`app/api/responses.py`) cierra siempre el iterador del cuerpo, también cuando el cliente se desconecta, y la usan `…/raw/…` y `…/download`. `tests/test_api/test_architecture.py` vigila que `app/api` no importe repositorios, modelos ORM ni más SQLAlchemy que `AsyncSession` | `read_me` consultaba el repositorio y decidía el 401 en el adaptador (regla 1 de AGENTS.md y §7.1). Starlette no cierra el generador de un cuerpo abandonado, así que el descriptor del archivo quedaba abierto hasta que el recolector de ciclos lo liberaba |
| 29 | M4 | Las rutas con cuerpo que exigen `write` (`POST /packages`, `/tokens`, `/users`) responden 401/403 antes de leer o validar el cuerpo (dependencia `WriterPrincipal`, que llama a `access.require_write`; el servicio repite la comprobación) | No volcar a disco 25 MiB de un no autorizado y dar 401 antes que 422 |
| 30 | M4 | `POST /packages` lee el multipart a mano (`request.form` con `max_files=1`, `max_fields=8`, `max_part_size=16 KiB`) y un middleware ASGI limita el cuerpo: subida `max_package_bytes` + 64 KiB, resto 1 MiB, `/mcp` hasta `max(base64(max_package_bytes) + 64 KiB, 1 MiB)`; el rechazo es 413 con `Connection: close` | Starlette vuelca todo el multipart a disco antes de llamar a la ruta |
| 31 | M4 | La sesión de BD de REST es `Depends(get_db, scope="function")` y se cierra antes de emitir el cuerpo; el pool se configura con `database.pool_size`, `max_overflow` y `pool_timeout`; con el pool agotado el error sale como 500 | Las descargas lentas no deben agotar el pool |
| 32 | M4 | `HTTPException` y los errores de validación usan el formato de error del contrato con códigos adicionales (`BAD_REQUEST`, `METHOD_NOT_ALLOWED`, `UNSUPPORTED_MEDIA_TYPE`, `HTTP_<n>`); los 5xx ocultan su detalle | Formato único de error |
| 33 | M4 | MCP: `publish=false` significa «seguir el defecto del registro»; los errores de validación de argumentos salen con el texto del SDK (`Error executing tool …`), no como `CODE: mensaje`; el SDK anuncia capacidades `prompts` y `resources` vacías; `GET` y `DELETE` en `/mcp` responden 405 (en modo sin estado un `GET` abre un flujo SSE que no termina); la protección DNS-rebinding del SDK está desactivada y la política de hosts pasa a la aplicación (M7); `structured_content` de `list_tags` y `list_categories` va en `{"result": [...]}` y esas herramientas admiten `limit` hasta 200 | Límites del SDK 2.x y del transporte Streamable HTTP sin estado |
| 34 | M4 | `alx`: la validación local usa los límites por defecto; `pack` es determinista por intérprete (depende de zlib); `pull` es transaccional, sin symlinks y sin sobrescribir sin `--force`; respuestas cortas limitadas a 16 MiB; rechaza URLs con credenciales y no sigue redirecciones; el SQLite de ejemplo vive en `db/` | `.gitignore` ignoraba cualquier directorio `data/` (ahora está anclado a `/data/`) |
| 35 | M6 | Sitio público: Markdown `commonmark` con `html=False` más tablas y tachado (alineación por clases, no `style`); las imágenes se sustituyen por su texto alternativo; solo se enlazan http, https, mailto y archivos del manifiesto; CSP estricta, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`; más de 20 `tag` o una consulta de más de 8 KiB dan 400; soporta `HEAD`; el render tiene presupuesto de CPU, hilo único y caché LRU; textos de interfaz en inglés | Contrato §9.2 y coste de render de Markdown patológico |
| 36 | M5 | MCP: el servidor no atiende `subscriptions/listen` (`MCPServer(subscriptions=False)`): la petición responde «Method not found» (`-32601`) al instante y ninguna capacidad anuncia `listChanged` ni `subscribe`, ni con el protocolo 2025-11-25 ni con el 2026-07-28 (en este último el SDK las anunciaba verdaderas). `run()` pasa a uvicorn `timeout_graceful_shutdown=20` (`GRACEFUL_SHUTDOWN_SECONDS`); el comando `uvicorn app.main:app` no pasa por `run()` y necesita `--timeout-graceful-shutdown` para tener el mismo tope | El SDK sirve `subscriptions/listen` como un flujo `text/event-stream` que no termina aunque el transporte use `json_response=True` (es la única petición que no responde en JSON), lo que contradecía el contrato §8 y la desviación 33, que solo cerró el `GET`. Medido contra `uvicorn app.main:app` real: con un cliente que mantenía `client.listen()` abierto, SIGTERM no detenía el servidor (seguía vivo a los 20 s en la reproducción y a los 100 s en la medición de la verificación; uvicorn queda en «Waiting for connections to close» porque el apagado de la aplicación llega después de que se cierren las conexiones) y 100 flujos anónimos costaban 100 descriptores de archivo sin caducar nunca. Las herramientas son fijas, así que no hay cambios que notificar. El tope de `run()` es defensa en profundidad: una petición con el cuerpo a medias también bloquea el apagado (más de 25 s sin tope; con él, 20 s) |
| 37 | M5 | `tests/conftest.py` hace `gc.collect()` y `gc.freeze()` al terminar la recolección de tests (`pytest_collection_finish`) y `tests/test_fixtures_isolation.py` vigila que haya objetos congelados. No se tocó ningún tope de tiempo | Con unos 9 100 tests recolectados el proceso rastrea unos 490 000 objetos y una recolección completa del GC cíclico cuesta unos 0,2 s sin carga (más con la máquina ocupada). Si una cae dentro de una medición, `tests/test_api/test_public.py::test_heading_floods_render_in_a_fraction_of_a_second_as_plain_text` supera su tope de 0,5 s: al añadir 24 tests a la recolección, la recolección del GC pasó a caer dentro de la medición de forma determinista (0,27 a 0,51 s frente a 0,04 a 0,05 s sin los 24 tests o con el GC desactivado). Con el montón congelado la misma medición da 0,08 a 0,10 s |
| 38 | M5 | `setup_logging` fija el logger `mcp.server.streamable_http` en `max(nivel, WARNING)`. Los fixtures `mcp_server`, `mcp_connect` y `mcp_client` entran en `mcp_lifespan()` en una tarea dedicada. `tests/helpers/live.py` arranca uvicorn con `PR_SET_PDEATHSIG` (Linux) | El SDK escribe una línea INFO por petición; anyio exige entrar y salir del lifespan en la misma tarea; evita servidores huérfanos si pytest muere |
| 39 | M5 | La matriz de autorización REST/MCP usa JSON-RPC crudo, porque el 401 ocurre antes de poder inicializar el cliente del SDK. `get_download_info` se compara contra `VersionDetail`, `GET /skills/{ref}` y el `Cache-Control` de la descarga. Con `public_read=false` el mensaje del 401 difiere entre REST y MCP (mismo `code`) | No existe un DTO REST equivalente a `get_download_info` |
| 40 | M5 | OpenAPI: las lecturas con acceso anónimo declaran `security: [{}, {BearerAuth: []}]` y las rutas de escritura y admin (ocultar, restaurar, publicar, deprecar, tokens, usuarios) el requisito obligatorio, declarado por ruta con `openapi_extra`. El 500 `STORAGE_ERROR` está documentado en las operaciones que leen blobs | Los clientes generados y Swagger UI no deben exigir token en lecturas públicas |
| 41 | M5 | `HEAD` en `/health`, `/info` y `…/download` (fuera del esquema OpenAPI; el de `download` devuelve cabeceras sin leer el blob). `…/versions/latest/download` usa `Cache-Control: public, no-cache` con `ETag`; las versiones exactas siguen en `public, immutable` | `latest` cambia al publicar otra versión |
| 42 | M5 | El 413 del transporte MCP sale con el JSON de error de la API; el limitador del SDK se desactiva (`SDK_BODY_LIMIT = 1 << 62`) y la pasarela es la única autoridad; el límite del escritor se evalúa por petición | Un solo formato de error |
| 43 | M5 | Caché de contenido en proceso (32 MiB por almacén) para el cuerpo del punto de entrada, el manifiesto parseado y las lecturas de texto en línea. Solo se cachea contenido ya verificado, así que nunca está obsoleto (las versiones son inmutables), pero un blob perdido después sigue sirviendo lo cacheado hasta que se expulse o se reinicie el proceso | Evita releer el zip en cada vista del sitio público |
| 44 | M5 | `split_frontmatter` es público en el inspector y el sitio público lo usa | Eliminaba expresiones regulares duplicadas |
| 45 | M5 | `uvicorn.run` (comando `alexandria`) usa `timeout_graceful_shutdown=20`; con el CLI `uvicorn app.main:app` en producción hay que pasar `--timeout-graceful-shutdown 20` | Una petición a medias bloquea SIGTERM indefinidamente |

Pendientes anotados por W1 que se atienden más adelante: `PRAGMA foreign_keys=ON` en SQLite (W2), construcción explícita de `PackageLimits` desde `registry.limits` (W2), prueba real de la rama PostgreSQL de `002` (M7), `reload=false` (M7), ejecución de la suite en Python 3.11–3.13 (M9).
