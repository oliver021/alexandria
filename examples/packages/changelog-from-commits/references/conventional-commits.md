# Conventional Commits: working summary

Conventional Commits is a lightweight convention for commit messages that makes history readable by people and tools. This page paraphrases version 1.0.0 of the specification for quick reference.

## Shape of a message

```
<type>[optional scope][!]: <description>

[optional body]

[optional footers]
```

- The type is a noun such as `feat` or `fix`, followed by an optional scope in parentheses, an optional `!` and a colon with a space.
- The description is a short summary written in the imperative mood, without a final period.
- The body adds context: what changed and why, not how.
- Footers carry metadata such as `Refs: #123` or `BREAKING CHANGE: description`.

## Types

| Type | Meaning | Release impact |
|---|---|---|
| `feat` | A new feature for the user | minor |
| `fix` | A bug fix for the user | patch |
| `perf` | A change that improves performance | patch |
| `refactor` | A change that neither fixes a bug nor adds a feature | patch |
| `revert` | Reverts an earlier commit | patch |
| `docs` | Documentation only | none |
| `test` | Tests only | none |
| `build` | Build system or dependencies | none |
| `ci` | Continuous integration configuration | none |
| `chore` | Maintenance that does not touch the product | none |
| `style` | Formatting that does not change behavior | none |

The specification only requires `feat` and `fix`; the other types come from common conventions such as the Angular guidelines. Projects add their own, for example `deprecate` or `security`, and the formatter of this skill understands both.

## Breaking changes

A change is breaking when users must change something to keep working. It is signalled in one of two ways, and either one means a major release:

- A `!` right before the colon: `refactor(api)!: rename the page parameter to cursor`.
- A footer that starts with `BREAKING CHANGE:` (the hyphenated form `BREAKING-CHANGE:` is equivalent), followed by an explanation.

## Relation to semantic versioning

| Commit content | Version part that grows |
|---|---|
| A breaking change of any type | MAJOR |
| `feat` | MINOR |
| `fix`, `perf`, `refactor`, `revert` | PATCH |

While the major version is 0 the public API is not considered stable, so projects often release breaking changes as minor versions. Ask the user how they want to handle it.

## Examples

```
feat(search): add tag filters

fix: stop crashing on empty queries

Closes #482.

refactor!: drop support for the legacy token format

BREAKING CHANGE: tokens issued before 2.0 are rejected. Issue new ones.

docs: explain the upload limits
```

## Reading messy histories

Many repositories mix conventional and free-form subjects. Treat free-form commits as unknown instead of guessing their type: list them separately, ask the user, or leave them out of the user-facing notes.
