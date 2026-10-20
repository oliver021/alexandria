---
name: incident-postmortem
display_name: Incident Postmortem
description: Write a blameless postmortem for a production incident from its timeline and render it as a self-contained HTML report with the bundled template. Use it after an outage, a degradation or a near miss, when the facts are known and the team needs a shareable write-up with action items.
version: 1.0.0
tags:
  - incident
  - postmortem
  - sre
  - html-template
  - reporting
category: operations
---

# Incident Postmortem

Turn what is known about an incident into a blameless postmortem and render it as a single HTML file that can be attached to a ticket or sent by mail. You collect and judge the facts; `scripts/render_postmortem.py` fills the template `templates/postmortem.html` and takes care of escaping.

A postmortem exists to learn, so it describes how the system and the process allowed the failure. It does not rank people.

## When to use

- An incident is resolved (or mitigated) and the team needs the written review.
- A near miss deserves the same analysis as an outage.
- Someone asks for "the postmortem", "the RCA" or "an incident report".

Do not use it while the incident is still being fought: first restore service, then write. Do not use it for performance reviews or for assigning fault.

## What you need

| Input | Where to get it |
|---|---|
| Timeline with timestamps | Alerts, deploy logs, chat transcript, ticket history. Use UTC. |
| Impact | Dashboards, support tickets, error budgets. Who was affected, for how long, how badly. |
| Severity | [references/severity-levels.md](references/severity-levels.md). |
| People to name as owners | Only for action items, never for causes. |

If a fact is missing, ask for it or write that it is unknown. Never fill gaps with guesses.

## Procedure

1. **Gather the timeline.** List events in chronological order, each with a UTC time and one factual sentence: detection, escalation, each mitigation attempt, the change that fixed it, the confirmation of recovery. Keep opinions out of this list.

2. **Set the severity and the status.** Pick `SEV1` to `SEV4` using the severity reference. The status is `investigating`, `mitigated` or `resolved`.

3. **Write the impact.** Say who was affected, what they could not do, for how long and how many were affected, with numbers when they exist. Separate customer impact from internal impact.

4. **Find the contributing causes.** Ask "why" repeatedly, until you reach a condition of the system or of the process that can be changed: a missing alert, an unsafe default, a risky manual step, an untested path. Stop before you reach a person. Prefer "the deploy tool allowed a change without review" to "an engineer skipped the review".

5. **Record what went well and what went wrong.** What helped (fast detection, a good runbook, a clear escalation) deserves to be kept. What hurt (slow detection, missing runbook, confusing dashboards) feeds the action items.

6. **Define action items.** Each one has an owner, a concrete action written as a verb phrase and a due date. Include at least one that prevents recurrence and one that shortens detection or recovery. Vague items such as "be more careful" are not actions.

7. **Write the input file.** Copy [references/example-incident.json](references/example-incident.json) and replace its content. The fields are explained in [references/template-fields.md](references/template-fields.md).

8. **Render.** Run:

   ```sh
   python scripts/render_postmortem.py incident.json -o postmortem.html
   ```

   The script checks every field first and prints all problems at once. It exits with status 1 when the input is invalid and writes nothing in that case.

9. **Review the result.** Open the HTML file and read it as someone who was not there. Check that the timeline is in order, that no sentence assigns blame, that every action item is concrete and that times are in UTC.

10. **Share and follow up.** Send the file to the team, put the action items in the tracker and schedule a review of their status.

## Language rules

- Describe systems and decisions, not people. Say "the alert had no owner", not "nobody looked at the alert".
- State facts in the past tense and uncertainty explicitly ("it is not known whether...").
- Give times in UTC with the same format everywhere.
- Do not paste secrets, tokens, personal data or internal URLs that readers of the report should not see.

## The template

`templates/postmortem.html` is a self-contained page: inline styles, no scripts, no external requests, readable on screen and in print. It contains `{{placeholders}}`. The renderer replaces them with escaped values, so text containing `<`, `&` or quotes is displayed literally and cannot inject markup. You can edit the template to match a house style; keep the placeholders you still need. If a placeholder is unknown or a value is missing, the renderer stops instead of producing a broken page.

The registry serves HTML files as plain text, never as web pages. To see the report as a page, render it locally and open the file in a browser.

## Checklist before sending

- [ ] The summary fits in three sentences and states the outcome.
- [ ] Impact has numbers or says why there are none.
- [ ] The root cause is a condition of the system, not a person.
- [ ] Every timeline entry has a UTC time and a verifiable fact.
- [ ] Every action item has an owner, a verb and a due date.
- [ ] At least one action prevents recurrence and one improves detection or recovery.
- [ ] The report contains no secrets or personal data.

## Common mistakes

- Starting to write before service is restored.
- Naming a person as the cause and stopping the analysis there.
- Listing action items nobody owns.
- Mixing time zones in the timeline.
- Calling the cause "human error": it is a symptom, so ask what made the error possible.
