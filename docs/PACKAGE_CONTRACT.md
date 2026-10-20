# Contrato del paquete y de la biblioteca (v1)

Documento normativo. Si el código contradice este documento, se corrige el código. Una desviación solo es válida si queda anotada en `docs/PLAN.md` (sección "Desviaciones") con su motivo.

## 0. Vocabulario

| Término | Significado |
|---|---|
| **Paquete** | Un archivo `.zip` con un punto de entrada y artefactos libres. |
| **Entrada** | Elemento del catálogo con nombre único. En código, tabla y URLs se llama `Skill` / `skills` aunque su `kind` sea `grounding`. |
| **Versión** | Un paquete inmutable asociado a una entrada (`SkillVersion`). |
| **kind** | `skill` (procedimental, punto de entrada `SKILL.md`) o `grounding` (referencial, punto de entrada `INDEX.md`). |
| **Principal** | Identidad autenticada: usuario más ámbito del token. El anónimo es `None`. |

Principio rector: **el zip es opaco y es la fuente de verdad; Alexandria solo extrae lo necesario para descubrir, validar y servir.** Nunca ejecuta nada del paquete.

## 1. Formato del paquete

### 1.1 Archivo y punto de entrada

- Un `.zip` con exactamente un punto de entrada en la raíz del paquete: `SKILL.md` o `INDEX.md` (nombre exacto, sensible a mayúsculas).
- `kind` se deduce del punto de entrada: `SKILL.md` → `skill`, `INDEX.md` → `grounding`.
- Si la raíz contiene ambos → `AMBIGUOUS_ENTRYPOINT`. Si no contiene ninguno → `MISSING_ENTRYPOINT`. Los `SKILL.md` / `INDEX.md` en subcarpetas son archivos normales.
- El resto del contenido es libre: scripts, tablas, bases SQLite, HTML, especificaciones, binarios.

### 1.2 Directorio envoltorio y entradas ignoradas

- Entradas ignoradas: cualquier cosa bajo `__MACOSX/`, y archivos llamados `.DS_Store` o `Thumbs.db`. No entran al manifiesto, no cuentan para los límites de bytes ni de archivos y su contenido nunca se lee como archivo (ni se hashea ni se comprueba su CRC), pero **deben ser seguras**: se les aplican las comprobaciones de metadatos de la sección 1.4 (nombre ambiguo o con NUL, `UNSAFE_PATH`, symlink, cifrado, compresión no soportada) y la comprobación de extensión de los flujos deflate (sección 1.4), porque el zip se sirve tal cual y un extractor ingenuo del consumidor las materializaría. Para esa comprobación un flujo deflate ignorado se infla y se descarta, con un presupuesto fijo de 64 MiB inflados compartido por todas las entradas ignoradas (independiente de los límites del paquete); superarlo es `TOO_LARGE`.
- Si, descontando las ignoradas, **todas** las entradas cuelgan de un único directorio `D/` y no hay punto de entrada en la raíz del zip pero sí en `D/`, entonces `D/` es el envoltorio: la raíz del paquete es `D/` y los paths del manifiesto se calculan relativos a ella. Solo se quita un nivel.
- Si hay un punto de entrada en la raíz del zip no hay envoltorio, aunque existan subcarpetas.
- La versión guarda `root_prefix` (`""` o `"D/"`) para localizar archivos dentro del zip original, que se almacena tal cual lo subió el autor.

### 1.3 Frontmatter del punto de entrada

El punto de entrada empieza (tras un BOM opcional) con un bloque YAML entre líneas `---`. El resto es el cuerpo Markdown.

- Tamaño máximo del bloque: 16 KiB. Se parsea con `yaml.safe_load`.
- **Se rechazan anclas y alias YAML** (`&`, `*`): antes de cargar se recorre el flujo de eventos con `yaml.parse` y se rechaza si aparece un `AnchorEvent`/`AliasEvent` (evita expansión tipo "billion laughs"). `RecursionError` y cualquier `yaml.YAMLError` se convierten en `BAD_FRONTMATTER`.
- Debe ser un mapa con claves de tipo string.

| Campo | Req. | Regla |
|---|---|---|
| `name` | sí | `^[a-z0-9]+(?:-[a-z0-9]+)*$`, 1 a 64 caracteres. No puede tener forma de UUID. |
| `description` | sí | string, tras `strip` entre 1 y 1024 caracteres. |
| `version` | condicional | string semver 2.0.0 sin metadatos de build (`+`). Requerido salvo que se indique al subir. Si aparece en ambos sitios deben coincidir (`VERSION_MISMATCH`). Un número YAML (`1.0`) es `INVALID_FIELD` con indicación de entrecomillarlo. Máximo 50 caracteres. |
| `display_name` | no | string de hasta 255. Por defecto: `name` con guiones como espacios y Title Case. |
| `tags` | no | lista de strings. Se normalizan con `strip` y minúsculas; patrón `^[a-z0-9][a-z0-9._+-]{0,31}$`; máximo 20; sin duplicados (se conserva el orden). Un valor inválido es `INVALID_FIELD`. |
| `category` | no | slug `^[a-z0-9][a-z0-9-]{0,63}$` tras `strip` y minúsculas. Texto libre, sin taxonomía cerrada. |
| `kind` | no | si aparece debe ser `skill` o `grounding` y coincidir con el punto de entrada (`KIND_MISMATCH`). No se guarda en `metadata`. |
| cualquier otro | no | se conserva en `metadata` (JSON). `date`/`datetime` se serializan a ISO 8601; otros tipos no JSON se convierten con `str`. |

Para indexar se guarda `search_text`: el cuerpo Markdown sin frontmatter, recortado a 20 000 caracteres.

### 1.4 Seguridad del archivo y límites

Todos los límites son configurables (sección 5). Valores por defecto:

| Límite | Defecto |
|---|---|
| `max_package_bytes` (zip subido) | 25 MiB |
| `max_uncompressed_bytes` (suma real descomprimida) | 100 MiB |
| `max_file_bytes` (un archivo descomprimido) | 50 MiB |
| `max_files` | 2000 |
| longitud máxima de path | 255 caracteres |

Reglas de rechazo (todas con `InvalidPackageError` y su `reason`):

- No es un zip o está corrupto, incluidos fallos de CRC al leer → `NOT_A_ZIP` / `CORRUPT_ARCHIVE`.
- Entrada cifrada → `ENCRYPTED_ENTRY`.
- Método de compresión distinto de *stored* o *deflate* → `UNSUPPORTED_COMPRESSION`.
- Enlace simbólico (modo Unix `S_IFLNK` en `external_attr`) → `SYMLINK_NOT_ALLOWED`.
- Path absoluto, con segmento `..` o `.` o vacío, con `\`, con NUL o caracteres de control, o con más de 255 caracteres → `UNSAFE_PATH`.
- Paths duplicados tras normalizar → `DUPLICATE_PATH`.
- Más de `max_files` → `TOO_MANY_FILES`.
- Las entradas con descriptor de datos (bit 3) llevan tras sus datos un descriptor de 12, 16, 20 o 24 bytes cuya firma opcional, CRC y tamaños deben coincidir con los del directorio central; cualquier otro relleno → `CORRUPT_ARCHIVE`.
- El archivo debe estar teselado: toda cabecera local figura en el directorio central y no hay huecos ni datos sobrantes entre entradas (evita diferenciales entre analizadores y entradas ocultas para extractores en flujo) → `CORRUPT_ARCHIVE`.
- El directorio central también está teselado: se recorre desde su inicio con las longitudes que declara cada registro (firma `PK\x01\x02`, 46 bytes más nombre, extra y comentario) y debe terminar exactamente donde empieza el registro de fin (o el registro ZIP64 de fin, si lo hay). Los registros recorridos deben ser tantos como `entradas en este disco` y `entradas totales`, los números de disco deben ser 0 y el desplazamiento declarado debe ser el real. Con registro ZIP64 de fin (localizador y registro contiguos al de fin, de 44 bytes de tamaño declarado, un solo disco) cada campo de 32 bits solo puede valer su marcador (`0xFFFF` o `0xFFFFFFFF`) o lo mismo que el registro ZIP64, y se aplican las mismas reglas a los valores de 64 bits. Si no, un último registro cuyas longitudes declaradas desbordan el tamaño del directorio se leería truncado por `zipfile` pero completo (con bytes del registro de fin dentro del nombre, por ejemplo segmentos `..`) por los extractores que respetan las longitudes → `CORRUPT_ARCHIVE`.
- Todo flujo deflate ocupa exactamente su extensión comprimida (`compress_size` del directorio central): debe ser válido, contener su bloque final y acabar justo donde acaba la extensión. Bytes sobrantes tras el bloque final (por ejemplo una cabecera local oculta que un extractor guiado por el final del flujo recorrería como otra entrada) o un flujo sin bloque final → `CORRUPT_ARCHIVE`. Una extensión vacía es un archivo vacío. La regla vale también para entradas que no se leen como archivo: un directorio debe inflar a 0 bytes y las entradas ignoradas se verifican con el presupuesto de la sección 1.2. La extensión de una entrada almacenada (*stored*) sin descriptor de datos es exactamente `compress_size` y no admite ambigüedad.
- Tamaños excedidos → `TOO_LARGE`. **No se confía en el tamaño declarado en la cabecera**: la descompresión se hace por flujo contando bytes reales contra los límites, y se aborta al superarlos.

La inspección es CPU y E/S síncronas: se ejecuta fuera del bucle de eventos (`asyncio.to_thread` o equivalente).

### 1.5 Manifiesto

Lista ordenada por `path` con una entrada por archivo del paquete (sin directorios ni ignorados, el punto de entrada incluido):

```json
{"path": "scripts/run.py", "size": 1234, "sha256": "…64 hex…", "media_type": "text/x-python", "text": true}
```

- `media_type`: tabla propia (instancia privada de `mimetypes.MimeTypes`, independiente del sistema, más `.md`, `.markdown`, `.yml`, `.yaml`, `.toml`, `.sql`, `.sqlite`, `.sqlite3` y `.db`) o `application/octet-stream`.
- `text`: `true` si los primeros 8 KiB no contienen NUL y decodifican como UTF-8 (tolerando un carácter multibyte cortado al final de la muestra).

### 1.6 Códigos `reason` de paquete inválido

`NOT_A_ZIP`, `CORRUPT_ARCHIVE`, `ENCRYPTED_ENTRY`, `UNSUPPORTED_COMPRESSION`, `SYMLINK_NOT_ALLOWED`, `UNSAFE_PATH`, `DUPLICATE_PATH`, `TOO_MANY_FILES`, `TOO_LARGE`, `MISSING_ENTRYPOINT`, `AMBIGUOUS_ENTRYPOINT`, `BAD_FRONTMATTER`, `MISSING_FIELD`, `INVALID_FIELD`, `KIND_MISMATCH`, `VERSION_MISMATCH`.

## 2. Identidad, versiones y ciclo de vida

### 2.1 Referencias

- El nombre de la entrada es único globalmente (un solo espacio de nombres para ambos `kind`).
- Las URLs y herramientas aceptan una referencia `{ref}`: un UUID (id) o un nombre. Si `ref` tiene forma de UUID se busca por id; si no, por nombre.
- El `kind` de una entrada es inmutable: subir un paquete con el mismo nombre y distinto `kind` es `ConflictError`.

### 2.2 Semver y `latest`

- Versiones semver 2.0.0 estrictas: `MAJOR.MINOR.PATCH` con prerelease opcional, sin build. La precedencia sigue semver.org.
- `latest` es la versión de mayor precedencia entre las `published`, prefiriendo las estables; si solo hay prereleases, la mayor de ellas. Las `deprecated` y `draft` nunca son `latest`.
- Un `{version}` acepta una versión exacta o el literal `latest`.
- `(skill_id, version)` es único.

### 2.3 Estados de versión

`draft` → `published` → `deprecated`.

| Transición | Quién | Regla |
|---|---|---|
| (subida) → `draft` | `write` | Por defecto. `publish=true` (o `registry.auto_publish`) publica en el mismo acto. |
| `draft` → `published` | dueño o admin | |
| `deprecated` → `published` | dueño o admin | Deshace la deprecación. |
| `published` → `deprecated` | dueño o admin | Si es la **única** versión `published`, `ConflictError` (para retirar la entrada completa se oculta la entrada). |

Una versión `deprecated` sigue resolviéndose y descargándose por versión exacta, pero no es `latest` y la UI la marca. Las versiones son **inmutables**: el zip, el manifiesto y los metadatos extraídos nunca cambian; una corrección es una versión nueva. No hay borrado de versiones en v1.

### 2.4 Estado de la entrada

`Skill.status`: `draft`, `published`, `unpublished`.

- `published` ⇔ existe al menos una versión `published` (y entonces `latest_version` no es nulo).
- `draft` ⇔ ninguna versión `published`.
- `unpublished` = oculta por su dueño o un admin (`DELETE /skills/{ref}`); se revierte con `POST /skills/{ref}/restore`, que recalcula `draft` o `published`.

### 2.5 Campos denormalizados de la entrada

Tras cada subida, publicación, deprecación o restauración el servicio recalcula, a partir de la versión `latest` (o, si no hay, la versión más reciente por semver), los campos `display_name`, `description`, `category`, `search_text`, `latest_version` y las filas de `skill_tags` de la entrada. La búsqueda y los filtros leen solo la tabla de entradas.

## 3. Control de acceso

### 3.1 Credenciales

El protocolo es uno solo, en REST y en MCP: `Authorization: Bearer <credencial>`, donde la credencial es

- un **JWT** de `POST /api/v1/auth/login` (ámbito `write`, expira según `auth.jwt_expiry_minutes`), o
- un **token de API** `alx_…`.

Token de API: formato `alx_` + `secrets.token_urlsafe(32)`. Solo se guarda `sha256` hex del token completo. Se muestra una única vez al crearlo. Campos: `name`, `prefix` (primeros 12 caracteres, solo para mostrar), `scope` (`read` | `write`), `expires_at` opcional, `last_used_at` (se actualiza como mucho cada 10 minutos), `revoked_at`. Máximo 20 activos por usuario. Un token `read` nunca puede escribir; un token no puede tener más permisos que su usuario. Un usuario inactivo invalida sus tokens.

Contraseñas locales: como máximo `MAX_PASSWORD_LENGTH` (1024) caracteres, definido en `app/auth/password.py`. Una contraseña mayor nunca llega al hash: el login la rechaza como credencial inválida (`UnauthorizedError`, sin coste de hash y de forma idéntica para usuarios existentes e inexistentes, para que no sirva de oráculo de enumeración) y `AuthService.create_user` la rechaza con `ValidationError`. `UserCreate.password` y `LoginRequest.password` declaran el mismo `max_length`. La verificación contra el hash señuelo (usuario inexistente) sigue el mismo camino de código que la del usuario real, también ante un `ValueError` del hasher.

### 3.2 Visibilidad de lectura

| Quién | Ve |
|---|---|
| Anónimo (si `registry.public_read`) o cualquier principal | Entradas `published`; versiones `published` y `deprecated`. |
| Dueño de la entrada o admin | Además: entradas `draft` y `unpublished`, versiones `draft`. |

- Lo no visible responde **404** (no se revela su existencia).
- Con `registry.public_read=false`, toda lectura exige principal: sin credencial válida, **401**. `GET /health` y `GET /info` siguen públicos.
- Las listas y búsquedas solo incluyen entradas con `latest_version` no nulo para quien no es dueño ni admin. Dueño y admin pueden filtrar por `status`.

### 3.3 Escritura

| Acción | Permiso |
|---|---|
| Subir paquete (nueva entrada) | Principal con ámbito `write`. La entrada queda a nombre del subidor. |
| Subir versión a entrada existente, publicar, deprecar, ocultar, restaurar | Ámbito `write` y dueño de la entrada o admin. |
| Gestionar usuarios | Admin con ámbito `write`. |
| Crear, listar y revocar tokens propios | Principal con ámbito `write`. Un admin puede revocar cualquiera. |

## 4. Modelo de datos

Identificadores UUID v4 en `String(36)`. Los enums se guardan como su `.value` en minúsculas (`values_callable`), no nativos (`native_enum=False`), para que SQLite y PostgreSQL se comporten igual.

**`skills`**: `id`, `name` (único, índice), `display_name`, `description` (Text), `kind` (`skill`|`grounding`, índice, defecto `skill`), `category` (String 64, nulo, índice), `owner_id` (FK `users`), `status`, `latest_version` (String 50, nulo), `search_text` (Text, nulo), `created_at`, `updated_at`.

**`skill_tags`**: `skill_id` (FK, borrado en cascada), `tag` (String 32); PK `(skill_id, tag)`; índice por `tag`.

**`skill_versions`**: `id`, `skill_id` (FK), `version`, `status`, `blob_sha256` (String 64, índice), `size_bytes`, `uncompressed_bytes` (BigInteger), `file_count`, `entrypoint` (String 16), `root_prefix` (String 255, defecto `""`), `display_name`, `description` (Text), `category` (nulo), `tags` (Text, lista JSON), `metadata` (Text, JSON), `manifest` (Text, lista JSON), `search_text` (Text), `uploaded_by` (FK `users`), `published_by` (FK `users`, nulo), `published_at`, `deprecated_at`, `created_at`; restricción única `(skill_id, version)`. Sin las columnas antiguas `capabilities` ni `endpoints`.

**`api_tokens`**: `id`, `user_id` (FK), `name` (String 100), `prefix` (String 12), `token_hash` (String 64, único), `scope`, `created_at`, `expires_at`, `last_used_at`, `revoked_at`.

Migración `002_package_registry` sobre `001_initial`, reversible, ejecutable en SQLite (con `op.batch_alter_table`) y escrita para PostgreSQL. Descarta el contenido de la antigua `skill_versions` (la BD de partida está vacía) y normaliza a minúsculas los enums ya guardados en `users` y `skills`.

## 5. Configuración

```yaml
server:   { host, port, reload: false, public_url: "http://localhost:8000" }
database: { driver, path, pool_size, auto_migrate: true }
storage:  { backend: local, path: data/blobs }
registry:
  public_read: true
  auto_publish: false
  limits:
    max_package_bytes: 26214400
    max_uncompressed_bytes: 104857600
    max_file_bytes: 52428800
    max_files: 2000
    max_inline_read_bytes: 262144
tokens:   { max_per_user: 20 }
```

- Variables de entorno: `JWT_SECRET`, `DATABASE_URL`, `LOGGING_LEVEL`, `PUBLIC_URL`, `STORAGE_PATH`, `PUBLIC_READ`, `AUTO_PUBLISH`, `CORS_ORIGINS` (lista separada por comas), `ALEXANDRIA_ENV` (`development` | `production`, por defecto `production`).
- Las secciones anidadas son `pydantic.BaseModel`, **no** `BaseSettings`: las `BaseSettings` anidadas leen variables de entorno por nombre de campo (un campo `path` lee el `PATH` del sistema). Las variables de entorno se aplican de forma explícita.
- Se elimina `registry.default_skill_status` (no se usa).

## 6. Organización del código

```
app/services/errors.py             jerarquía DomainError (hereda de ValueError)
app/services/semver.py             parseo y orden semver
app/services/package_inspector.py  inspect_package(), PackageLimits, ParsedPackage, ManifestEntry
app/services/principal.py          Principal (dataclass, sin ORM)
app/services/skill_service.py      entradas: lectura, búsqueda, ocultar/restaurar, taxonomía
app/services/version_service.py    versiones: listar, resolver, publicar, deprecar
app/services/package_service.py    subida, manifiesto, lectura de archivos, descarga
app/services/token_service.py      tokens de API
app/services/auth_service.py      login y resolución de Principal; AuthService.create_user es la vía interna (seed, tests)
app/services/user_service.py      gestión de usuarios (solo admin con ámbito write; un admin no puede desactivarse a sí mismo)
app/text.py                       is_valid_text, validación de cadenas compartida por servicios y autenticación
app/storage/                      BlobStore (protocolo) y LocalBlobStore
app/models/skill.py, token.py, schemas.py
app/repositories/skill_repo.py, token_repo.py
app/api/                          REST (adaptador fino) y public.py
app/mcp/                          MCP (adaptador fino)
app/cli.py                        CLI `alx`
```

Transacciones: los repositorios hacen `flush` y `refresh` pero nunca `commit` (tampoco `user_repo`); el servicio es dueño de la transacción y confirma al final de cada caso de uso. Las conexiones SQLite activan `PRAGMA foreign_keys=ON` y sustituyen `lower()` por una función con plegado de mayúsculas Unicode (`str.lower()`), porque la integrada solo pliega ASCII y la búsqueda (sección 9.1) no encontraría `Árbol` con `árbol`. Todo motor SQLite que atienda consultas del catálogo (el de la aplicación y los de los tests de repositorios y servicios) se configura con `configure_sqlite_engine` (`app/db/session.py`); un motor que solo llame a `enable_sqlite_foreign_keys` vuelve al `lower()` ASCII sin avisar.

Concurrencia sobre una entrada: los casos de uso que recalculan sus campos denormalizados o dependen de otras versiones (subir a una entrada existente, publicar, deprecar, restaurar) empiezan, dentro de su transacción, bloqueando la fila de la entrada (`SELECT … FOR UPDATE` con `populate_existing`; sin efecto en SQLite, donde la primera escritura serializa a los escritores) y leen las versiones con `populate_existing`, de modo que nunca deciden sobre filas obsoletas del mapa de identidad. La guarda de `deprecar` («no se puede deprecar la única versión `published`») se evalúa dentro de esa transacción, después de escribir la deprecación y de releer las versiones publicadas, y revierte con `ConflictError` si no queda ninguna.

Reglas de AGENTS.md vigentes: lógica solo en servicios; los servicios no importan FastAPI; excepciones de dominio, no HTTP; `PaginatedResult[T]` en toda lista paginada; Alembic para el esquema; sin comentarios en el código salvo petición expresa; `async def` en todo acceso a BD.

### 6.1 Errores de dominio

| Excepción | HTTP | `error.code` |
|---|---|---|
| `NotFoundError` | 404 | `NOT_FOUND` |
| `ConflictError` | 409 | `CONFLICT` |
| `ValidationError` | 422 | `VALIDATION_ERROR` |
| `InvalidPackageError` (con `reason`) | 422 | `INVALID_PACKAGE` |
| `ForbiddenError` | 403 | `FORBIDDEN` |
| `UnauthorizedError` | 401 | `UNAUTHORIZED` |
| `PayloadTooLargeError` | 413 | `PAYLOAD_TOO_LARGE` |
| `StorageError` | 500 | `STORAGE_ERROR` |

`StorageError` indica una inconsistencia interna (blob ausente o corrupto de una versión que existe, columnas JSON ilegibles). Se registra con nivel error y al cliente solo le llega un mensaje genérico, sin rutas ni detalles internos; en MCP es el error de herramienta `STORAGE_ERROR: mensaje`.

Cuerpo de error en REST: `{"error": {"code": "…", "message": "…", "reason": "…"}}`. `reason` aparece cuando la excepción lo lleva (siempre en paquete inválido; también en `ValidationError` como `INVALID_PATH`) y se omite si es nulo. Un manejador global traduce `DomainError`; las rutas no capturan excepciones por su cuenta. En MCP el error se devuelve como error de herramienta con el texto `CODE: mensaje`.

## 7. API REST (`/api/v1`)

| Método y ruta | Acceso | Notas |
|---|---|---|
| `GET /health` | público | |
| `GET /info` | público | `{name, version, public_read, public_url, mcp_path, limits}` |
| `POST /auth/login` | público | |
| `GET /auth/me` | principal | |
| `GET /skills` | lectura | Parámetros: `q`, `kind`, `tag` (repetible, todos), `category`, `status` (solo dueño/admin), `mine`, `sort` (`relevance`\|`name`\|`updated`\|`created`), `limit` (1–100), `offset`. Devuelve `PaginatedResult` de `SkillSummary`. |
| `GET /skills/{ref}` | lectura | `SkillDetail` |
| `DELETE /skills/{ref}` | dueño/admin | Oculta la entrada. 204. |
| `POST /skills/{ref}/restore` | dueño/admin | `SkillDetail` |
| `POST /packages` | `write` | `multipart/form-data`: `file`, `version?`, `publish?`. 201 `{skill, version}`. 413 si supera `max_package_bytes` (se lee por trozos). |
| `GET /skills/{ref}/versions` | lectura | Lista de `VersionSummary`, de mayor a menor semver. |
| `GET /skills/{ref}/versions/{version}` | lectura | `VersionDetail`. `{version}` puede ser `latest`. |
| `POST /skills/{ref}/versions/{version}/publish` | dueño/admin | |
| `POST /skills/{ref}/versions/{version}/deprecate` | dueño/admin | |
| `GET …/versions/{version}/entrypoint` | lectura | `EntrypointContent` (`FileContent` + `name`, `version` y `kind` resueltos). |
| `GET …/versions/{version}/files` | lectura | `FileListing` (`{name, version, files: ManifestEntry[]}`), igual que la herramienta `list_files`. |
| `GET …/versions/{version}/files/{path:path}` | lectura | `FileContent` en JSON (equivalente a la herramienta `read_file`). |
| `GET …/versions/{version}/raw/{path:path}` | lectura | Bytes del archivo (ver sección 9). |
| `GET …/versions/{version}/download` | lectura | El zip. `Content-Type: application/zip`, `Content-Disposition: attachment; filename="{name}-{version}.zip"`, `X-Content-SHA256`, `ETag` con el sha256. `Cache-Control`: `public, immutable` para una versión exacta si `DownloadInfo.requires_auth` es falso; `public, no-cache` si `{version}` es `latest` y `requires_auth` es falso (puede cambiar de versión, así que el `ETag` revalida); `private, no-store` si `requires_auth` es verdadero. |
| `GET /tags`, `GET /categories` | lectura | `[{name, count}]`; parámetros `kind`, `limit`. Solo cuentan entradas visibles. |
| `POST /tokens`, `GET /tokens`, `DELETE /tokens/{id}` | `write` | `TokenCreated` incluye `token` en claro solo al crear. |
| `GET /users`, `POST /users` | admin | |

Desaparecen: `POST /skills`, `PUT /skills/{id}`, `POST /skills/{id}/versions` y `…/unpublish`. Estados: 200/201/204, 401 (con `WWW-Authenticate: Bearer`), 403, 404, 409, 413, 422.

### 7.1 Formas de respuesta (JSON, snake_case, fechas ISO 8601 UTC)

- `SkillSummary`: `id, name, display_name, description, kind, category, tags[], status, latest_version, owner_id, created_at, updated_at`.
- `SkillDetail`: `SkillSummary` + `versions: VersionSummary[]`.
- `VersionSummary`: `id, skill_id, version, status, sha256, size_bytes, uncompressed_bytes, file_count, entrypoint, display_name, description, category, tags[], uploaded_by, published_by, published_at, deprecated_at, created_at`.
- `VersionDetail`: `VersionSummary` + `metadata{}`, `download_url` (absoluta, desde `server.public_url`).
- `ManifestEntry`: `path, size, sha256, media_type, text`.
- `FileContent`: `path, size, sha256, media_type, text, content, truncated, encoding`. Si `text=false`, `content` es `null`. Si un texto supera `max_inline_read_bytes`, `content` trae el prefijo recortado en frontera de carácter y `truncated=true`.
- `UserResponse`: `id, username, email, role, is_active, created_at`; `id` es un string como todos los identificadores y `role` el valor en minúsculas. Sin `password_hash`: `AuthService` devuelve este DTO y nunca la fila ORM.
- `TokenResponse`: `id, name, prefix, scope, created_at, expires_at, last_used_at, revoked_at`. `TokenCreated`: lo anterior + `token`. Petición `TokenCreate`: `name`, `scope` (`read` por defecto), `expires_in_days` opcional.
- Los esquemas Pydantic de todas estas formas viven en `app/models/schemas.py` y son la única definición: REST, MCP y el sitio público los comparten. Los servicios devuelven estos DTOs, no objetos ORM.

## 8. MCP

Transporte: Streamable HTTP, sin estado y con respuestas JSON, montado en `/mcp`. SDK `mcp` 2.x (`from mcp.server.mcpserver import MCPServer`; `FastMCP` ya no existe). La credencial es la misma cabecera `Authorization: Bearer`. En v1 se exponen **solo herramientas** (las herramientas son la primitiva que todos los clientes soportan). Ninguna respuesta es un flujo `text/event-stream`: el servidor no atiende `subscriptions/listen` (responde `-32601` al instante) y no anuncia `listChanged` ni `subscribe` en ninguna versión del protocolo, porque la lista de herramientas es fija.

| Herramienta | Acceso | Parámetros | Devuelve |
|---|---|---|---|
| `search_skills` | lectura | `query=""`, `kind?`, `tags=[]`, `category?`, `limit=10` (máx. 50), `offset=0` | `PaginatedResult` de `SkillSummary` |
| `get_skill` | lectura | `name` | `SkillDetail` |
| `get_entrypoint` | lectura | `name`, `version="latest"` | `FileContent` + `version` y `kind` resueltos |
| `list_files` | lectura | `name`, `version="latest"` | `{name, version, files: ManifestEntry[]}` |
| `read_file` | lectura | `name`, `path`, `version="latest"` | `FileContent` |
| `get_download_info` | lectura | `name`, `version="latest"` | `{name, version, sha256, size_bytes, url, requires_auth}` |
| `list_tags`, `list_categories` | lectura | `kind?`, `limit=50` | `[{name, count}]` |
| `upload_package` | `write` | `content_base64`, `version?`, `publish=false` | `{skill, version}` |
| `publish_version`, `deprecate_version` | `write` | `name`, `version` | `VersionSummary` |

Paridad: cada operación de lectura devuelve el mismo JSON que su equivalente REST (mismos esquemas Pydantic, `model_dump(mode="json")`). Los binarios no viajan por MCP salvo `upload_package`; para descargar se usa `get_download_info` y la URL REST.

## 9. Búsqueda y servicio de archivos

### 9.1 Búsqueda

Portable (SQLite y PostgreSQL), sin FTS en v1.

- `q` se divide por espacios en términos. Cada término debe coincidir con al menos un campo (AND entre términos, OR entre campos): `name`, `display_name`, `description`, etiquetas, `search_text`. Comparación insensible a mayúsculas con `LIKE` escapando `%`, `_` y `\`. El plegado es Unicode y usa `str.lower()` en ambos lados (el término en Python, las columnas con `lower()` en SQL): en SQLite lo garantiza la función registrada en cada conexión (sección 6) y en PostgreSQL hace falta una base de datos con `LC_CTYPE` UTF-8, no `C`. La comparación sigue distinguiendo acentos (`arbol` no encuentra `Árbol`).
- Puntuación por término (se suma entre campos y entre términos): los niveles de nombre son excluyentes (exacto 100, prefijo 60, contiene 40); además, `display_name` contiene 30, etiqueta exacta 25, `description` contiene 10, `search_text` contiene 3. Una etiqueta basta para que el término coincida si lo contiene como subcadena, pero solo la coincidencia exacta puntúa.
- Se atienden como máximo 10 términos distintos de `q`; el filtro `tag` admite como máximo 20 valores (más de 20 devuelve vacío, ninguna entrada puede tener tantas).
- Orden `relevance` (defecto con `q`): puntuación descendente y luego `name`. Sin `q`, el defecto es `name`.
- Varias `tag` se combinan con AND. `kind` y `category` son filtros exactos.
- La paginación cuenta con una subconsulta; `total` es el recuento real tras filtros y visibilidad.

### 9.2 Servir archivos del paquete

- La ruta pedida se valida contra el manifiesto de la versión: rechazo de `..`, absolutas, `\` y NUL (`ValidationError`, reason `INVALID_PATH`); si no está en el manifiesto, `NotFoundError`. Se lee de `root_prefix + path` y se comprueba el sha256 del contenido contra el manifiesto.
- `…/raw/…` responde siempre con `X-Content-Type-Options: nosniff` y `Content-Security-Policy: default-src 'none'; sandbox`. Los archivos de texto se sirven como `text/plain; charset=utf-8`; el resto como `application/octet-stream` con `Content-Disposition: attachment`. **Nunca** se sirve HTML, SVG ni JS con su tipo propio: el origen del registro aloja la UI de administración y sus credenciales.
- Todo lo que se muestre del paquete en HTML (sitio público, administración) va escapado. El Markdown se renderiza con `markdown-it-py` con `html=False`.

### 9.3 Almacén de blobs

`BlobStore` es un protocolo asíncrono. `LocalBlobStore(root: Path)` guarda cada zip direccionado por contenido en `root/ab/<sha256>.zip`.

- Clave = sha256 hex de 64 caracteres, validada con regex antes de tocar el sistema de archivos.
- `put` es idempotente, verifica que el hash de los bytes coincide con la clave y escribe de forma atómica (archivo temporal y `os.replace`).
- Operaciones mínimas: `put`, `get`, `exists`, `delete`, y lectura por trozos para la descarga.
- La E/S de archivos va fuera del bucle de eventos.

## 10. Tests

- Pirámide de AGENTS.md: servicios primero, luego REST, MCP y paridad REST/MCP.
- Helper común: `tests/helpers/packages.py` con `make_zip(files, *, wrapper=None, ...)`, `skill_md(...)`, `index_md(...)`, `make_skill_zip(name, version, ...)`, `make_grounding_zip(name, version, ...)`.
- BD en memoria; el almacén de blobs en `tmp_path`. REST inyecta el almacén con `Depends` sobrescribible; MCP lo inyecta con el proveedor de contexto de `app/mcp/context.py`.
- Cobertura mínima de `app/services`: 85 %.
- Pruebas de seguridad obligatorias para la ingesta: zip-slip, symlink, bomba de descompresión (tamaño declarado falso), cifrado, anclas YAML, duplicados, `__MACOSX`, envoltorio, ambos puntos de entrada, directorio central desbordado por su último registro, recuentos del registro de fin que no coinciden, bytes tras el bloque final de un flujo deflate (con y sin descriptor de datos) y flujos sin bloque final, también en directorios y entradas ignoradas.

## 11. Fuera de alcance en v1

Búsqueda FTS, rate limiting, borrado de versiones y recolección de blobs huérfanos, firmas de paquetes, backend S3, organizaciones o equipos, OAuth/IdentityServer, recursos MCP, rangos semver, webhooks, Docker y CI.
