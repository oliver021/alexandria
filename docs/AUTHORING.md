# Guía de autoría de paquetes

Cómo escribir, validar, empaquetar, publicar y consumir paquetes para Alexandria. La referencia normativa es `docs/PACKAGE_CONTRACT.md`; esta guía explica lo que dice con ejemplos y con el flujo de trabajo de la CLI `alx`. Si una frase de esta guía contradice el contrato, manda el contrato.

Hay tres paquetes de ejemplo completos en `examples/packages/`, descritos en `examples/README.md`.

## 1. Qué es un paquete

Un paquete es un archivo `.zip` con un **punto de entrada** en la raíz y los artefactos que hagan falta: documentos, scripts, tablas, bases de datos, plantillas. Alexandria guarda el zip tal cual lo subes, calcula su `sha256` y extrae solo lo necesario para descubrirlo y servirlo: el frontmatter del punto de entrada y un manifiesto con el tamaño y el hash de cada archivo. **Nunca ejecuta nada del paquete.**

El punto de entrada decide el tipo (`kind`):

| Punto de entrada | `kind` | Para qué sirve |
|---|---|---|
| `SKILL.md` | `skill` | Conocimiento **procedimental**: cómo hacer algo, paso a paso. |
| `INDEX.md` | `grounding` | Conocimiento **referencial**: datos y documentos en los que apoyar respuestas. |

Los dos tipos comparten nombre, versiones, búsqueda y permisos. El nombre de una entrada es único en todo el registro, no por tipo.

## 2. Estructura

La raíz del paquete contiene **exactamente un** punto de entrada: `SKILL.md` o `INDEX.md`, con ese nombre y esas mayúsculas. Todo lo demás es libre. Una estructura habitual:

```text
mi-skill/
├── SKILL.md              punto de entrada (obligatorio)
├── scripts/              programas que el agente puede ejecutar
├── references/           documentos de apoyo que el agente lee bajo demanda
└── templates/            plantillas, formularios, ejemplos de salida
```

```text
mi-referencia/
├── INDEX.md              punto de entrada: qué hay y cómo usarlo
├── tables/               CSV, JSON, YAML
├── docs/                 documentos Markdown o HTML
└── db/                   bases SQLite u otros binarios
```

Reglas que se aplican a todo el contenido:

- Un `SKILL.md` o `INDEX.md` dentro de una subcarpeta es un archivo normal.
- Si la raíz tiene ambos puntos de entrada, el paquete es ambiguo y se rechaza.
- Si todas las entradas del zip cuelgan de un único directorio `D/` y el punto de entrada está en `D/`, ese directorio se trata como envoltorio y se descarta (es lo que producen muchas herramientas gráficas al comprimir una carpeta). `alx pack` no genera envoltorios: el punto de entrada va en la raíz.
- Se ignoran `__MACOSX/`, `.DS_Store` y `Thumbs.db`: no entran en el manifiesto ni cuentan para los límites.
- Las rutas no pueden ser absolutas, contener segmentos `..` o `.` ni vacíos, usar `\`, tener caracteres de control ni superar 255 caracteres.
- Dos rutas que solo se diferencien en mayúsculas o en la normalización Unicode (`Datos.csv` y `datos.csv`) cuentan como duplicadas. Un nombre no puede ser a la vez archivo y directorio.
- No se admiten enlaces simbólicos, entradas cifradas ni métodos de compresión distintos de *stored* y *deflate*.

## 3. El frontmatter

El punto de entrada empieza con un bloque YAML entre dos líneas `---` (se tolera un BOM al principio). Lo que sigue es el cuerpo en Markdown.

```markdown
---
name: changelog-from-commits
display_name: Changelog from Commits
description: Turn a range of Conventional Commits into a Keep a Changelog release section. Use it when preparing release notes.
version: 1.0.0
tags:
  - changelog
  - git
category: release-management
---

# Changelog from Commits

...
```

| Campo | ¿Obligatorio? | Regla |
|---|---|---|
| `name` | sí | Minúsculas y dígitos separados por guiones sencillos: `^[a-z0-9]+(?:-[a-z0-9]+)*$`, de 1 a 64 caracteres. No puede tener forma de UUID. |
| `description` | sí | Texto de 1 a 1024 caracteres tras quitar espacios. |
| `version` | sí* | Semver 2.0.0 como cadena, sin metadatos de build (`+`), hasta 50 caracteres. *Puede omitirse si la indicas al subir (`--version`); si aparece en ambos sitios deben coincidir. |
| `display_name` | no | Hasta 255 caracteres. Por defecto es el `name` con guiones convertidos en espacios y cada palabra capitalizada. |
| `tags` | no | Lista de cadenas. Se les quitan los espacios y se pasan a minúsculas; cada una debe cumplir `^[a-z0-9][a-z0-9._+-]{0,31}$`. Máximo 20, sin duplicados (se conserva el orden). |
| `category` | no | Una etiqueta de texto libre en forma de *slug*: `^[a-z0-9][a-z0-9-]{0,63}$`, tras quitar espacios y pasar a minúsculas. No hay lista cerrada. |
| `kind` | no | `skill` o `grounding`. Si aparece debe coincidir con el punto de entrada. No se guarda. |
| cualquier otro | no | Se conserva tal cual en `metadata` (JSON). Las fechas se serializan en ISO 8601. |

Cuidados con el YAML:

- **Entrecomilla las versiones.** `version: 1.0` lo lee YAML como el número 1,0 y se rechaza con una indicación; `version: "1.0.0"` o `version: 1.0.0` (que YAML no interpreta como número) son válidas. Para valores que YAML podría convertir en números, fechas o booleanos usa comillas.
- El bloque no puede superar 16 KiB, debe ser un mapa con claves de tipo cadena, y no puede repetir claves.
- No se admiten **anclas ni alias** (`&`, `*`), y el anidamiento tiene un máximo de 32 niveles.
- Los caracteres de control en `description`, `display_name` o en valores de `metadata` se rechazan.

Qué se indexa: la búsqueda mira `name`, `display_name`, `description`, las etiquetas y los primeros 20 000 caracteres del cuerpo. Una buena descripción dice **qué hace** y **cuándo usarlo**, con las palabras que usaría quien lo busca.

## 4. Skill o grounding

| | Skill (`SKILL.md`) | Grounding (`INDEX.md`) |
|---|---|---|
| Responde a | «¿cómo hago X?» | «¿qué sabemos sobre X?» |
| Contenido típico | Pasos, criterios de decisión, listas de comprobación, scripts, plantillas | Tablas, especificaciones, glosarios, documentos, bases de datos |
| El cuerpo del punto de entrada es | Las instrucciones | Un índice: qué hay, dónde, con qué columnas y cómo consultarlo |
| Ejemplo | `changelog-from-commits`, `incident-postmortem` | `http-reference` |

**Un buen `SKILL.md`** abre con una frase de propósito y sigue con: cuándo usarlo y cuándo no, qué entradas necesita, un procedimiento numerado con verbos claros, qué hace cada script (con ejemplos de invocación), una lista de comprobación final y los errores más comunes. Enlaza los archivos de `references/` con rutas relativas y no los copies en el cuerpo: así el agente los lee solo si los necesita.

**Un buen `INDEX.md`** es un mapa: una tabla con cada archivo y su contenido, un diccionario de datos (columnas y significado), ejemplos de consulta, y una sección de límites y procedencia (de dónde salen los datos, qué no cubren).

Consideraciones de lectura:

- Por MCP y por `GET …/files/{ruta}` se leen los archivos de **texto** en línea, hasta 256 KiB (`registry.limits.max_inline_read_bytes`); si un texto es mayor, la respuesta viene recortada y marcada con `truncated`. Divide los documentos largos en varios archivos.
- Un archivo se considera texto si sus primeros 8 KiB no contienen NUL y son UTF-8 válido. Todo lo demás (bases SQLite, imágenes, PDF) es binario: su contenido no viaja por MCP, solo se obtiene descargando el paquete.
- El HTML, el SVG y el JavaScript de un paquete se sirven siempre como texto plano o como descarga, nunca con su tipo propio: se pueden publicar plantillas y hojas HTML, pero el navegador no las interpreta desde el registro. Renderízalas localmente.
- Todo lo que el registro muestra de tu paquete en páginas HTML va escapado y el Markdown se renderiza sin HTML en bruto.
- La página pública de la entrada da formato solo a los primeros 64 KiB del punto de entrada. Si ese texto tiene más de 10 000 líneas o más de 500 títulos, si su HTML pasaría de 1 MiB o si darle formato cuesta más de 1 s de CPU, la página lo muestra como texto plano con un aviso. REST y MCP no cambian: devuelven siempre el texto sin formato.

## 5. Versionado y ciclo de vida

### Versiones

Las versiones siguen **semver 2.0.0** estricto: `MAJOR.MINOR.PATCH` con prerelease opcional (`2.0.0-rc.1`) y sin metadatos de build. Una versión nueva es la única forma de corregir un paquete publicado, porque **las versiones son inmutables**: el zip, el manifiesto y los metadatos no cambian nunca y no se pueden borrar.

| Cambio | Sube |
|---|---|
| Incompatible con lo anterior (otro formato de datos, otro procedimiento) | MAJOR |
| Contenido nuevo compatible | MINOR |
| Correcciones | PATCH |

`latest` es la versión de mayor precedencia entre las publicadas, prefiriendo las estables sobre las prerelease. Una versión `draft` o `deprecated` nunca es `latest`. En cualquier lugar donde se pide una versión se acepta una exacta o el literal `latest`.

### Estados de una versión

```text
(subida) ──▶ draft ──▶ published ──▶ deprecated
                          ▲              │
                          └──────────────┘
```

| Transición | Quién | Regla |
|---|---|---|
| subida → `draft` | cualquier principal con ámbito `write` | Es el estado por defecto. `--publish` (o `registry.auto_publish`) la publica en el mismo acto. |
| `draft` → `published` | dueño o admin | |
| `published` → `deprecated` | dueño o admin | Si es la única versión publicada se rechaza con `CONFLICT`; para retirar toda la entrada se oculta la entrada. |
| `deprecated` → `published` | dueño o admin | Deshace la deprecación. |

Una versión `deprecated` sigue pudiéndose descargar por su número exacto, pero deja de ser `latest` y las interfaces la marcan. Un `draft` solo lo ven su dueño y los administradores.

`alx publish` sube y, con `--publish`, publica. Para pasar de borrador a publicado después, o para deprecar, usa la API:

```sh
curl -X POST -H "Authorization: Bearer $ALX_TOKEN" \
  "$ALX_URL/api/v1/skills/mi-skill/versions/1.0.0/publish"
curl -X POST -H "Authorization: Bearer $ALX_TOKEN" \
  "$ALX_URL/api/v1/skills/mi-skill/versions/0.9.0/deprecate"
```

(por MCP son las herramientas `publish_version` y `deprecate_version`).

### Estado de la entrada y propiedad

- La entrada queda a nombre de quien sube la primera versión; solo él y los administradores pueden subir más versiones, publicar, deprecar u ocultar. Subir una versión a una entrada ajena da `FORBIDDEN`.
- Estado de la entrada: `published` si hay al menos una versión publicada, `draft` si no la hay y `unpublished` cuando su dueño o un administrador la oculta (`DELETE /api/v1/skills/{ref}`). Se revierte con `POST /api/v1/skills/{ref}/restore`.
- El `kind` de una entrada es inmutable: subir con el mismo nombre un paquete de otro tipo da `CONFLICT`.
- `latest_version`, la descripción, la categoría y las etiquetas de la entrada se recalculan a partir de la versión `latest` tras cada subida o cambio de estado.

### Flujo recomendado

1. Sube como borrador y revisa el resultado (`alx pull` desde otra máquina o con otro usuario lector).
2. Publica con la API cuando esté bien, o sube directamente con `--publish` si el paquete ya está verificado.
3. Para cambiar algo, sube una versión nueva; deprecar la anterior es opcional.

## 6. Límites y errores frecuentes

### Límites por defecto

Los fija el operador del registro en `registry.limits`; `GET /api/v1/info` los devuelve. `alx` aplica los valores por defecto al validar en local.

| Límite | Valor por defecto |
|---|---|
| Tamaño del zip subido | 25 MiB |
| Suma real de los archivos descomprimidos | 100 MiB |
| Un archivo descomprimido | 50 MiB |
| Número de archivos | 2000 |
| Longitud de una ruta | 255 caracteres |
| Bloque de frontmatter | 16 KiB |
| Lectura en línea de un archivo de texto | 256 KiB |
| Cuerpo indexado para la búsqueda | 20 000 caracteres |

El servidor no se fía de los tamaños declarados en el zip: descomprime contando bytes reales y aborta al superar un límite.

### Motivos de rechazo (`reason`)

Un paquete inválido responde `INVALID_PACKAGE` (HTTP 422) y un `reason`. `alx validate` y `alx pack` los muestran antes de subir.

| `reason` | Causa habitual | Cómo arreglarlo |
|---|---|---|
| `NOT_A_ZIP` | El archivo no es un zip: un `.tar.gz` renombrado, una página de error descargada. | Crea el zip con `alx pack`. |
| `CORRUPT_ARCHIVE` | Zip truncado, con bytes sobrantes, datos escondidos entre entradas o un directorio central inconsistente. | Vuelve a empaquetar con `alx pack`. |
| `ENCRYPTED_ENTRY` | Zip protegido con contraseña. | Comprime sin cifrado. |
| `UNSUPPORTED_COMPRESSION` | Compresión bzip2, lzma, zstd… | Usa deflate (el valor por defecto) o stored. |
| `SYMLINK_NOT_ALLOWED` | Hay un enlace simbólico en el árbol. | Sustitúyelo por una copia del archivo. `alx` no sigue enlaces para no empaquetar archivos de fuera del árbol por accidente. |
| `UNSAFE_PATH` | Ruta absoluta, con `..`, con `\`, con caracteres de control o de más de 255 caracteres; también archivos especiales (tuberías, dispositivos) y nombres no codificables en UTF-8. | Renombra el archivo. |
| `DUPLICATE_PATH` | Dos rutas iguales salvo mayúsculas o normalización Unicode; un nombre que es archivo y directorio. | Deja una sola. |
| `TOO_MANY_FILES` | Más de 2000 archivos (típico: `node_modules` sin excluir). | Excluye dependencias o agrupa los datos. |
| `TOO_LARGE` | Zip, archivo o total descomprimido por encima de los límites. | Reduce, comprime los datos o guárdalos fuera y enlázalos. |
| `MISSING_ENTRYPOINT` | No hay `SKILL.md` ni `INDEX.md` en la raíz (ni en el único directorio envoltorio). | Créalo con el nombre y las mayúsculas exactas. |
| `AMBIGUOUS_ENTRYPOINT` | Hay `SKILL.md` e `INDEX.md` en la raíz. | Deja solo el que corresponda al tipo. |
| `BAD_FRONTMATTER` | Faltan los `---`, el YAML no se puede leer, hay anclas, alias o claves repetidas, el bloque pasa de 16 KiB o el punto de entrada no es UTF-8. | Corrige el bloque; valida con `alx validate`. |
| `MISSING_FIELD` | Falta `name`, `description` o la versión. | Añade el campo, o pasa `--version`. |
| `INVALID_FIELD` | `name` mal formado, `description` vacía o demasiado larga, versión que no es semver (o un número YAML como `1.0`), etiqueta o categoría inválidas, caracteres de control. | Ajusta el valor a la tabla de la sección 3. |
| `KIND_MISMATCH` | El campo `kind` no coincide con el punto de entrada. | Quita `kind` o corrígelo. |
| `VERSION_MISMATCH` | La versión pasada con `--version` difiere de la del frontmatter. | Usa la misma, o quita una de las dos. |

### Otros errores del servidor

Todos tienen la forma `{"error": {"code": "…", "message": "…", "reason": "…"}}`; `reason` solo aparece cuando aplica.

| HTTP | `code` | Cuándo |
|---|---|---|
| 401 | `UNAUTHORIZED` | Falta la credencial, es inválida o ha caducado, o el registro es privado. |
| 403 | `FORBIDDEN` | Token de solo lectura que intenta escribir, o una entrada que es de otro usuario. |
| 404 | `NOT_FOUND` | No existe o no puedes verla (no se revela cuál de las dos). |
| 409 | `CONFLICT` | La versión ya existe, el `kind` no coincide, o se intenta deprecar la única versión publicada. |
| 413 | `PAYLOAD_TOO_LARGE` | El zip supera el máximo de subida. |
| 422 | `INVALID_PACKAGE`, `VALIDATION_ERROR` | Paquete inválido (con su `reason`) o parámetros incorrectos. |
| 500 | `STORAGE_ERROR` | Inconsistencia interna: el blob falta o está corrupto. Avisa al operador. |

## 7. Flujo de trabajo con `alx`

La CLI viene con el proyecto (`pip install -e .`). Sin instalarla se puede ejecutar con `python -m app.cli`. Los mensajes y los errores no muestran trazas.

```text
alx validate RUTA              comprueba un directorio o un .zip
alx pack DIRECTORIO            construye el .zip determinista
alx publish RUTA               sube un directorio o un .zip
alx search [CONSULTA]          busca en el catálogo
alx download NOMBRE[@VERSION]  descarga el .zip y verifica su sha256
alx pull NOMBRE[@VERSION]      descarga, revalida y extrae con seguridad
```

Códigos de salida: **0** correcto, **1** fallo (paquete inválido, error del servidor, red o integridad) y **2** uso incorrecto (argumentos, ruta inexistente, falta el token). Con `--json` la salida normal es JSON; los errores salen por la salida de errores, también como JSON. Si ocurre algo inesperado, `alx` lo resume en una línea y sale con 1; con `ALX_DEBUG=1` en el entorno se muestra la traza completa, útil para informar de un fallo.

Los ejemplos de salida de esta sección son ilustrativos: los hashes, los tamaños y los identificadores serán los de tu paquete.

### 7.1 Validar

```sh
$ alx validate mi-skill
OK: mi-skill
  kind          skill
  name          mi-skill
  version       1.0.0
  display name  Mi Skill
  description   Hace algo útil. Úsalo cuando haga falta.
  category      demos
  tags          demo, ejemplo
  entrypoint    SKILL.md
  files         3 (143 B uncompressed)
  package       454 B, sha256 9cf851773979d67ffd67df834a3e4831ad574f6852263a998f70e8aeacb3cd56
```

Con `--files` lista el manifiesto, y con `--json` devuelve el resumen y el manifiesto completo. Si el paquete es inválido el código de salida es 1:

```text
INVALID: mi-skill
  MISSING_FIELD: The frontmatter field 'description' is required
```

`validate` ejecuta el mismo inspector que el servidor, así que lo que pasa aquí pasa allí (con los límites por defecto). Funciona sobre directorios y sobre archivos `.zip` ya hechos. La opción `--version` aporta la versión cuando el frontmatter no la tiene.

### 7.2 Empaquetar

```sh
$ alx pack mi-skill -o dist/
Packed dist/mi-skill-1.0.0.zip
  kind     skill
  name     mi-skill
  version  1.0.0
  files    3 (143 B uncompressed)
  size     454 B (454 bytes)
  sha256   9cf851773979d67ffd67df834a3e4831ad574f6852263a998f70e8aeacb3cd56
```

`pack` valida el contenido **antes** de escribir: si el paquete es inválido no se crea ningún archivo ni directorio, y un archivo de salida anterior se conserva. El zip es **determinista**:

- las entradas van ordenadas por ruta, sin entradas de directorio;
- la fecha de todas es 1980-01-01 00:00:00, la creación se marca como Unix y no hay campos extra;
- los permisos se normalizan a `0644`, o `0755` si el propietario puede ejecutar el archivo;
- la compresión es deflate de nivel 9.

Dos paquetes del mismo árbol dan el mismo `sha256`, sea cual sea el orden de creación de los archivos, sus fechas, sus permisos intermedios o la ruta del directorio. El resultado depende de la versión de zlib del intérprete.

No se empaquetan `.git`, `__pycache__`, `node_modules`, `.DS_Store`, `Thumbs.db` ni `*.pyc`. Los enlaces simbólicos y los archivos especiales hacen fallar el empaquetado. Si ejecutas `alx pack .` desde dentro del directorio, el `NOMBRE-VERSION.zip` resultante queda en su raíz y no se incluye en ningún paquete posterior (ni en `pack`, ni en `validate`, ni en `publish`); si eliges como salida otro archivo dentro del árbol, `pack` también lo excluye y te avisa de que está dentro.

`-o` acepta un archivo, un directorio existente o una ruta que termina en `/` (se crea). Si la salida cae dentro del árbol del paquete, `pack` solo la reemplaza cuando ya es un archivo comprimido: nunca sobrescribe un archivo fuente.

### 7.3 Publicar

```sh
export ALX_URL=https://registro.example.org
export ALX_TOKEN=alx_...
alx publish mi-skill --publish
```

```text
Uploaded mi-skill 1.0.0 (skill)
  status  published
  sha256  9cf851773979d67ffd67df834a3e4831ad574f6852263a998f70e8aeacb3cd56
  size    454 B, 3 files
  id      0b6c1f4e-6f1a-4d0e-9a4e-6a7a0f3c9e11
```

`publish` empaqueta si le das un directorio y valida siempre en local antes de subir, de modo que un paquete inválido no llega a enviarse. Opciones:

| Opción | Efecto |
|---|---|
| `--url URL` | Dirección del registro. Por defecto, `$ALX_URL` o `http://localhost:8000`. |
| `--token TOKEN` | Credencial. Por defecto, `$ALX_TOKEN`. Prefiere la variable de entorno: un argumento queda en el historial del intérprete y en la lista de procesos. |
| `--version V` | Versión que se envía con la subida; debe coincidir con la del frontmatter si existe. |
| `--publish` | Publica la versión en el mismo acto. Sin esta opción queda en borrador. |
| `--json` | Imprime la respuesta del servidor: `{"skill": …, "version": …}`. |

Qué hace con los errores del servidor: muestra `CODE (reason): mensaje` y sale con 1.

```text
error: CONFLICT: Version 1.0.0 of 'mi-skill' already exists
error: INVALID_PACKAGE (BAD_FRONTMATTER): The frontmatter of SKILL.md is not closed by a '---' line
```

Seguridad de la conexión: la dirección no puede llevar usuario ni contraseña, no se siguen redirecciones (se informa de a dónde apuntan) y se avisa si se envía un token por `http://` a un equipo que no es local. El token no se escribe nunca en la salida. La CLI respeta las variables de entorno habituales de proxy (`HTTPS_PROXY`, `NO_PROXY`) y de certificados (`SSL_CERT_FILE`, `SSL_CERT_DIR`), útiles para registros con una CA propia.

### 7.4 Consultar

```sh
$ alx search http
NAME            KIND       LATEST  DESCRIPTION
http-reference  grounding  1.0.0   Reference tables and specifications for designing and deb...

1 of 1 result(s) shown
```

Opciones: `--kind skill|grounding`, `--tag` (repetible; deben cumplirse todas), `--limit` (1 a 100, 20 por defecto) y `--offset`. La búsqueda divide la consulta en palabras y exige que cada una aparezca en el nombre, el título, la descripción, las etiquetas o el texto; distingue acentos (`arbol` no encuentra `Árbol`).

```sh
alx download http-reference@1.0.0 -o /tmp/        # solo el zip, verificado
alx pull http-reference -d ./refs                  # extrae con seguridad
```

`download` exige que el servidor envíe `X-Content-SHA256`, calcula el hash mientras descarga y descarta el archivo si no coincide. `pull` añade la revalidación completa del zip con el mismo inspector y la extracción segura que se describe en la sección 8.4.

## 8. Credenciales y consumo

### 8.1 Credenciales

Toda la autenticación es una cabecera `Authorization: Bearer <credencial>`, igual en REST y en MCP. La credencial es una de estas dos:

- Un **JWT** de `POST /api/v1/auth/login` (ámbito `write`, caduca a los `auth.jwt_expiry_minutes` configurados).
- Un **token de API** (`alx_` más 43 caracteres), pensado para agentes y automatizaciones. Se muestra **una sola vez**, al crearlo; el servidor guarda solo su hash.

| Ámbito | Permite |
|---|---|
| `read` | Leer, buscar y descargar. Nunca escribir. |
| `write` | Lo anterior, más subir, publicar, deprecar y gestionar tus tokens. |

Un token nunca tiene más permisos que su usuario, y un usuario desactivado invalida los suyos. Hay un máximo de 20 tokens activos por usuario.

```sh
JWT=$(curl -s -X POST "$ALX_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"username": "ana", "password": "…"}' | python -c 'import json,sys; print(json.load(sys.stdin)["token"])')

curl -s -X POST "$ALX_URL/api/v1/tokens" \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name": "ci", "scope": "write", "expires_in_days": 90}'

curl -s -H "Authorization: Bearer $ALX_TOKEN" "$ALX_URL/api/v1/tokens"
curl -s -X DELETE -H "Authorization: Bearer $ALX_TOKEN" "$ALX_URL/api/v1/tokens/ID_DEL_TOKEN"
```

Usa un token `read` para los agentes que solo consumen y uno `write` con caducidad para la integración continua. Si el registro tiene `registry.public_read=false`, toda lectura exige credencial.

### 8.2 REST

| Operación | Petición |
|---|---|
| Buscar | `GET /api/v1/skills?q=…&kind=…&tag=…&category=…&limit=…&offset=…` |
| Ficha y versiones | `GET /api/v1/skills/{ref}` y `GET /api/v1/skills/{ref}/versions` |
| Una versión | `GET /api/v1/skills/{ref}/versions/{version}` (acepta `latest`) |
| Punto de entrada | `GET …/versions/{version}/entrypoint` |
| Manifiesto | `GET …/versions/{version}/files` |
| Un archivo (JSON) | `GET …/versions/{version}/files/{ruta}` |
| Un archivo (bytes) | `GET …/versions/{version}/raw/{ruta}` |
| El zip | `GET …/versions/{version}/download` |
| Etiquetas y categorías | `GET /api/v1/tags`, `GET /api/v1/categories` |
| Subir | `POST /api/v1/packages` (`multipart/form-data` con `file`, y opcionalmente `version` y `publish`) |

`{ref}` es el nombre o el UUID de la entrada. La descarga lleva las cabeceras `X-Content-SHA256` y `ETag` con el hash del zip y `Content-Disposition` con el nombre `NOMBRE-VERSION.zip`.

`…/raw/{ruta}` entrega el archivo en flujo, con `Content-Length` igual al tamaño del manifiesto, y no envía ni un byte hasta haber comprobado su `sha256` contra el manifiesto: si el almacén está dañado responde `500 STORAGE_ERROR` en lugar de un cuerpo incompleto. Los archivos de texto se muestran como `text/plain` y el resto como descarga; nunca se sirve HTML, SVG ni JavaScript con su tipo propio.

```sh
curl -s "$ALX_URL/api/v1/skills?q=changelog&kind=skill"
curl -sD - -o mi.zip "$ALX_URL/api/v1/skills/changelog-from-commits/versions/latest/download"
```

### 8.3 MCP

El servidor MCP usa el transporte *Streamable HTTP*, sin estado y con respuestas JSON, en `/mcp` (`GET /api/v1/info` devuelve la ruta en `mcp_path`). Se autentica con la misma cabecera `Authorization: Bearer`. Un cliente necesita dos datos: la URL `https://registro.example.org/mcp` y esa cabecera con un token `read` (o `write` si el agente va a publicar). Cada respuesta es un único documento JSON: el servidor no mantiene flujos abiertos, no atiende `subscriptions/listen` y no anuncia `listChanged`, así que un cliente que quiera saber si hay novedades vuelve a llamar a `search_skills`.

| Herramienta | Parámetros | Devuelve |
|---|---|---|
| `search_skills` | `query`, `kind`, `tags`, `category`, `limit` (máx. 50), `offset` | Página de entradas |
| `get_skill` | `name` | La entrada con sus versiones |
| `get_entrypoint` | `name`, `version="latest"` | Contenido de `SKILL.md` o `INDEX.md` |
| `list_files` | `name`, `version="latest"` | Manifiesto |
| `read_file` | `name`, `path`, `version="latest"` | Un archivo de texto (recortado a 256 KiB si es mayor) |
| `get_download_info` | `name`, `version="latest"` | `sha256`, tamaño, URL y si hace falta autenticación |
| `list_tags`, `list_categories` | `kind`, `limit` | Recuentos |
| `upload_package` | `content_base64`, `version`, `publish` | La entrada y la versión creadas |
| `publish_version`, `deprecate_version` | `name`, `version` | La versión |

Un recorrido típico de un agente: `search_skills` para encontrar el paquete, `get_entrypoint` para leer las instrucciones, `read_file` para abrir solo los documentos de `references/` o las tablas que necesite, y, si hay que ejecutar scripts o consultar una base SQLite, `get_download_info` para obtener la URL del zip y `alx pull` para extraerlo. Los errores llegan como errores de herramienta con el texto `CODE: mensaje`.

### 8.4 Extraer un zip descargado sin riesgos

Descomprimir un zip ajeno con `unzip` o con `zipfile.extractall` es donde los consumidores suelen equivocarse: un nombre `../../.bashrc` sale del directorio destino, un enlace simbólico apunta a `/etc`, un archivo ya existente se pisa sin avisar y un zip pequeño puede expandirse hasta llenar el disco. `alx pull` evita cada uno de ellos:

1. Descarga con tope de tamaño y comprueba el `sha256` anunciado en `X-Content-SHA256`; si no coincide, o si el servidor no lo envía, no sigue.
2. Revalida el zip con el inspector del servidor y rechaza con su `reason` las rutas inseguras, los enlaces simbólicos, las entradas cifradas, los duplicados y las bombas de descompresión.
3. Comprueba que el paquete declara el nombre (y la versión exacta, si la pediste) que solicitaste.
4. Extrae **solo los archivos del manifiesto**, relativos a la raíz del paquete (sin el directorio envoltorio y sin la basura de macOS), y nada más.
5. Vuelve a comprobar cada ruta antes de escribir, verifica el tamaño y el `sha256` de cada archivo contra el manifiesto y asigna permisos `0644` o `0755` (nunca los bits setuid, setgid o sticky).
6. No sigue ni atraviesa enlaces simbólicos que existan en el destino y no sobrescribe archivos existentes sin `--force`; con `--force` solo reemplaza archivos normales, jamás directorios ni enlaces.
7. Es transaccional: prepara todos los archivos con nombres temporales, los coloca al final y, si algo falla, retira lo que hubiera colocado y los directorios que creó. No borra nunca nada que ya estuviera en el destino.

El destino por defecto es `./NOMBRE`. Si usas otra herramienta para descomprimir, aplica estas mismas comprobaciones.

## 9. Seguridad y buenas prácticas

- **No pongas secretos en un paquete.** Cualquiera con acceso de lectura al registro lo ve, y con `registry.public_read=true` eso es cualquiera. Tampoco datos personales.
- Alexandria no ejecuta nada de lo que publicas, pero **quien consume tus scripts sí puede ejecutarlos**: escríbelos para que sean seguros, documenta lo que hacen y evita descargar y ejecutar código remoto.
- Los scripts de ejemplo usan solo la biblioteca estándar y no hacen peticiones de red; es una buena base.
- Prefiere formatos de texto (CSV, JSON, Markdown) a binarios cuando el agente deba leerlos por MCP, y deja el binario (SQLite) como complemento generado a partir del texto con un script reproducible, como hace `http-reference`.
- Documenta la procedencia de los datos y su fecha. Un grounding sin fuente no se puede comprobar.
- Mantén los paquetes pequeños y enfocados: es más fácil elegirlos, leerlos y versionarlos que un paquete que lo hace todo.
- Cada versión nueva debe ser autosuficiente: no dependas de que exista una versión anterior.

## 10. Lista de comprobación antes de publicar

- [ ] `alx validate` termina con código 0 y el resumen muestra el `kind`, el nombre y la versión esperados.
- [ ] El frontmatter tiene `name`, `description`, `version`, `tags`, `category` y, si hace falta, `display_name`; la versión está en semver y es nueva.
- [ ] La descripción dice qué hace y cuándo usarlo, con palabras que se buscarían.
- [ ] El cuerpo del punto de entrada se puede seguir sin abrir nada más, y enlaza el resto con rutas relativas.
- [ ] Los archivos de texto que debe leer un agente están en UTF-8 y ninguno pasa de 256 KiB.
- [ ] No hay secretos, datos personales, enlaces simbólicos ni basura (`.git`, `node_modules`, `__pycache__`).
- [ ] Los scripts funcionan con la biblioteca estándar (o declaran sus dependencias) y se han probado.
- [ ] `alx pack` dos veces da el mismo `sha256`.
- [ ] Probado el ciclo completo: `alx publish`, `alx search`, `alx pull` en un directorio nuevo.
