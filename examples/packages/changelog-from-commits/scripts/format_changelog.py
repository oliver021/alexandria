from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

RECORD_SEPARATOR = "\x1e"
FIELD_SEPARATOR = "\x1f"
SHORT_HASH_LENGTH = 7
OTHER = "Other"

SUBJECT = re.compile(
    r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^()]+)\))?(?P<breaking>!)?: (?P<description>\S.*)$"
)
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE: (?P<note>.+)$", re.MULTILINE)
VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

SECTION_BY_TYPE = {
    "feat": "Added",
    "fix": "Fixed",
    "perf": "Changed",
    "refactor": "Changed",
    "revert": "Removed",
    "deprecate": "Deprecated",
    "security": "Security",
    "sec": "Security",
}
SECTION_ORDER = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security", OTHER]
PATCH_TYPES = {"fix", "perf", "refactor", "revert", "security", "sec", "deprecate"}


@dataclass(frozen=True)
class Entry:
    section: str
    kind: str
    scope: str | None
    description: str
    breaking: bool
    notes: tuple[str, ...]
    short_hash: str


def parse_records(text: str) -> list[tuple[str, str, str]]:
    records = []
    for chunk in text.split(RECORD_SEPARATOR):
        if not chunk.strip():
            continue
        fields = chunk.strip("\n").split(FIELD_SEPARATOR)
        if len(fields) < 2:
            continue
        body = fields[2] if len(fields) > 2 else ""
        records.append((fields[0].strip(), fields[1].strip(), body))
    return records


def classify(commit_hash: str, subject: str, body: str) -> Entry:
    short_hash = commit_hash[:SHORT_HASH_LENGTH]
    notes = tuple(match["note"].strip() for match in BREAKING_FOOTER.finditer(body))
    match = SUBJECT.match(subject)
    if match is None:
        section = "Changed" if notes else OTHER
        return Entry(section, "", None, subject, bool(notes), notes, short_hash)
    kind = match["type"].lower()
    breaking = bool(match["breaking"]) or bool(notes)
    section = SECTION_BY_TYPE.get(kind, "Changed" if breaking else OTHER)
    if breaking:
        section = "Changed"
    return Entry(
        section, kind, match["scope"], match["description"].strip(), breaking, notes, short_hash
    )


def suggest_bump(entries: list[Entry]) -> str:
    if any(entry.breaking for entry in entries):
        return "major"
    if any(entry.kind == "feat" for entry in entries):
        return "minor"
    if any(entry.kind in PATCH_TYPES for entry in entries):
        return "patch"
    return "none"


def render_entry(entry: Entry) -> list[str]:
    prefix = "**BREAKING:** " if entry.breaking else ""
    scope = f"**{entry.scope}:** " if entry.scope else ""
    lines = [f"- {prefix}{scope}{entry.description} ({entry.short_hash})"]
    lines.extend(f"  - {note}" for note in entry.notes)
    return lines


def render(entries: list[Entry], version: str, release_date: str, include_other: bool) -> str:
    lines = [f"## [{version}] - {release_date}", ""]
    visible = [entry for entry in entries if include_other or entry.section != OTHER]
    for section in SECTION_ORDER:
        members = [entry for entry in visible if entry.section == section]
        if not members:
            continue
        members.sort(key=lambda entry: not entry.breaking)
        lines.append(f"### {section}")
        lines.append("")
        for entry in members:
            lines.extend(render_entry(entry))
        lines.append("")
    if not visible:
        lines.extend(["_No notable changes._", ""])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Turn the records of scripts/collect_commits.sh into a changelog section."
    )
    parser.add_argument("--version", help="version of the release, for example 1.3.0")
    parser.add_argument("--date", help="release date as YYYY-MM-DD (default: today, UTC)")
    parser.add_argument("--input", help="file with the records (default: standard input)")
    parser.add_argument(
        "--include-other",
        action="store_true",
        help="also list non-conventional and internal commits under Other",
    )
    parser.add_argument(
        "--suggest-bump",
        action="store_true",
        help="print major, minor, patch or none instead of the section",
    )
    return parser


def read_input(path: str | None) -> str:
    if path is None:
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.suggest_bump:
        if args.version is None:
            parser.error("--version is required unless --suggest-bump is used")
        if not VERSION.fullmatch(args.version):
            parser.error(f"invalid version {args.version!r}: use MAJOR.MINOR.PATCH")
    if args.date is not None and not DATE.fullmatch(args.date):
        parser.error(f"invalid date {args.date!r}: use YYYY-MM-DD")
    try:
        stream = read_input(args.input)
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    entries = [classify(*record) for record in parse_records(stream)]
    if args.suggest_bump:
        print(suggest_bump(entries))
        return 0
    skipped = sum(1 for entry in entries if entry.section == OTHER)
    if skipped and not args.include_other:
        print(
            f"note: {skipped} commit(s) left out; use --include-other to list them", file=sys.stderr
        )
    release_date = args.date or datetime.now(UTC).date().isoformat()
    print(render(entries, args.version, release_date, args.include_other))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
