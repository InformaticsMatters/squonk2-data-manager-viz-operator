---
name: release
description: >-
  Cut a release of the squonk2-data-manager-viz-operator. Use when the user
  asks to "release", "cut a release", "make a release", "publish a release", or
  "tag a release" of this repository. Handles semver numbering (alpha/beta/rc
  pre-releases and full releases), enforces that the major matches the pinned
  kubernetes package, blocks releases when CI has failed, generates release
  notes, and creates the GitHub release.
---

# Release

Cut a release of this operator. A release is a Git **tag** plus a **GitHub
release**; pushing the tag triggers the `build-tag` workflow, which builds and
pushes the operator container image. Releases are therefore outward-facing and
hard to reverse — follow the steps in order and **confirm with the user before
creating the release**.

## Rules (must hold)

- Releases are **always cut from the latest `main`**.
- The release **major must equal** the major of the operator's pinned
  `kubernetes` package (`operator/requirements.txt`). The helper enforces this.
- Numbering is semver with pre-release channels **alpha** (`-alpha.N`), **beta**
  (`-beta.N`) and **release candidate** (`-rc.N`); the first pre-release in a
  series uses `N = 1`. A full release has no suffix.
- alpha/beta/rc releases are marked as a **Pre-release** on GitHub.
- **Never release if CI has failed** for the commit being released.

## Steps

### 1. Get onto the latest `main`

Run this from the main checkout (not a feature worktree):

```bash
git checkout main
git fetch origin
git pull --ff-only origin main
git rev-parse HEAD            # the commit being released
```

If the working tree is dirty or `main` cannot fast-forward, stop and tell the
user.

### 2. Refuse to release on a failed (or unfinished) CI run

Check the `build` workflow for the exact commit being released. Do **not**
release unless its latest run is `completed` with conclusion `success`:

```bash
sha="$(git rev-parse HEAD)"
gh run list --branch main --workflow build --commit "$sha" \
  --limit 1 --json status,conclusion,headSha,url
```

- conclusion `success` → continue.
- conclusion `failure`/`cancelled`/`timed_out` → **abort** and report the run URL.
- still `in_progress`/`queued`, or no run found yet → tell the user CI has not
  finished and stop (offer to wait and retry rather than releasing blind).

### 3. Ask the user for the title and channel

Use `AskUserQuestion` to collect:

1. **Title** — a short human title for the release (free text).
2. **Channel** — one of:
   - `alpha` — early pre-release
   - `beta` — pre-release
   - `rc` — release candidate
   - `final` — full (non pre-release) release

### 4. Compute the next tag

The helper reads the kubernetes major from `operator/requirements.txt`, defaults
the base version to `{major}.0.0`, inspects existing tags, and prints the next
tag (incrementing `N` per channel, first is `1`):

```bash
python .claude/skills/release/next_release.py next \
  --channel <alpha|beta|rc|final> \
  --requirements operator/requirements.txt
```

- If the user needs a different minor/patch (the major is fixed), pass an
  explicit `--base MAJOR.MINOR.PATCH` (its major must still match the package).
- The helper raises (non-zero exit) on a major mismatch, a malformed base, or a
  `final` tag that already exists — surface the message and stop; do not invent
  a tag.

Show the computed tag to the user and **get explicit confirmation** before the
next step.

### 5. Create the GitHub release

Create the release on `main`'s head. `--generate-notes` builds the notes from
all commits/PRs since the previous release. Add `--prerelease` for
alpha/beta/rc (i.e. any channel other than `final`):

```bash
# Pre-release (alpha/beta/rc):
gh release create "<tag>" --target main --title "<title>" \
  --generate-notes --prerelease

# Full release (final):
gh release create "<tag>" --target main --title "<title>" \
  --generate-notes
```

`gh release create` creates the tag for you; do not create the tag separately.

### 6. Confirm

Report the release URL (`gh release view <tag> --web` / the create output) and
remind the user that the `build-tag` workflow is now building and pushing the
`informaticsmatters/data-manager-viz-operator:<tag>` image.

## Helper

`next_release.py` holds the pure, unit-tested numbering logic
(`tests/test_release.py`). It also exposes `major` to print the pinned
kubernetes major:

```bash
python .claude/skills/release/next_release.py major operator/requirements.txt
```
