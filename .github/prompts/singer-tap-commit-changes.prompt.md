---
mode: agent
description: >
  Stage and commit all changes made by any Singer tap workflow task into a single
  descriptive git commit. Detects what files changed, groups them by category
  (packages, unit-tests, schemas, changelog, version, other), builds a dynamic
  commit message that describes each category, then stages and commits.
  Used as the final step in the master workflow when the user wants to commit.
inputs:
  - id: tapDirectory
    description: "Full local path to the tap  e.g. C:\\Users\\you\\workspace\\taps\\tap-monday"
    type: promptString
  - id: commitScope
    description: "Short scope for the commit subject  e.g. tap-monday  (blank = auto-detect from directory name)"
    type: promptString
    default: ""
---

# Singer Tap — Commit Changes Workflow

You are a senior Singer tap engineer preparing a clean, descriptive git commit
for all changes made during a tap development workflow session.

---

## Step 1 — Resolve Tap Name and Check Git Status

```python
TAP_DIR  = "${input:tapDirectory}"
# Auto-detect scope from directory name if not provided
scope = "${input:commitScope}".strip() or os.path.basename(TAP_DIR)
```

Run:
```powershell
$TAP_DIR = "${input:tapDirectory}"
git -C $TAP_DIR status --porcelain
```

If the output is **empty** (working tree clean), print:
```
⏭️  Nothing to commit — working tree is clean.
```
and stop here.

---

## Step 2 — Categorise Changed Files

Parse every line of `git status --porcelain` output. Each line is:
```
XY filepath
```
where `XY` is the status code (e.g. `M `, ` M`, `??`, `A `) and `filepath` is
the relative path from the repo root.

Group files into these categories (a file can appear in only one — use the first matching rule):

| Priority | Category | Match rule | Example |
|---|---|---|---|
| 1 | `version` | `*/__init__.py` **and** file contains `__version__` | `tap_monday/__init__.py` |
| 2 | `packages` | filename is exactly `setup.py` | `setup.py` |
| 3 | `unit-tests` | path starts with `tests/` | `tests/unittests/test_sync.py` |
| 4 | `schemas` | path matches `*/schemas/*.json` | `tap_monday/schemas/boards.json` |
| 5 | `changelog` | filename matches `CHANGELOG*` or `CHANGES*` | `CHANGELOG.md` |
| 6 | `other` | anything not matched above | `requirements.txt` |

Build a summary dict:
```python
categories = {
    "version":    [],   # files + new version string if detectable
    "packages":   [],   # files + list of upgraded deps if detectable
    "unit-tests": [],   # files + test count + coverage % if detectable
    "schemas":    [],   # files + count of fixed issues if detectable
    "changelog":  [],   # files
    "other":      [],   # files
}
```

Print the categorisation table:
```
Category      Files
─────────────────────────────────────────────────
packages    : setup.py
unit-tests  : tests/unittests/test_discovery_sync.py
schemas     : (none)
changelog   : (none)
version     : (none)
other       : (none)
```

---

## Step 3 — Enrich Category Summaries

For each non-empty category, gather extra detail to make the commit message informative:

**`packages`** — Read `setup.py` and compare with the last committed version of `setup.py`
(`git show HEAD:setup.py`) to list exactly which deps changed:
```
requests 2.32.4 → 2.32.5
```

**`unit-tests`** — For each new/modified test file, count:
- Number of test functions (`def test_` occurrences)
- If a recent pytest run output is available in the session context, extract total tests passing and coverage %

**`schemas`** — Count how many schema files changed and infer the type of fix from
the diff (e.g. added `"null"` to type arrays = NOT_NULLABLE fix).

**`version`** — Read `__version__` from the current file and from
`git show HEAD:PATH` to get old → new version string.

**`changelog`** — Extract the first heading/version line from `CHANGELOG.md`
to include in the message.

If any enrichment fails (e.g. no prior commit), use the bare filenames only.

---

## Step 4 — Build the Commit Message

Construct the subject line:
```
chore(SCOPE): apply workflow improvements
```

If **only one category** has changes, use a more specific subject:
- packages only  → `chore(SCOPE): upgrade pinned dependencies to latest`
- unit-tests only → `test(SCOPE): add/update unit test suite`
- schemas only    → `fix(SCOPE): resolve NOT_NULLABLE schema issues`
- version only    → `chore(SCOPE): bump version X.Y.Z → A.B.C`
- changelog only  → `docs(SCOPE): update CHANGELOG for vX.Y.Z`

Build the body — one bullet per non-empty category, with enrichment detail:

```
chore(tap-monday): apply workflow improvements

Changes included in this commit:
- packages:    upgrade pinned deps — requests 2.32.4 → 2.32.5
- unit-tests:  add/update 2 test file(s) — 154 tests passing, 98% coverage
- schemas:     fix 3 NOT_NULLABLE issue(s) in 2 schema file(s)
- changelog:   update CHANGELOG.md for v0.0.2
- version:     bump version 0.0.1 → 0.0.2
- other:       requirements.txt
```

Only include bullet lines for categories that have files. Never include empty bullets.

---

## Step 5 — Stage and Commit

```powershell
# Stage everything under the tap directory
git -C $TAP_DIR add -A

# Commit with the constructed message
git -C $TAP_DIR commit -m "<subject line>" -m "<body from Step 4>"
```

Print the full commit message used, then print the resulting commit hash:
```
✅ Committed: abc1234
```

**If the commit fails:**
- Show the full error output
- Do NOT retry automatically
- Tell the user exactly what to fix (e.g. "no changes staged", "pre-commit hook failed at ...")

---

## Final Report

```
╔══════════════════════════════════════════════════════════════╗
║         Singer Tap — Commit Report                           ║
╚══════════════════════════════════════════════════════════════╝

Tap        : SCOPE
Directory  : ${input:tapDirectory}

────────────────────────────────────────────────┐
 FILES COMMITTED
────────────────────────────────────────────────┘
 packages    : setup.py
 unit-tests  : tests/unittests/test_discovery_sync.py
 schemas     : (none)
 changelog   : (none)
 version     : (none)
 other       : (none)

────────────────────────────────────────────────┐
 COMMIT
────────────────────────────────────────────────┘
 Subject     : chore(tap-monday): apply workflow improvements
 Hash        : abc1234
 Branch      : gl-master

────────────────────────────────────────────────
 RESULT: ✅ COMMITTED  /  ⏭️ NOTHING TO COMMIT  /  ❌ FAILED
────────────────────────────────────────────────
```

---

## Important Rules

- **Never commit release tags** — that is handled by `singer-tap-release-prep`.
  If you detect a `git tag` was already created for the current version, skip tagging.
- **Never force-push** or amend existing commits.
- **Never commit credentials** — if `config.json` or any file containing `api_token`,
  `password`, `secret`, or `access_token` appears in the changed files list, exclude
  it from staging and warn the user immediately.
- If `dryRun` mode is needed, just print the commit message and file list without
  actually running `git add` or `git commit`.
