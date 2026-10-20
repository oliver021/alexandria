---
name: changelog-from-commits
display_name: Changelog from Commits
description: Turn a range of Conventional Commits into a Keep a Changelog release section, review it and add it to CHANGELOG.md. Use it when preparing release notes, bumping a version or answering what changed since the last release.
version: 1.0.0
tags:
  - changelog
  - git
  - release
  - conventional-commits
category: release-management
---

# Changelog from Commits

Produce a release section for `CHANGELOG.md` from the git history of a repository. The section follows [Keep a Changelog](references/keep-a-changelog.md) and reads the commits as [Conventional Commits](references/conventional-commits.md). Two small scripts do the mechanical work (collecting and grouping); you do the part that needs judgement (choosing the version, rewriting entries for humans, checking the result).

## When to use

- The user asks for release notes, a changelog entry or "what changed since the last release".
- A version is about to be tagged and `CHANGELOG.md` has to be updated.
- A pull request needs a summary of the commits it contains.

Do not use it to summarize uncommitted work, to rewrite the history of old releases, or for repositories that have no git history.

## What you need

| Input | How to get it |
|---|---|
| Repository | The current working directory, with `git` available. |
| Commit range | Default: from the latest tag to `HEAD`. Pass an explicit range such as `v1.2.0..HEAD` when the user names one. |
| Version number | Ask the user. If they have no preference, derive it in step 3. |
| Release date | Today in UTC, unless the user gives another date. |

## Procedure

1. **Check the repository.** Run `git status --short` and `git describe --tags --abbrev=0`. If there are uncommitted changes, tell the user they will not appear in the notes. If there is no tag, the whole history is used.

2. **Collect the commits.** Run `scripts/collect_commits.sh` (add a range as its first argument when the user gave one). Its output is a stream of records meant for the next script, not for reading.

3. **Choose the version.** Pipe the stream into `python scripts/format_changelog.py --suggest-bump`. It prints `major`, `minor`, `patch` or `none`:
   - `major`: some commit is marked as a breaking change.
   - `minor`: some commit adds a feature and none breaks compatibility.
   - `patch`: there are only fixes, performance work or refactors.
   - `none`: nothing user-visible changed. Confirm with the user before releasing.

   Apply the bump to the previous version (for versions below 1.0.0 ask the user how they want breaking changes numbered).

4. **Generate the draft.** Run the two scripts together:

   ```sh
   scripts/collect_commits.sh | python scripts/format_changelog.py --version 1.3.0
   ```

   Add `--date YYYY-MM-DD` to override the date and `--include-other` to keep commits that are not conventional or that are internal (`docs`, `test`, `chore`, `ci`, `build`, `style`). By default those are left out and a note on stderr says how many were skipped.

5. **Edit the draft for humans.** The script groups and formats; it cannot judge. Go through every bullet:
   - Rewrite it from the point of view of someone who uses the software, not of someone who wrote the commit.
   - Merge bullets that describe the same change (a feature and its follow-up fixes become one line).
   - Drop entries that only matter to maintainers.
   - Keep the `**BREAKING:**` markers and explain in one sentence what users must change.
   - Never invent changes that are not in the commits.

6. **Insert the section.** Open `CHANGELOG.md`. Place the new section directly below the `## [Unreleased]` heading (create the heading if the file lacks it) and above the previous release. Update the link references at the bottom of the file when the project uses them, for example `[1.3.0]: https://example.org/compare/v1.2.0...v1.3.0`.

7. **Verify.** Use the checklist below and show the final section to the user before committing anything.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/collect_commits.sh [RANGE]` | Prints the commits of a range (merge commits excluded) as records of hash, subject and body. Without a range it uses the latest tag up to `HEAD`. |
| `scripts/format_changelog.py` | Reads those records from stdin (or `--input FILE`) and prints a Markdown section. `--version` is required unless `--suggest-bump` is used. |

The mapping from commit type to section:

| Commit type | Section |
|---|---|
| `feat` | Added |
| `fix` | Fixed |
| `perf`, `refactor` | Changed |
| `deprecate` | Deprecated |
| `revert` | Removed |
| `security`, `sec` | Security |
| any type with `!` or a `BREAKING CHANGE:` footer | Changed, marked **BREAKING** |
| `docs`, `test`, `chore`, `ci`, `build`, `style`, non-conventional subjects | left out (or **Other** with `--include-other`) |

## Example

Input commits:

```
feat(api): add pagination to the search endpoint
fix: stop crashing on empty queries
refactor!: rename the page parameter to cursor
chore: update lock file
```

Draft produced for version 2.0.0:

```markdown
## [2.0.0] - 2026-10-16

### Added

- **api:** add pagination to the search endpoint (a1b2c3d)

### Changed

- **BREAKING:** rename the page parameter to cursor (e4f5a6b)

### Fixed

- stop crashing on empty queries (c7d8e9f)
```

## Checklist before finishing

- [ ] The version follows semantic versioning and matches the bump of step 3.
- [ ] The date is in ISO 8601 form (`YYYY-MM-DD`).
- [ ] Every breaking change is marked and says what users must do.
- [ ] No bullet mentions internal tooling, file names or commit hashes unless the project wants them.
- [ ] Sections are in the order Added, Changed, Deprecated, Removed, Fixed, Security, and empty sections are absent.
- [ ] The section sits below `## [Unreleased]` and above the previous release.

## Common mistakes

- Copying commit subjects verbatim. They are written for reviewers; changelog entries are written for users.
- Listing every commit. A changelog is a curated summary, not a log.
- Using local time or an ambiguous date format.
- Forgetting the comparison links at the bottom of the file in projects that keep them.
- Releasing with the `none` suggestion without asking: a release without user-visible changes is usually a mistake.
