---
mode: agent
description: >
  Prepare a Singer tap for release: bumps the version in setup.py / pyproject.toml,
  updates CHANGELOG.md with all commits since the last tag, creates a git tag,
  and prints a release checklist. Does NOT push or publish — leaves that to the user.
inputs:
  - id: tapDirectory
    description: "Full local path to the tap  e.g. C:\\Users\\you\\workspace\\taps\\tap-klaviyo"
    type: promptString
  - id: releaseType
    description: "Version bump type: patch | minor | major"
    type: promptString
    default: "patch"
  - id: releaseSummary
    description: "One-line human summary of this release (used as the changelog heading)"
    type: promptString
    default: ""
  - id: createGitTag
    description: "yes = create the annotated git tag locally after bump | no = bump only"
    type: promptString
    default: "yes"
---

# Singer Tap — Release Prep

You are an expert in Singer tap development and versioning.
Your job is to **prepare the tap for a release**: bump the version, update the changelog,
and optionally tag the commit.

Follow every step in order. Never push to remote — leave that to the user.

**Inputs:**
- Tap directory  : `${input:tapDirectory}`
- Release type   : `${input:releaseType}` (`patch` | `minor` | `major`)
- Release summary: `${input:releaseSummary}`
- Create git tag : `${input:createGitTag}`

---

## Step 1 — Detect Current Version

Check `setup.py`:
```bash
Select-String -Path "${input:tapDirectory}\setup.py" -Pattern "version\s*="
```

If `pyproject.toml` exists instead:
```bash
Select-String -Path "${input:tapDirectory}\pyproject.toml" -Pattern "^version"
```

Extract `CURRENT_VERSION` (e.g. `1.1.3`).

---

## Step 2 — Calculate New Version

```python
current = "CURRENT_VERSION"   # replace with actual value
parts   = list(map(int, current.split(".")))
while len(parts) < 3:
    parts.append(0)

release_type = "${input:releaseType}"
if release_type == "major":
    new_version = f"{parts[0]+1}.0.0"
elif release_type == "minor":
    new_version = f"{parts[0]}.{parts[1]+1}.0"
else:  # patch (default)
    new_version = f"{parts[0]}.{parts[1]}.{parts[2]+1}"

print(f"Current : {current}")
print(f"New     : {new_version}")
```

Set `NEW_VERSION` to the computed string.

---

## Step 3 — Bump Version in Source

### 3a — In `setup.py`

Read `setup.py`. Find the line containing `version=`. Replace it:
```
version='CURRENT_VERSION'   →   version='NEW_VERSION'
```

Also look for `__version__` assignments in `TAP_MODULE/__init__.py` or `TAP_MODULE/version.py` and update them:
```
__version__ = 'CURRENT_VERSION'   →   __version__ = 'NEW_VERSION'
```

### 3b — In `pyproject.toml` (if present)

Find `version = "CURRENT_VERSION"` under `[project]` or `[tool.poetry]` and replace.

Confirm the change:
```bash
Select-String -Path "${input:tapDirectory}\setup.py" -Pattern "version\s*="
```

---

## Step 4 — Collect Commits Since Last Tag

```bash
cd "${input:tapDirectory}"
git fetch --tags
$last_tag = git describe --tags --abbrev=0 2>$null
if ($last_tag) {
    git log "$last_tag..HEAD" --oneline --no-merges
} else {
    git log --oneline --no-merges | Select-Object -First 30
}
```

Store the commit list as `COMMIT_LIST`.

Categorise commits by prefix (if any):
- `feat:` → Features
- `fix:` → Bug Fixes
- `chore:`, `ci:`, `build:` → Maintenance
- `docs:` → Documentation
- Everything else → Other Changes

---

## Step 5 — Update CHANGELOG.md

Read the existing `CHANGELOG.md` (create it if it doesn't exist). Insert a new entry **at the top**
(below any title/header) following this format:

```markdown
## vNEW_VERSION — YYYY-MM-DD

RELEASE_SUMMARY

### Features
- commit message 1
- commit message 2

### Bug Fixes
- commit message 3

### Maintenance
- commit message 4

### Other Changes
- commit message 5
```

Rules:
- If a category has no commits, omit that heading entirely.
- Use the current date for `YYYY-MM-DD`.
- If `${input:releaseSummary}` is blank, use `"Release vNEW_VERSION"`.
- Preserve all existing changelog content below the new entry.

---

## Step 6 — Stage and Commit the Bump

```bash
cd "${input:tapDirectory}"
git add setup.py CHANGELOG.md
# also stage pyproject.toml and version files if changed
git status
git commit -m "chore: bump version to vNEW_VERSION"
```

---

## Step 7 — Create Git Tag (if `${input:createGitTag}` = `yes`)

```bash
cd "${input:tapDirectory}"
git tag -a "vNEW_VERSION" -m "Release vNEW_VERSION — RELEASE_SUMMARY"
git tag | Sort-Object | Select-Object -Last 5
```

Confirm the tag was created:
```bash
git describe --tags --abbrev=0
```

---

## Step 8 — Pre-Release Checklist

Run a final verification:

```bash
# 1. Tests must all pass
cd "${input:tapDirectory}"
python -m pytest tests/unittests/ -q

# 2. Module imports cleanly at new version
python -c "import TAP_MODULE; print(getattr(TAP_MODULE, '__version__', 'no __version__ attr'))"

# 3. Package builds without errors (setup.py or pyproject.toml)
python setup.py --version 2>$null || python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
```

---

## Step 9 — Final Release Summary

```
╔══════════════════════════════════════════════════════════════════╗
║              Singer Tap Release Prep Complete                    ║
╚══════════════════════════════════════════════════════════════════╝

Tap        : TAP_NAME
Directory  : ${input:tapDirectory}
Old version: CURRENT_VERSION
New version: NEW_VERSION
Tag        : vNEW_VERSION  (created: ${input:createGitTag})

────────────────────────────────────────────────────────────────────
FILES CHANGED
────────────────────────────────────────────────────────────────────
  ✅ setup.py              — version bumped to NEW_VERSION
  ✅ TAP_MODULE/__init__.py — __version__ updated  (if present)
  ✅ CHANGELOG.md          — vNEW_VERSION entry added

────────────────────────────────────────────────────────────────────
COMMIT
────────────────────────────────────────────────────────────────────
  ✅ chore: bump version to vNEW_VERSION

────────────────────────────────────────────────────────────────────
RELEASE CHECKLIST — complete before pushing
────────────────────────────────────────────────────────────────────
  [ ] All unit tests pass (python -m pytest tests/unittests/ -v)
  [ ] CHANGELOG.md reviewed and checked for accuracy
  [ ] PR opened against master / main
  [ ] PR reviewed and approved
  [ ] Merge PR → triggers CI

────────────────────────────────────────────────────────────────────
PUSH COMMANDS (run manually after review)
────────────────────────────────────────────────────────────────────
  git push origin <branch-name>
  git push origin vNEW_VERSION          # push the tag
```

---

## Important Rules

- Never push to remote — all git operations are local only.
- Never force-push or amend existing tags.
- If `git log` returns no commits since the last tag, do NOT create a new tag — inform the user there is nothing to release.
- If tests fail in Step 8, abort the release and report the failure.
