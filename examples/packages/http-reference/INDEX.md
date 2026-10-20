---
name: http-reference
display_name: HTTP Reference
description: Reference tables and specifications for designing and debugging HTTP APIs, with status codes, methods, error scenarios, the problem details format and a ready-to-query SQLite copy of the tables.
version: 1.0.0
tags:
  - http
  - api
  - reference
  - status-codes
  - rest
category: api-design
---

# HTTP Reference

Grounding data for questions about HTTP in API work: which status code fits a situation, whether a method is safe or idempotent, how to shape an error body, and which codes caches may reuse. Use it to answer from the tables instead of from memory.

The tables paraphrase RFC 9110 (HTTP semantics) and the RFCs named in their `rfc` column. They are a convenience, not a replacement for the standards; when a decision is contractual, cite the RFC.

## Files

| Path | What it holds | Read it with |
|---|---|---|
| `tables/status_codes.csv` | 53 registered status codes with class, meaning and default cacheability. | `read_file` |
| `tables/methods.csv` | The 9 request methods with safety, idempotency and usual success codes. | `read_file` |
| `tables/scenarios.csv` | 21 everyday situations and the status code, headers and notes that fit them. | `read_file` |
| `spec/problem-details.yaml` | JSON Schema (as YAML) of `application/problem+json`, with examples. | `read_file` |
| `site/cheatsheet.html` | One page summary: the five classes, a decision list and the method matrix. | `read_file` (served as text, never as a page) |
| `db/http-reference.sqlite` | The three tables above plus the `scenario_details` view, indexed. | download the package and query it locally |
| `scripts/build_db.py` | Rebuilds the SQLite file from the CSV files. | run it with Python 3.11 or newer |

All text files are UTF-8 and small enough to be read inline. The SQLite file is binary: it cannot be read through MCP or `read_file`, so fetch the package with `alx pull http-reference -d ./http-reference` (or download the zip) when you need joins or filters.

## How to answer common questions

| Question | Where to look |
|---|---|
| What does 409 mean, and when is it better than 422? | `status_codes.csv` for the meaning, `scenarios.csv` for the guidance. |
| Which status should an API return when a duplicate is created? | `scenarios.csv`, the row about a duplicate create or state conflict. |
| Can a response with status 404 be cached without explicit headers? | `status_codes.csv`, column `cacheable_by_default` (yes). |
| Is PUT idempotent? Is PATCH? | `methods.csv`, columns `safe` and `idempotent`. |
| How should an error body look? | `spec/problem-details.yaml`. |

## Data dictionary

### `tables/status_codes.csv`

| Column | Meaning |
|---|---|
| `code` | Three digit status code, from 100 to 599. Unique. |
| `name` | Registered reason phrase, in the wording of RFC 9110 where it was renamed. |
| `class` | `informational`, `success`, `redirection`, `client_error` or `server_error`. |
| `summary` | One sentence on what the code tells the client. |
| `cacheable_by_default` | `yes` when a cache may reuse the response without explicit freshness information (the heuristically cacheable codes of RFC 9110), `no` otherwise. |
| `rfc` | The document that defines the code. |

### `tables/methods.csv`

| Column | Meaning |
|---|---|
| `method` | Method token, in upper case. Unique. |
| `safe` | `yes` when the method does not ask the server to change anything. |
| `idempotent` | `yes` when repeating the request leaves the server in the same state as sending it once. |
| `typical_success` | Status codes that usually answer a successful request. |
| `summary` | One sentence on the purpose of the method. |
| `rfc` | The document that defines the method. |

### `tables/scenarios.csv`

| Column | Meaning |
|---|---|
| `scenario` | A situation an API has to answer, in plain words. Unique. |
| `status` | The status code recommended for it. Always present in `status_codes.csv`. |
| `headers` | Response headers that normally go with that status, empty when none. |
| `notes` | Guidance and pitfalls. This table is opinionated advice, not part of any standard. |

## Quick answers

The codes API authors look up most, in the same format as the table:

```csv
code,name,class
200,OK,success
201,Created,success
204,No Content,success
400,Bad Request,client_error
401,Unauthorized,client_error
403,Forbidden,client_error
404,Not Found,client_error
409,Conflict,client_error
422,Unprocessable Content,client_error
429,Too Many Requests,client_error
500,Internal Server Error,server_error
503,Service Unavailable,server_error
```

Rules of thumb that hold in almost every API:

- `401` means "who are you?" and `403` means "I know who you are, and no".
- `400` is for requests that cannot be parsed; `422` is for requests that parse and then break a rule.
- `404` is also the right answer when a caller must not learn that something exists.
- `409` and `412` protect against clashes; `429` and `503` ask the client to come back later.
- Errors are easier to handle as `application/problem+json` than as free text.

## Querying the SQLite copy

The database has the tables `status_codes`, `methods` and `scenarios`, the view `scenario_details` and `PRAGMA user_version` set to 1. Booleans are stored as 0 and 1.

```sql
SELECT code, name FROM status_codes
WHERE class = 'client_error' AND cacheable_by_default = 1
ORDER BY code;

SELECT name FROM methods WHERE idempotent = 1 AND safe = 0;

SELECT scenario, status, status_name, headers
FROM scenario_details
WHERE status_class = 'client_error'
ORDER BY status;
```

Run them with `sqlite3 db/http-reference.sqlite` or from Python with the `sqlite3` module, opening the file read-only.

## Rebuilding the database

The CSV files are the source of truth. After editing them, run:

```sh
python scripts/build_db.py
```

The script validates the columns and the flags, checks that every scenario points to a known status code, builds the file in a temporary location and replaces `db/http-reference.sqlite` atomically. For the same SQLite version the result is byte for byte identical on every run, because it inserts rows in file order and stores no timestamps.

## Limits of this package

- It covers the registered codes and methods that API work meets most often, not the whole IANA registry.
- Reason phrases and summaries are paraphrases; quote the RFC when exact wording matters.
- `scenarios.csv` reflects common practice. Some teams choose differently, for example `400` instead of `422`; follow the conventions of the API you are working on when they exist.
