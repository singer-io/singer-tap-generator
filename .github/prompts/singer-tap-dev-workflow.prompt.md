---
mode: agent
description: >
  Master Singer tap development workflow. Asks what you want to do, then orchestrates
  the correct specialist prompts in the right order: repo setup → upgrade packages →
  unit tests → schema audit → discovery/sync test → release prep.
  Use this as the single entry-point for all tap work.
inputs:
  - id: tapNameOrUrl
    description: "Tap name or full Git URL  e.g. tap-klaviyo  OR  https://github.com/singer-io/tap-klaviyo"
    type: promptString
  - id: tapDirectory
    description: "Full local path for the tap  e.g. C:\\Users\\you\\workspace\\taps\\tap-klaviyo"
    type: promptString
  - id: tasks
    description: >
      Comma-separated list of tasks to run after setup (setup always runs automatically).
      Options: upgrade-packages | unit-tests | integration-tests | schema-audit | discovery-sync | release | commit
      Example: upgrade-packages,unit-tests,integration-tests,commit
      Use 'all' to run the full pipeline (upgrade-packages → unit-tests → integration-tests → schema-audit → discovery-sync → release → commit).
    type: promptString
    default: "all"
  - id: skipPackages
    description: "Packages to leave untouched during upgrade  e.g. singer-python,backoff  (blank = upgrade all)"
    type: promptString
    default: ""
  - id: dryRunUpgrade
    description: "yes = show what upgrade-packages would change but do NOT write setup.py | no = apply"
    type: promptString
    default: "no"
  - id: branchName
    description: "Working branch name — the branch to create (if branchStrategy=new) or checkout (if branchStrategy=existing)"
    type: promptString
    default: "feature/tap-improvements"
  - id: branchStrategy
    description: "new = create a new branch from parentBranch | existing = checkout a branch that already exists"
    type: promptString
    default: "new"
  - id: parentBranch
    description: "Parent branch to base work on — only used when branchStrategy=new (blank = auto-detect)"
    type: promptString
    default: ""
  - id: hasTestAccount
    description: "yes = use real API credentials for discovery/sync test | no = mock mode"
    type: promptString
    default: "no"
  - id: configFilePath
    description: "Path to config.json with real credentials (only needed when hasTestAccount=yes)"
    type: promptString
    default: ""
  - id: releaseType
    description: "patch | minor | major (for release task)"
    type: promptString
    default: "patch"
  - id: releaseSummary
    description: "One-line summary for the release changelog entry"
    type: promptString
    default: ""
---

# Singer Tap — Master Development Workflow

> **STATELESS EXECUTION** — Each invocation of this workflow is fully independent.
> Do **not** reference, carry over, or reuse any outputs, file paths, branch names, test results,
> or task state from any previous conversation or workflow run.
> Treat every run as if it is the first time this workflow has ever been executed.

You are a senior Singer tap engineer orchestrating the complete tap development lifecycle.
Your job is to **run the correct specialist steps in the right order** based on `${input:tasks}`.

You have access to these specialist prompt workflows:
| ID | Prompt | Purpose |
|---|---|---|
| `setup` | `singer-tap-repo-setup` | Clone repo, create branch, install tap |
| `upgrade-packages` | `singer-tap-upgrade-packages` | Upgrade all pinned packages in setup.py to latest |
| `unit-tests` | `singer-tap-unittests` | Generate / update full unit test suite |
| `integration-tests` | `singer-tap-integration-tests` | Generate / update tap-tester integration test suite |
| `schema-audit` | `singer-tap-schema-audit` | Audit all schemas for Singer conventions |
| `discovery-sync` | `singer-tap-discovery-sync-test` | Run discovery + sync, validate Singer output |
| `release` | `singer-tap-release-prep` | Bump version, update CHANGELOG, create tag |
| `commit` | `singer-tap-commit-changes` | Stage and commit all workflow changes with a descriptive message |

---

## Step 0 — Confirm Inputs and Plan Tasks

### 0a — Load inputs (auto-detect file or fall back to interactive)

First, check whether a default input file exists:

```powershell
# Resolve the .github/prompts/ folder (same directory as this prompt file)
$inputFile = Join-Path $PSScriptRoot "workflow-inputs.json"
Test-Path $inputFile
```

If `Test-Path` returns `True`, load all values from that file:

```python
import json, pathlib

input_path = pathlib.Path("<resolved .github/prompts/workflow-inputs.json>")
with open(input_path) as f:
    cfg = json.load(f)

tapNameOrUrl    = cfg.get("tapNameOrUrl",    "")
tapDirectory    = cfg.get("tapDirectory",    "")
tasks           = cfg.get("tasks",           "all")
branchName      = cfg.get("branchName",      "feature/tap-improvements")
branchStrategy  = cfg.get("branchStrategy",  "new")
parentBranch    = cfg.get("parentBranch",    "")
skipPackages    = cfg.get("skipPackages",    "")
dryRunUpgrade   = cfg.get("dryRunUpgrade",   "no")
hasTestAccount  = cfg.get("hasTestAccount",  "no")
configFilePath  = cfg.get("configFilePath",  "")
releaseType     = cfg.get("releaseType",     "patch")
releaseSummary  = cfg.get("releaseSummary",  "")

print(f"✅ Loaded inputs from: {input_path}")
```

Required fields: `tapNameOrUrl`, `tapDirectory`, `tasks`, `branchName`. If any are blank after loading, stop:
```
❌ workflow-inputs.json is missing required field: <field_name>
Fix the file and re-run. Template: .github/prompts/workflow-inputs.sample.json
```

If `Test-Path` returns `False` (file does not exist), use the VS Code interactive inputs:
```python
tapNameOrUrl    = "${input:tapNameOrUrl}"
tapDirectory    = "${input:tapDirectory}"
tasks           = "${input:tasks}"
branchName      = "${input:branchName}"
branchStrategy  = "${input:branchStrategy}"
parentBranch    = "${input:parentBranch}"
skipPackages    = "${input:skipPackages}"
dryRunUpgrade   = "${input:dryRunUpgrade}"
hasTestAccount  = "${input:hasTestAccount}"
configFilePath  = "${input:configFilePath}"
releaseType     = "${input:releaseType}"
releaseSummary  = "${input:releaseSummary}"
```

### 0b — Display confirmation and wait for YES

Display the following with the resolved values:

```
╔══════════════════════════════════════════════════════════════════════╗
║           Singer Tap — Workflow Input Confirmation                   ║
╚══════════════════════════════════════════════════════════════════════╝
  Input source    : FILE (.github/prompts/workflow-inputs.json)
                    —OR— INTERACTIVE (no input file found)

  Tap name / URL  : <tapNameOrUrl>
  Tap directory   : <tapDirectory>
  Tasks           : <tasks>
  Branch name     : <branchName>
  Branch strategy : <branchStrategy>  (new = create from parentBranch | existing = checkout)
  Parent branch   : <parentBranch>  (only for branchStrategy=new; blank = auto-detect)

  [upgrade-packages options]
  Skip packages   : <skipPackages>  (blank = upgrade all)
  Dry run         : <dryRunUpgrade>

  [discovery-sync options]
  Has test account: <hasTestAccount>
  Config file     : <configFilePath>  (only for real credentials)

  [release options]
  Release type    : <releaseType>
  Release summary : <releaseSummary>

Are these values correct? Type YES to proceed, or tell me what to change.
```

**Wait for the user to reply with YES (or corrections) before proceeding.**
If the user provides corrections, update the values, redisplay the summary, and ask again.
Only continue once the user has confirmed with YES.

### 0c — Resolve task list

Once confirmed:

```python
tasks_raw = tasks.strip().lower()

# setup is ALWAYS the first task — mandatory, regardless of what the user specified
OPTIONAL_TASKS = ["upgrade-packages", "unit-tests", "integration-tests", "schema-audit", "discovery-sync", "release", "commit"]

if tasks_raw in ("all", ""):
    additional_tasks = OPTIONAL_TASKS
else:
    # strip 'setup' from user input if they included it — it will always run anyway
    user_tasks = [t.strip() for t in tasks_raw.split(",") if t.strip() != "setup"]
    additional_tasks = [t for t in user_tasks if t in OPTIONAL_TASKS]
    unknown = [t for t in user_tasks if t not in OPTIONAL_TASKS]
    if unknown:
        print(f"WARNING: Unknown tasks ignored: {unknown}")

tasks = ["setup"] + additional_tasks  # setup is always prepended

print("Tasks to execute (in order):")
for i, t in enumerate(tasks, 1):
    marker = " ← MANDATORY" if t == "setup" else ""
    print(f"  {i}. {t}{marker}")
```

Print the confirmed plan, then begin task execution.

---

## Step 1 — SETUP  *(MANDATORY — always runs first)*

**Goal:** Ensure the tap is cloned, the working branch exists, and the package is installed.


Execute all steps from `singer-tap-repo-setup` with these parameters:
- `tapNameOrUrl`  → `${input:tapNameOrUrl}`
- `tapDirectory`  → `${input:tapDirectory}`
- `parentBranch`  → `${input:parentBranch}`
- `branchStrategy`→ `${input:branchStrategy}`
- `branchName`    → `${input:branchName}`

**Success gate:** The tap module must be importable before proceeding.
```bash
python -c "import TAP_MODULE; print('import OK')"
```
If this fails, fix the installation before moving on.

---

## Step 2 — UPGRADE PACKAGES  *(only if `upgrade-packages` in tasks)*

**Goal:** All pinned `==` dependencies in `setup.py` are on their latest PyPI release.

Execute all steps from `singer-tap-upgrade-packages` with these parameters:
- `tapDirectory`    → `${input:tapDirectory}`
- `skipPackages`    → `${input:skipPackages}`
- `dryRun`          → `${input:dryRunUpgrade}`
- `runTests`        → `yes`
- `commitChanges`   → `no`   *(master workflow delegates all commits to the `commit` task — see Step 7)*

**Success gate:**
- `setup.py` updated (or reported as already up to date in dry-run mode)
- Tap import succeeds after reinstall
- All existing unit tests still pass (exit code 0)

Record:
- Total packages checked
- Packages upgraded (old → new)
- Packages already at latest
- Packages reverted due to test failures

---

## Step 3 — UNIT TESTS  *(only if `unit-tests` in tasks)*

**Goal:** Maximum unit test coverage for the tap with all tests passing.

Execute all steps from `singer-tap-unittests` with these parameters:
- `tapNameOrUrl`  → `${input:tapNameOrUrl}`
- `tapDirectory`  → `${input:tapDirectory}`
- `parentBranch`  → `${input:parentBranch}`
- `branchStrategy`→ `existing`
- `branchName`    → `${input:branchName}`

**Success gate:** `python -m pytest tests/unittests/ -q` must exit with code 0.

Record:
- Total tests passing
- Coverage % for `utils.py`, `__init__.py` (or main module files)

---

## Step 4 — INTEGRATION TESTS  *(only if `integration-tests` in tasks)*

**Goal:** Full tap-tester integration test suite scaffolded and passing.

Execute all steps from `singer-tap-integration-tests` with these parameters:
- `tapDirectory`    → `${input:tapDirectory}`
- `branchStrategy`  → `existing`
- `branchName`      → `${input:branchName}`
- `testMode`        → `live`

**Success gate:** All 7 test files created/updated under `tests/` with no syntax errors.

Record:
- Streams discovered
- Files created / updated
- Credential env vars required

---

## Step 5 — SCHEMA AUDIT  *(only if `schema-audit` in tasks)*

**Goal:** All schema files pass Singer convention checks.

Execute all steps from `singer-tap-schema-audit` with these parameters:
- `tapDirectory`  → `${input:tapDirectory}`
- `streamsToAudit`→ *(blank = all)*
- `fixIssues`     → `yes`

**Success gate:** Zero `NOT_NULLABLE` issues remain after auto-fix.

Record:
- Total issues found
- Issues auto-fixed
- Issues requiring manual review

---

## Step 6 — DISCOVERY & SYNC TEST  *(only if `discovery-sync` in tasks)*

**Goal:** Tap's discovery and sync output are valid Singer messages.

Execute all steps from `singer-tap-discovery-sync-test` with these parameters:
- `tapNameOrUrl`    → `${input:tapNameOrUrl}`
- `tapDirectory`    → `${input:tapDirectory}`
- `hasTestAccount`  → `${input:hasTestAccount}`
- `configFilePath`  → `${input:configFilePath}`
- `streamsToTest`   → *(blank = all)*
- `startDate`       → `2024-01-01T00:00:00Z`

**Success gate (mock mode):** All streams in `schemas/` produce valid SCHEMA + RECORD + STATE messages.
**Success gate (real mode):** Exit code 0 from tap CLI, no protocol violations in Singer output.

Record:
- Streams discovered
- Total records returned (or "mock" if mock mode)
- Any protocol violations

---

## Step 7 — RELEASE PREP  *(only if `release` in tasks)*

**Goal:** Version bumped, CHANGELOG updated, git tag created — ready for PR.

**Pre-check:** All previous tasks must have passed their success gates. If any failed, skip release and report:
```
❌ Release skipped — previous steps had failures. Fix them first.
```

Execute all steps from `singer-tap-release-prep` with:
- `tapDirectory`   → `${input:tapDirectory}`
- `releaseType`    → `${input:releaseType}`
- `releaseSummary` → `${input:releaseSummary}`
- `createGitTag`   → `yes`

**Success gate:** `python -m pytest tests/unittests/ -q` exits with code 0.

---

## Step 8 — COMMIT ALL CHANGES  *(only if `commit` in tasks)*

**Goal:** Stage and commit every file changed by any task that ran, with a single
descriptive git commit message that summarises each category of change.

Execute all steps from `singer-tap-commit-changes` with these parameters:
- `tapDirectory`  → `${input:tapDirectory}`
- `commitScope`   → *(blank — auto-detect from directory name)*

**Success gate:** `git -C TAP_DIR status --porcelain` returns empty after the commit
(all changes are committed), or the step reports “Nothing to commit”.

Record:
- Files staged per category (packages, unit-tests, schemas, changelog, version, other)
- Commit subject line
- Commit hash

---

```
║           Singer Tap — Master Workflow Report                        ║
╚══════════════════════════════════════════════════════════════════════╝

Tap        : TAP_NAME
Directory  : ${input:tapDirectory}
Branch     : ${input:branchName}
Tasks run  : ${input:tasks}
Run date   : <current datetime>

────────────────────────────────────────────────────────────────────────
TASK RESULTS
────────────────────────────────────────────────────────────────────────

  1. SETUP             : ✅ PASSED / ⏭️ SKIPPED / ❌ FAILED
     Branch            : ${input:branchName}
     Tap installed     : TAP_NAME vX.Y.Z

  2. UPGRADE PACKAGES  : ✅ PASSED / ⏭️ SKIPPED / ⚠️  PARTIAL / ❌ FAILED
     Packages checked  : <N>
     Upgraded          : <N>  (e.g. requests 2.32.4 → 2.32.5)
     Already latest    : <N>
     Reverted          : <N>  (broke tests — kept old pin)

  3. UNIT TESTS        : ✅ PASSED / ⏭️ SKIPPED / ❌ FAILED
     Tests passing     : <N>
     Coverage          : utils.py=XX%  __init__.py=XX%

  4. INTEGRATION TESTS : ✅ PASSED / ⏭️ SKIPPED / ❌ FAILED
     Streams discovered: <N>
     Files created     : <N>
     Env vars required : TAP_<NAME>_<KEY>

  5. SCHEMA AUDIT      : ✅ PASSED / ⏭️ SKIPPED / ⚠️  WARNINGS / ❌ FAILED
     Issues found      : <N>
     Issues fixed      : <N>
     Remaining         : <N>

  6. DISCOVERY & SYNC  : ✅ PASSED / ⏭️ SKIPPED / ⚠️  WARNINGS / ❌ FAILED
     Mode              : REAL CREDENTIALS / MOCK
     Streams found     : <N>
     Records returned  : <N> (mock) / <N> (real)
     Protocol issues   : <N>

  7. RELEASE PREP      : ✅ PASSED / ⏭️ SKIPPED / ❌ FAILED
     Version           : OLD → NEW
     Tag created       : vNEW_VERSION

  8. COMMIT CHANGES    : ✅ COMMITTED / ⏭️ SKIPPED (not in tasks) / ⚠️  NOTHING TO COMMIT / ❌ FAILED
     Files staged      : setup.py, tests/unittests/test_X.py ...
     Commit hash       : <hash> / N/A

────────────────────────────────────────────────────────────────────────
OVERALL STATUS
────────────────────────────────────────────────────────────────────────
  ✅ ALL TASKS PASSED
  — or —
  ❌ <N> task(s) failed — see details above

────────────────────────────────────────────────────────────────────────
NEXT ACTIONS
────────────────────────────────────────────────────────────────────────
  [ ] Review test output and fix any remaining failures
  [ ] Review CHANGELOG.md entry for accuracy
  [ ] Open PR:  git push origin ${input:branchName}
  [ ] After PR merge: git push origin vNEW_VERSION   (to publish tag)
```

---

## Workflow Quick Reference

> **Setup always runs first automatically** — no need to include it in `tasks`.

Run just one additional task (setup will still run first):
```
tasks=upgrade-packages       — Setup + upgrade setup.py deps to latest
tasks=unit-tests             — Setup + unit tests
tasks=integration-tests      — Setup + tap-tester integration test suite
tasks=schema-audit           — Setup + schema quality check only
tasks=discovery-sync         — Setup + discovery + sync validation only
tasks=release                — Setup + release prep only
tasks=commit                 — Setup + stage and commit all current changes
```

Run the full lifecycle (with commit at the end):
```
tasks=all   OR   tasks=upgrade-packages,unit-tests,integration-tests,schema-audit,discovery-sync,commit
```

Run specific tasks and commit when done:
```
tasks=upgrade-packages,unit-tests,integration-tests,commit
```

Upgrade packages without modifying any files (preview only):
```
tasks=upgrade-packages  dryRunUpgrade=yes
```

Upgrade packages but leave singer-python pinned:
```
tasks=upgrade-packages  skipPackages=singer-python
```

Commit only (after manual changes, no other tasks):
```
tasks=commit
```

---

## Important Rules

- **`setup` is mandatory and always runs first** — it is automatically prepended to every task list. You cannot skip it.
- **Release is always last** — it must gate on all prior tasks passing.
- If any step fails its success gate, stop and do not run subsequent tasks.
- All fixes are done autonomously — never ask the user to fix things manually.
