# Severity levels

Use the lowest number that matches the worst impact observed during the incident. The scale is a common starting point; replace the examples with the ones of your own service and keep the labels `SEV1` to `SEV4`, which the renderer accepts.

| Level | Impact | Typical examples | Expected response |
|---|---|---|---|
| `SEV1` | Critical. Most users cannot use the product, data is lost or exposed, or a legal or safety obligation is at risk. | Full outage, data corruption, a security breach in progress. | Page the on-call immediately, open a bridge, update stakeholders at a fixed interval until recovery. |
| `SEV2` | Major. A core feature is unavailable or badly degraded for many users, with no acceptable workaround. | Checkout fails for a region, sign-in is slow enough to time out. | Page the on-call, assign an incident lead, communicate status. |
| `SEV3` | Minor. A secondary feature is degraded or a small group is affected, and a workaround exists. | A report page fails, a batch job is late, one customer is blocked. | Handle during working hours, track in the ticket system. |
| `SEV4` | Low. Cosmetic problems or near misses with no user impact. | A dashboard shows stale numbers, a risky change was caught before release. | Backlog item. A short postmortem is optional but useful for near misses. |

## Choosing the level

1. Start from the worst moment of the incident, not from the average.
2. Count who was affected and for how long; scope and duration both matter.
3. Raise the level when data was lost, exposed or silently wrong, even if few users noticed.
4. If two levels fit, choose the higher one. Lowering it later is cheap.

## When a postmortem is expected

| Level | Postmortem |
|---|---|
| `SEV1`, `SEV2` | Always, within a few working days while memory is fresh. |
| `SEV3` | When the cause is not understood, when it repeated, or when the team wants to learn from it. |
| `SEV4` | Optional. Near misses are a cheap way to improve before something breaks. |
