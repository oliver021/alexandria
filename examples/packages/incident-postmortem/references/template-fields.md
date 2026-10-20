# Input fields of the renderer

`scripts/render_postmortem.py` reads one JSON object and fills the placeholders of `templates/postmortem.html`. All text is escaped before it enters the page. A copy of this shape with realistic values is in [example-incident.json](example-incident.json).

## Fields of the JSON file

| Field | Type | Required | Notes |
|---|---|---|---|
| `title` | string | yes | Short name of the incident. |
| `incident_id` | string | yes | Identifier from the tracker, for example `INC-2026-041`. |
| `severity` | string | yes | One of `SEV1`, `SEV2`, `SEV3`, `SEV4`. |
| `status` | string | yes | One of `investigating`, `mitigated`, `resolved`. |
| `started_at` | string | yes | ISO 8601 timestamp, preferably in UTC: `2026-10-02T09:14:00Z`. |
| `resolved_at` | string | no | ISO 8601 timestamp using the same convention as `started_at`. When it is left out the page says `ongoing` and the duration is `n/a`. |
| `summary` | string | yes | What happened and how it ended, in a few sentences. Blank lines separate paragraphs. |
| `impact` | string | yes | Who was affected, how and for how long. Blank lines separate paragraphs. |
| `root_cause` | string | yes | The conditions of the system or process that allowed the failure. Blank lines separate paragraphs. |
| `timeline` | list | yes | Not empty. Each item is an object with the strings `time` and `event`. |
| `what_went_well` | list of strings | no | Things worth keeping. |
| `what_went_wrong` | list of strings | no | Things that made the incident longer or harder. |
| `action_items` | list | yes | Not empty. Each item is an object with the strings `owner`, `action` and `due`. |

## Placeholders of the template

| Placeholder | Filled with |
|---|---|
| `{{title}}`, `{{incident_id}}`, `{{severity}}`, `{{status}}`, `{{started_at}}` | The escaped value, as inline text. |
| `{{resolved_at}}` | The escaped value, or `ongoing`. |
| `{{duration}}` | Computed from the two timestamps, such as `1h 12m`, or `n/a`. |
| `{{summary}}`, `{{impact}}`, `{{root_cause}}` | One `<p>` element per paragraph. Place them where block content is allowed. |
| `{{timeline_rows}}`, `{{action_rows}}` | `<tr>` elements. Place them inside a `<tbody>`. |
| `{{went_well_items}}`, `{{went_wrong_items}}` | `<li>` elements. Place them inside a `<ul>`. |

The renderer replaces each placeholder once and never expands the text of the data, so a value that contains `{{title}}` is shown literally. A placeholder that is not in this table makes the renderer stop with an error.

## Exit status

| Code | Meaning |
|---|---|
| 0 | The report was written. |
| 1 | The input or the template is invalid, or a file cannot be read or written. Every problem found is printed to standard error. |
| 2 | The command line is wrong. |
