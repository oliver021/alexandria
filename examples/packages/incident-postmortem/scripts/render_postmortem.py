from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "postmortem.html"
SEVERITIES = ("SEV1", "SEV2", "SEV3", "SEV4")
STATUSES = ("investigating", "mitigated", "resolved")
PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")

SCALARS = ("title", "incident_id", "severity", "status", "started_at")
PARAGRAPHS = ("summary", "impact", "root_cause")
OPTIONAL_LISTS = ("what_went_well", "what_went_wrong")


class InvalidIncident(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def text_of(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def check_scalars(data: dict, problems: list[str]) -> None:
    for field in (*SCALARS, *PARAGRAPHS):
        if text_of(data.get(field)) is None:
            problems.append(f"{field}: a non-empty string is required")
    if data.get("severity") not in SEVERITIES:
        problems.append(f"severity: must be one of {', '.join(SEVERITIES)}")
    if data.get("status") not in STATUSES:
        problems.append(f"status: must be one of {', '.join(STATUSES)}")


def check_times(data: dict, problems: list[str]) -> str:
    started = text_of(data.get("started_at"))
    resolved = data.get("resolved_at")
    started_time = parse_time(started) if started else None
    if started and started_time is None:
        problems.append("started_at: must be an ISO 8601 timestamp such as 2026-10-02T09:14:00Z")
    if resolved is None:
        return "n/a"
    resolved_text = text_of(resolved)
    resolved_time = parse_time(resolved_text) if resolved_text else None
    if resolved_time is None:
        problems.append("resolved_at: must be an ISO 8601 timestamp or be left out")
        return "n/a"
    if started_time is None:
        return "n/a"
    if (started_time.tzinfo is None) != (resolved_time.tzinfo is None):
        problems.append("resolved_at: use the same timezone convention as started_at")
        return "n/a"
    seconds = int((resolved_time - started_time).total_seconds())
    if seconds < 0:
        problems.append("resolved_at: must not be earlier than started_at")
        return "n/a"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def check_records(data: dict, field: str, keys: tuple[str, ...], problems: list[str]) -> None:
    items = data.get(field)
    if not isinstance(items, list) or not items:
        problems.append(f"{field}: a non-empty list is required")
        return
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict) or any(text_of(item.get(key)) is None for key in keys):
            problems.append(f"{field}[{position}]: needs the text fields {', '.join(keys)}")


def check_notes(data: dict, field: str, problems: list[str]) -> None:
    items = data.get(field, [])
    if not isinstance(items, list) or any(text_of(item) is None for item in items):
        problems.append(f"{field}: must be a list of non-empty strings")


def validate(data: object) -> str:
    if not isinstance(data, dict):
        raise InvalidIncident(["the input must be a JSON object"])
    problems: list[str] = []
    check_scalars(data, problems)
    duration = check_times(data, problems)
    check_records(data, "timeline", ("time", "event"), problems)
    check_records(data, "action_items", ("owner", "action", "due"), problems)
    for field in OPTIONAL_LISTS:
        check_notes(data, field, problems)
    if problems:
        raise InvalidIncident(problems)
    return duration


def escape(value: object) -> str:
    return html.escape(str(value).strip(), quote=True)


def paragraphs(value: str) -> str:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", value) if block.strip()]
    return "\n".join(f"<p>{escape(' '.join(block.split()))}</p>" for block in blocks)


def rows(items: list[dict], keys: tuple[str, ...], classes: tuple[str, ...]) -> str:
    lines = []
    for item in items:
        cells = "".join(
            f'<td class="{css}">{escape(item[key])}</td>'
            if css
            else f"<td>{escape(item[key])}</td>"
            for key, css in zip(keys, classes, strict=True)
        )
        lines.append(f"<tr>{cells}</tr>")
    return "\n".join(lines)


def bullets(items: list[str]) -> str:
    if not items:
        return "<li>Nothing recorded.</li>"
    return "\n".join(f"<li>{escape(item)}</li>" for item in items)


def build_values(data: dict, duration: str) -> dict[str, str]:
    values = {field: escape(data[field]) for field in SCALARS}
    values.update({field: paragraphs(data[field]) for field in PARAGRAPHS})
    values["resolved_at"] = escape(data["resolved_at"]) if data.get("resolved_at") else "ongoing"
    values["duration"] = duration
    values["timeline_rows"] = rows(data["timeline"], ("time", "event"), ("time", ""))
    values["action_rows"] = rows(data["action_items"], ("owner", "action", "due"), ("", "", ""))
    values["went_well_items"] = bullets(data.get("what_went_well", []))
    values["went_wrong_items"] = bullets(data.get("what_went_wrong", []))
    return values


def render(template: str, values: dict[str, str]) -> str:
    unknown = sorted(set(PLACEHOLDER.findall(template)) - set(values))
    if unknown:
        raise InvalidIncident([f"the template uses unknown placeholders: {', '.join(unknown)}"])
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a postmortem report from a JSON file.")
    parser.add_argument("input", help="JSON file that describes the incident")
    parser.add_argument("-o", "--output", help="file to write (default: standard output)")
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="HTML template to fill")
    args = parser.parse_args(argv)
    try:
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        duration = validate(data)
        template = Path(args.template).read_text(encoding="utf-8")
        report = render(template, build_values(data, duration))
        if args.output:
            Path(args.output).write_text(report, encoding="utf-8")
        else:
            sys.stdout.write(report)
    except InvalidIncident as error:
        for problem in error.problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
