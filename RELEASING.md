# Releasing mom-bot

Authoritative release process for the repo's `v*` tags. This document exists because siege-web's v1.3.0 release cut the tag before updating `CHANGELOG.md` or version sources — the exact footgun this checklist prevents.

## What a release does

**A release = publishing artifacts. A release is NOT a deploy.**

Pushing a `v*` tag triggers `.github/workflows/release.yml` (added in #215), which publishes two artifacts: a GitHub Release page and an immutable GHCR image tagged `:vX.Y.Z` at `ghcr.io/glitchwerks/mom-bot`. The mutable `:main` tag continues to track HEAD as before — the versioned tag is an additional pointer to the same SHA, not a replacement. Deploying the new image to the Azure Container App is a separate step, performed manually via `workflow_dispatch` on `.github/workflows/deploy.yml`. The tag-push does not trigger a deploy. You decide when prod gets the new image.

## Pre-tag checklist

Run through every item **before** running `git tag`. The metadata updates (CHANGELOG, version) must land in a commit on `main` that the tag will point at. Tagging before these steps produces an inconsistency between the image label, the `/api/version` response, and the in-app changelog — the failure mode this document exists to prevent.

### 1. Determine the new version

Follow semver from v1.0.0 onward. See [Versioning policy](#versioning-policy) below for what counts as a breaking change, minor addition, or patch fix.

```bash
git fetch --tags origin
git log v<previous>..main --oneline
```

Review that log to determine the appropriate bump.

### 2. Finalize `CHANGELOG.md`

Every PR merged since the previous tag should have an entry under `## [Unreleased]` in `CHANGELOG.md`. The release-cutter is responsible for filling any gaps before tagging.

- Replace `## [Unreleased]` with:
  ```
  ## [Unreleased]

  ## [X.Y.Z] - YYYY-MM-DD
  ```
  (Preserve an empty `[Unreleased]` heading so the next cycle has somewhere to land entries.)
- Use today's date in ISO 8601 (`YYYY-MM-DD`).
- Sub-section order: `### Added`, `### Changed`, `### Fixed`, `### Infrastructure`, `### Documentation`. Omit empty sub-sections.
- Keep-a-Changelog format: https://keepachangelog.com/

### 3. Bump the version in two places

mom-bot's version is defined in two places that must stay in sync:

- **`pyproject.toml`** — `[project] version = "X.Y.Z"` (line 6)
- **`src/mom_bot/__init__.py`** — `__version__ = "X.Y.Z"` (line 7)

`__version__` is what `/ping` and `GET /api/version` report at runtime. Both files must be updated in the same commit as the CHANGELOG promotion.

> **Note:** These two fields are currently hardcoded independently. If the project is later wired to use `importlib.metadata.version("mom-bot")` at runtime, only `pyproject.toml` will need updating. Until that change lands, update both.

### 4. Open a release PR

Branch name: `chore/release-vX.Y.Z`.

PR title: `chore(release): vX.Y.Z`.

PR body must contain:
- The release notes (same content as the `[X.Y.Z]` section of `CHANGELOG.md`)
- Any PRs from `v<previous>..HEAD` deliberately skipped from the changelog, with a one-line justification
- `Closes #<release-tracking-issue>` for the milestone tracking issue

Merge via squash-merge after CI is green.

### 5. Verify CI is green on the merge commit

After the release PR squash-merges, `build-image.yml` builds and pushes the GHCR image for that SHA. Before tagging, confirm:

- The CI workflow (`ci.yml`) shows green on the merge commit
- The `build-image.yml` run for that SHA completed successfully
- The image is reachable: `docker manifest inspect ghcr.io/glitchwerks/mom-bot:<merge-sha>`

### 6. Manual prod smoke on the candidate image

Before tagging, verify the candidate image works in prod:

1. Dispatch `deploy.yml` with the merge commit SHA as `commit_sha`.
2. Confirm slash commands respond (at minimum: `/ping` returns the new version string).
3. Confirm day-role-sync is working (trigger a manual sync or verify the scheduled run completed cleanly).

If smoke fails, do not tag. Fix the issue, merge a new commit, and restart from step 4.

## Tag-push procedure

After the release PR is merged, CI is green, and smoke is verified:

```bash
# From your local checkout, on main
git fetch origin --tags
git checkout main
git pull origin main

# Confirm HEAD is the release PR's squash commit
git log -1

# Annotated tag
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

### What to watch after pushing

1. **`release.yml` workflow** — navigate to the Actions tab and confirm the `Release` run triggered on the new tag. It should publish the GitHub Release and push the `:vX.Y.Z` GHCR image. (The workflow is added in #215; its exact steps are defined in `.github/workflows/release.yml`.)
2. **GitHub Release page** — verify it appears at `https://github.com/glitchwerks/mom-bot/releases/tag/vX.Y.Z` with the correct release notes.
3. **GHCR image** — confirm the versioned image is reachable: `docker manifest inspect ghcr.io/glitchwerks/mom-bot:vX.Y.Z`.

### Deploy is separate

The tag does not deploy to prod. When you are ready to promote the new version, dispatch `deploy.yml` with the tagged SHA (or leave `commit_sha` blank to use the tag's HEAD). The deploy workflow verifies the image exists in GHCR, runs `alembic upgrade head`, and updates the Azure Container App.

## Versioning policy

mom-bot follows semver from v1.0.0 onward.

**MAJOR** — breaking changes that require coordinated action from consumers:
- Sidecar HTTP contract changes that break siege-web consumers (renamed/removed endpoints or fields, changed auth scheme). These must go through the contract change protocol in `rsl-mom-apps` — see `glitchwerks/rsl-mom-apps`'s `CLAUDE.md` for the protocol.
- Discord slash command name or signature changes that disrupt users (e.g., renaming `/ping` to `/health`).

**MINOR** — additive, backward-compatible changes:
- New sidecar API endpoints (siege-web can ignore them).
- New Discord slash commands.
- New environment variables with documented defaults.

**PATCH** — backward-compatible fixes and internal improvements:
- Bug fixes that do not change the external contract.
- Observability improvements (new log lines, metrics).
- Dependency updates and security patches.
- Documentation and runbook updates.

When in doubt, prefer the higher bump.

## Hotfix flow

A hotfix is a patch release branched from a release tag rather than from `main`. Use this when `main` has unreleased breaking work that should not ship with the fix.

```bash
# Create a hotfix branch from the release tag
git checkout -b hotfix/vX.Y.Z+1 vX.Y.Z

# Cherry-pick the fix commit(s) from main
git cherry-pick <fix-sha>

# Apply the same pre-tag checklist:
# - Add a CHANGELOG entry under the new version
# - Bump version in pyproject.toml and src/mom_bot/__init__.py
# - Push the branch; open a PR targeting main (for the record and CI)
# - After CI is green and smoke passes, tag from this branch

git tag -a vX.Y.Z+1 -m "vX.Y.Z+1"
git push origin vX.Y.Z+1
```

After the hotfix tag is pushed, merge the hotfix branch into `main` so the fix is not lost in the next regular release. If the cherry-pick conflicts with ongoing work, resolve the conflict in the merge rather than skipping it.

## RC and pre-release tags

Use the `-rc.N` suffix for release candidates: `v1.0.0-rc.1`, `v1.0.0-rc.2`, etc. Increment `N` for each successive candidate if the previous RC uncovered a blocking issue.

`release.yml` should treat RC tags the same as full releases: push the `:vX.Y.Z-rc.N` GHCR image and create a GitHub Release marked as pre-release. This lets siege-web (or any consumer) test against the immutable RC image before the stable tag is cut.

RC tags follow the same pre-tag checklist. The only difference: the `[Unreleased]` section is not promoted to a stable version heading until the final tag. Use `## [X.Y.Z-rc.N] - YYYY-MM-DD` in CHANGELOG if you want to record the RC history, or simply accumulate under `[Unreleased]` and promote once at the stable tag.

## Why this document exists

siege-web's v1.3.0 release cut the tag before updating `CHANGELOG.md` or version sources; the tag had to be re-cut at the metadata-fix HEAD after the fact.
