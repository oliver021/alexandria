# Keep a Changelog: working summary

A changelog is a file that lists, for every release, the notable changes that matter to the people who use the software. This page summarizes the conventions of Keep a Changelog 1.1.0 that the skill applies. It is a paraphrase for quick reference, not the original text.

## Principles

- Changelogs are for humans. Write for users, not for machines or for the author of the commit.
- There is one entry per release, and the newest release comes first.
- Every release has a version and a date.
- Mention whether the project follows semantic versioning.
- Changes are grouped by kind so readers can find what matters to them.

## File layout

```markdown
# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

## [1.3.0] - 2026-10-16

### Added

- A new thing.

### Fixed

- A bug that is gone.

## [1.2.0] - 2026-08-02

...

[Unreleased]: https://example.org/compare/v1.3.0...HEAD
[1.3.0]: https://example.org/compare/v1.2.0...v1.3.0
[1.2.0]: https://example.org/compare/v1.1.0...v1.2.0
```

## The six kinds of change

| Heading | Use it for |
|---|---|
| Added | New features. |
| Changed | Changes in existing functionality, including performance work and refactors that users can notice. |
| Deprecated | Features that will be removed in a future release. |
| Removed | Features that were removed in this release. |
| Fixed | Bug fixes. |
| Security | Fixes for vulnerabilities, so users know when to upgrade. |

Always keep the headings in this order and leave out the ones that have no entries.

## Rules of thumb

- Dates use the ISO 8601 form `YYYY-MM-DD`.
- Keep an `## [Unreleased]` section at the top to collect changes before a release. When releasing, move its content into the new version section.
- Version headings are links to the comparison between two tags when the project hosts its code online.
- A yanked release keeps its entry and gets the tag `[YANKED]` after the date.
- Do not paste the commit log. Summarize, merge related changes and drop the noise.
- Deprecations are announced in one release and removed in a later one.
