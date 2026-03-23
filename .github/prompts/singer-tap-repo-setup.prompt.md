---
mode: agent
description: >
  Clone (if needed), configure Python virtual-env, install the tap, and prepare a
  working branch — ready for any subsequent Singer tap task (unit tests, schema audit,
  new stream, release prep, etc.).
inputs:
  - id: tapNameOrUrl
    description: "Tap name or full Git URL  e.g. tap-klaviyo  OR  https://github.com/singer-io/tap-klaviyo"
    type: promptString
  - id: tapDirectory
    description: "Full local path where the tap should live  e.g. C:\\Users\\you\\workspace\\taps\\tap-klaviyo"
    type: promptString
  - id: parentBranch
    description: "Parent branch to base the working branch on (blank = auto-detect from remote HEAD)"
    type: promptString
    default: ""
  - id: branchStrategy
    description: "new = create a new branch from parentBranch | existing = checkout an existing branch"
    type: promptString
    default: "new"
  - id: branchName
    description: "Name of the branch to create or checkout"
    type: promptString
---

# Singer Tap — Repo Setup

You are an expert in Singer tap development. Your job is to make the tap repository
**fully ready to work on** — cloned, installed into a virtual-env, and on the correct branch.

Follow each step in order. Fix any errors autonomously; do not ask the user.

**Inputs:**
- Tap name / URL  : `${input:tapNameOrUrl}`
- Local directory : `${input:tapDirectory}`
- Parent branch   : `${input:parentBranch}` *(blank = auto-detect)*
- Branch strategy : `${input:branchStrategy}`  (`new` | `existing`)
- Branch name     : `${input:branchName}`

---

## Step 1 — Resolve Names

Derive:
- `TAP_NAME`   — last path segment of `${input:tapNameOrUrl}` if it contains `http`, otherwise the raw input
  e.g. `https://github.com/singer-io/tap-klaviyo` → `tap-klaviyo`
- `TAP_MODULE` — `TAP_NAME` with hyphens replaced by underscores
  e.g. `tap-klaviyo` → `tap_klaviyo`
- `CLONE_URL`  — if `${input:tapNameOrUrl}` starts with `http`, use it directly; otherwise
  `https://github.com/singer-io/${input:tapNameOrUrl}`

---

## Step 2 — Clone If Missing

Check whether the directory already exists:

```bash
# PowerShell
Test-Path "${input:tapDirectory}"
```

**If `False`:** clone the repository:
```bash
git clone CLONE_URL "${input:tapDirectory}"
```
Confirm the clone succeeded — the directory must now contain a `TAP_MODULE` package folder.

---

## Step 3 — Detect Parent Branch

**If `${input:parentBranch}` is blank**, run:
```bash
cd "${input:tapDirectory}"
git remote show origin | Select-String "HEAD branch" | ForEach-Object { $_ -replace ".*HEAD branch: *", "" }
```
Use the output as `PARENT_BRANCH` for all subsequent steps.

Otherwise set `PARENT_BRANCH = ${input:parentBranch}`.

---

## Step 4 — Fetch & Set Up Working Branch

Always fetch first:
```bash
cd "${input:tapDirectory}"
git fetch origin
```

**If `${input:branchStrategy}` is `new`:**
```bash
cd "${input:tapDirectory}"
git checkout PARENT_BRANCH
git pull origin PARENT_BRANCH
git checkout -b "${input:branchName}"
```

**If `${input:branchStrategy}` is `existing`:**
```bash
cd "${input:tapDirectory}"
git checkout "${input:branchName}"
git pull origin PARENT_BRANCH
```

Confirm active branch:
```bash
cd "${input:tapDirectory}"
git branch --show-current
```

---

## Step 5 — Create Virtual Environment and Install the Tap

### 5a — Resolve paths

Derive the following paths from `${input:tapDirectory}`:
- `TAPS_DIR`     — the parent folder of the tap directory
  e.g. `C:\Users\you\workspace\taps\tap-klaviyo` → `C:\Users\you\workspace\taps`
- `VENVS_DIR`    — `TAPS_DIR\virtual_envs`
- `TAP_VENV_DIR` — `VENVS_DIR\TAP_NAME`
  e.g. `C:\Users\you\workspace\taps\virtual_envs\tap-klaviyo`

### 5b — Ensure `virtual_envs` directory exists

```bash
# Check for the shared virtual_envs directory
if (-not (Test-Path "VENVS_DIR")) {
    New-Item -ItemType Directory -Path "VENVS_DIR" | Out-Null
    Write-Host "Created VENVS_DIR"
} else {
    Write-Host "VENVS_DIR already exists"
}
```

### 5c — Create the tap's virtual environment (if not already present)

```bash
if (-not (Test-Path "TAP_VENV_DIR\Scripts\python.exe")) {
    python -m venv "TAP_VENV_DIR"
    Write-Host "Created venv at TAP_VENV_DIR"
} else {
    Write-Host "Venv already exists at TAP_VENV_DIR"
}
```

Verify the venv was created:
```bash
Test-Path "TAP_VENV_DIR\Scripts\python.exe"
```

### 5d — Install the tap into the venv

Use the venv's pip directly (do NOT rely on the system pip):
```bash
cd "${input:tapDirectory}"
# Try dev extras first; fall back to plain install
& "TAP_VENV_DIR\Scripts\pip.exe" install -e ".[dev]" 2>$null
if ($LASTEXITCODE -ne 0) {
    & "TAP_VENV_DIR\Scripts\pip.exe" install -e .
}
```

### 5e — Verify installation

```bash
& "TAP_VENV_DIR\Scripts\python.exe" -c "import TAP_MODULE; print('TAP_MODULE import OK')"
```

If the import fails, read `setup.py` / `pyproject.toml` to find the correct package name and retry.

> **Note:** All subsequent commands in this prompt that call `python` or `pytest` must use  
> `TAP_VENV_DIR\Scripts\python.exe` and `TAP_VENV_DIR\Scripts\pytest.exe` respectively,  
> not the system Python.

---

## Step 6 — Confirm `tests/unittests/` Structure

Check whether the standard test structure exists:
```bash
Test-Path "${input:tapDirectory}\tests\unittests\__init__.py"
```

If `False`, create the empty init file:
```bash
New-Item -Path "${input:tapDirectory}\tests\unittests\__init__.py" -ItemType File -Force
```

---

## Step 7 — Summary

Print:
```
✅ Repo setup complete

Tap        : TAP_NAME
Directory  : ${input:tapDirectory}
Branch     : ${input:branchName}
Module     : TAP_MODULE
Venv       : TAPS_DIR\virtual_envs\TAP_NAME

Status:
  ✅ Repository cloned / already present
  ✅ Branch ready (PARENT_BRANCH → ${input:branchName})
  ✅ virtual_envs\ directory present (VENVS_DIR)
  ✅ Tap venv created at VENVS_DIR\TAP_NAME
  ✅ Tap installed into venv
  ✅ tests/unittests/__init__.py present

Python executable for this tap:
  VENVS_DIR\TAP_NAME\Scripts\python.exe

Ready for next step:
  • Unit tests    → run singer-tap-unittests
  • Schema audit  → run singer-tap-schema-audit
  • New stream    → run singer-tap-new-stream
  • Release prep  → run singer-tap-release-prep
```

---

## Important Rules

- Never modify source files in this step — only install and branch operations are allowed.
- If `git pull` causes merge conflicts, abort (`git merge --abort`) and report the conflict to the user.
- Always verify each step before proceeding to the next.
