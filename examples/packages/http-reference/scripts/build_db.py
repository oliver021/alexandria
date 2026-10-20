from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TABLES = PACKAGE_ROOT / "tables"
DEFAULT_OUTPUT = PACKAGE_ROOT / "db" / "http-reference.sqlite"

SCHEMA_VERSION = 1
PAGE_SIZE = 4096

SCHEMA = """
CREATE TABLE status_codes (
    code INTEGER PRIMARY KEY CHECK (code BETWEEN 100 AND 599),
    name TEXT NOT NULL,
    class TEXT NOT NULL CHECK (
        class IN ('informational', 'success', 'redirection', 'client_error', 'server_error')
    ),
    summary TEXT NOT NULL,
    cacheable_by_default INTEGER NOT NULL CHECK (cacheable_by_default IN (0, 1)),
    rfc TEXT NOT NULL
);

CREATE TABLE methods (
    name TEXT PRIMARY KEY,
    safe INTEGER NOT NULL CHECK (safe IN (0, 1)),
    idempotent INTEGER NOT NULL CHECK (idempotent IN (0, 1)),
    typical_success TEXT NOT NULL,
    summary TEXT NOT NULL,
    rfc TEXT NOT NULL
);

CREATE TABLE scenarios (
    id INTEGER PRIMARY KEY,
    scenario TEXT NOT NULL UNIQUE,
    status INTEGER NOT NULL REFERENCES status_codes (code),
    headers TEXT NOT NULL,
    notes TEXT NOT NULL
);

CREATE INDEX idx_status_codes_class ON status_codes (class);
CREATE INDEX idx_scenarios_status ON scenarios (status);

CREATE VIEW scenario_details AS
SELECT
    scenarios.id AS id,
    scenarios.scenario AS scenario,
    scenarios.status AS status,
    status_codes.name AS status_name,
    status_codes.class AS status_class,
    scenarios.headers AS headers,
    scenarios.notes AS notes
FROM scenarios
JOIN status_codes ON status_codes.code = scenarios.status;
"""

STATUS_COLUMNS = ["code", "name", "class", "summary", "cacheable_by_default", "rfc"]
METHOD_COLUMNS = ["method", "safe", "idempotent", "typical_success", "summary", "rfc"]
SCENARIO_COLUMNS = ["scenario", "status", "headers", "notes"]
FLAGS = {"yes": 1, "no": 0}


class BuildError(Exception):
    pass


def read_table(path: Path, columns: list[str]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != columns:
            raise BuildError(
                f"{path.name}: expected the columns {columns}, found {reader.fieldnames}"
            )
        rows = list(reader)
    for number, row in enumerate(rows, start=2):
        if None in row or any(value is None for value in row.values()):
            raise BuildError(f"{path.name}: line {number} has the wrong number of fields")
    return rows


def flag(value: str, path: Path, column: str) -> int:
    if value not in FLAGS:
        raise BuildError(f"{path.name}: {column} must be yes or no, found {value!r}")
    return FLAGS[value]


def status_rows(path: Path) -> list[tuple[int, str, str, str, int, str]]:
    return [
        (
            int(row["code"]),
            row["name"],
            row["class"],
            row["summary"],
            flag(row["cacheable_by_default"], path, "cacheable_by_default"),
            row["rfc"],
        )
        for row in read_table(path, STATUS_COLUMNS)
    ]


def method_rows(path: Path) -> list[tuple[str, int, int, str, str, str]]:
    return [
        (
            row["method"],
            flag(row["safe"], path, "safe"),
            flag(row["idempotent"], path, "idempotent"),
            row["typical_success"],
            row["summary"],
            row["rfc"],
        )
        for row in read_table(path, METHOD_COLUMNS)
    ]


def scenario_rows(path: Path) -> list[tuple[int, str, int, str, str]]:
    return [
        (number, row["scenario"], int(row["status"]), row["headers"], row["notes"])
        for number, row in enumerate(read_table(path, SCENARIO_COLUMNS), start=1)
    ]


def build(tables: Path, output: Path) -> dict[str, int]:
    statuses = status_rows(tables / "status_codes.csv")
    methods = method_rows(tables / "methods.csv")
    scenarios = scenario_rows(tables / "scenarios.csv")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    partial.unlink(missing_ok=True)
    connection = sqlite3.connect(partial)
    try:
        connection.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        connection.executemany("INSERT INTO status_codes VALUES (?, ?, ?, ?, ?, ?)", statuses)
        connection.executemany("INSERT INTO methods VALUES (?, ?, ?, ?, ?, ?)", methods)
        connection.executemany("INSERT INTO scenarios VALUES (?, ?, ?, ?, ?)", scenarios)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
        connection.execute("VACUUM")
    except (sqlite3.Error, BuildError):
        connection.close()
        partial.unlink(missing_ok=True)
        raise
    connection.close()
    os.replace(partial, output)
    return {"status_codes": len(statuses), "methods": len(methods), "scenarios": len(scenarios)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the SQLite copy of the tables of this package."
    )
    parser.add_argument(
        "--tables", type=Path, default=DEFAULT_TABLES, help="directory with the CSV files"
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT, help="database file to write"
    )
    args = parser.parse_args(argv)
    try:
        counts = build(args.tables, args.output)
    except (BuildError, OSError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    summary = ", ".join(f"{count} {name}" for name, count in counts.items())
    print(f"wrote {args.output} with {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
