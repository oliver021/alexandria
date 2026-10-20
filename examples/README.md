# Paquetes de ejemplo

Tres paquetes reales y útiles, escritos para el contrato de `docs/PACKAGE_CONTRACT.md`. Sirven para probar un registro Alexandria de extremo a extremo y como plantilla al crear paquetes propios. La guía completa de autoría está en `docs/AUTHORING.md`.

| Directorio | `kind` | Versión | Qué muestra |
|---|---|---|---|
| `packages/changelog-from-commits` | `skill` | 1.0.0 | Un procedimiento con pasos, `scripts/` (un script de shell ejecutable y un script de Python) y `references/` con resúmenes de las convenciones que aplica. |
| `packages/http-reference` | `grounding` | 1.0.0 | Datos de referencia: tablas CSV, una especificación YAML, una hoja HTML y una base SQLite generada por un script reproducible. |
| `packages/incident-postmortem` | `skill` | 1.0.0 | Un skill con plantilla HTML (`templates/`), un renderizador que escapa los valores y un JSON de ejemplo. |

Cada directorio contiene el árbol fuente del paquete: lo que hay dentro es exactamente lo que se empaqueta, salvo los archivos de ruido (`.git`, `__pycache__`, `node_modules`, `.DS_Store`, `Thumbs.db` y `*.pyc`).

## Requisitos

- Python 3.11 o superior y el proyecto instalado (`pip install -e ".[dev]"`), que deja el comando `alx` en el entorno. Sin instalarlo se puede usar `python -m app.cli`.
- Para publicar y descargar, un registro en marcha (`uvicorn app.main:app`, por defecto en `http://localhost:8000`) y, para publicar, un token con ámbito `write`.

## Validar

`alx validate` aplica exactamente las mismas comprobaciones que hará el servidor al recibir el paquete y no necesita red:

```sh
alx validate examples/packages/changelog-from-commits
alx validate examples/packages/http-reference --files
alx validate examples/packages/incident-postmortem --json
```

El código de salida es 0 si el paquete es válido, 1 si no lo es (con el `reason` del error) y 2 si el uso es incorrecto.

## Empaquetar

```sh
mkdir -p dist
alx pack examples/packages/http-reference -o dist/
sha256sum dist/http-reference-1.0.0.zip
```

El zip es determinista: entradas ordenadas, fecha fija (1980-01-01), permisos normalizados a `0644` (o `0755` si el archivo es ejecutable) y sin campos extra. Empaquetar dos veces el mismo árbol da el mismo `sha256`, aunque los archivos se hayan creado en otro orden o tengan otras fechas. El valor depende de la versión de zlib de la máquina, así que compara resultados obtenidos con el mismo intérprete.

Si no indicas `-o`, el archivo se llama `NOMBRE-VERSION.zip` y se escribe en el directorio actual.

## Publicar

Hace falta una credencial con ámbito `write`. Con un usuario existente se obtiene un JWT y, con él, un token de API que se muestra una sola vez:

```sh
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "TU_USUARIO", "password": "TU_CLAVE"}'

curl -s -X POST http://localhost:8000/api/v1/tokens \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name": "ejemplos", "scope": "write", "expires_in_days": 30}'
```

La segunda respuesta trae el campo `token` (`alx_...`). Guárdalo en el entorno en lugar de pasarlo en cada comando, para que no quede en el historial del intérprete:

```sh
export ALX_URL=http://localhost:8000
export ALX_TOKEN=alx_...
alx publish examples/packages/changelog-from-commits --publish
alx publish examples/packages/http-reference --publish
alx publish examples/packages/incident-postmortem --publish
```

Sin `--publish` la versión queda como borrador y solo la ve su dueño. Las versiones son inmutables: volver a subir `1.0.0` devuelve `CONFLICT`; para publicar un cambio hay que subir otra versión (`2.0.0`, `1.1.0`...) cambiando `version` en el frontmatter.

## Consumir

```sh
alx search http
alx search --kind grounding --tag reference
alx download http-reference -o /tmp/
alx pull http-reference -d ./refs
```

`alx pull` descarga, comprueba el `sha256` anunciado por el servidor, vuelve a validar el zip y solo entonces extrae los archivos del manifiesto. Nunca escribe fuera del directorio destino, rechaza enlaces simbólicos y no sobrescribe nada sin `--force`.

Con la base SQLite ya extraída se pueden hacer consultas locales:

```sh
sqlite3 refs/db/http-reference.sqlite \
  "SELECT code, name FROM status_codes WHERE class = 'client_error' AND cacheable_by_default = 1"
```

Por MCP el agente lee los archivos de texto sin descargar nada: `get_entrypoint` devuelve `INDEX.md` o `SKILL.md`, `list_files` el manifiesto y `read_file` cualquier archivo de texto, por ejemplo `tables/status_codes.csv`. Los binarios, como la base SQLite, solo se obtienen descargando el paquete.

## Probar los scripts de los ejemplos

Los scripts usan solo la biblioteca estándar:

```sh
printf 'abc1234\037feat(api): add pagination\037\036' | \
  python examples/packages/changelog-from-commits/scripts/format_changelog.py --version 1.1.0

python examples/packages/incident-postmortem/scripts/render_postmortem.py \
  examples/packages/incident-postmortem/references/example-incident.json -o postmortem.html
```

## Regenerar la base SQLite

Los CSV de `packages/http-reference/tables/` son la fuente de verdad. Después de editarlos:

```sh
python examples/packages/http-reference/scripts/build_db.py
```

El script valida columnas y valores, comprueba las claves foráneas, construye el archivo en una ubicación temporal y lo coloca de forma atómica. No guarda fechas ni valores aleatorios: con la misma versión de SQLite produce siempre los mismos bytes. Tras regenerar la base hay que subir una versión nueva del paquete.

## Añadir un ejemplo

1. Crea `examples/packages/<nombre>/` con `SKILL.md` o `INDEX.md` y su frontmatter completo (`name`, `description`, `version`, `tags`, `category`, además de `display_name`).
2. Ejecuta `alx validate examples/packages/<nombre>`.
3. Registra el ejemplo, con su `kind`, nombre y versión esperados, en `tests/test_examples.py`: esa prueba falla si hay un directorio sin registrar.

## Pruebas

```sh
pytest tests/test_examples.py
```

La prueba valida cada ejemplo con el mismo inspector que usa el servidor, comprueba que el empaquetado es determinista, ejecuta los scripts, regenera la base SQLite y la compara con la incluida, y publica y vuelve a descargar los tres paquetes contra un registro simulado.
