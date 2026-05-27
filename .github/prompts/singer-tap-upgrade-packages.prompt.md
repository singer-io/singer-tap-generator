---
mode: agent
description: >
  Upgrade all pinned packages in a Singer tap's setup.py to their latest
  compatible versions. Reads install_requires (and extras_require), resolves
  the latest PyPI version for every package, updates setup.py, re-installs,
  and runs the existing unit tests to confirm nothing broke.
inputs:
  - id: tapDirectory
    description: "Full local path to the tap  e.g. C:\\Users\\you\\workspace\\taps\\tap-monday"
    type: promptString
  - id: skipPackages
    description: "Comma-separated list of package names to leave untouched  e.g. singer-python,backoff"
    type: promptString
    default: ""
  - id: dryRun
    description: "yes = report what would change but do NOT modify setup.py | no = apply changes"
    type: promptString
    default: "no"
  - id: runTests
    description: "yes = run unit tests after upgrading | no = skip"
    type: promptString
    default: "yes"
---

# Singer Tap — Package Upgrade Workflow

You are a senior Python engineer responsible for keeping Singer tap dependencies
up to date. Your job is to **find the latest PyPI version of every pinned package
in `setup.py`**, update the pins, reinstall, and verify the test suite still passes.

---

## Step 1 — Resolve Paths and Read `setup.py`

```python
TAP_DIR = "${input:tapDirectory}"
SETUP_PY = TAP_DIR + "/setup.py"
```

Read `setup.py` and extract every package from:
- `install_requires`
- all `extras_require` groups (e.g. `dev`, `test`)

For each entry normalise to `(package_name, current_version_pin)`:

| Raw string | Package name | Current pin |
|---|---|---|
| `"singer-python==6.1.1"` | `singer-python` | `6.1.1` |
| `"requests>=2.28,<3"` | `requests` | `>=2.28,<3` |
| `"coverage"` (no pin) | `coverage` | *(unpinned)* |

Skip packages listed in `${input:skipPackages}` (split on comma, strip whitespace).

---

## Step 2 — Resolve Latest Version from PyPI

For every package that is **pinned with `==`**, query PyPI:

Write and run the following Python script to get latest versions:

```python
# Write to TAP_DIR/_check_latest.py then execute with venv python
import json
import urllib.request
import sys

packages = [PACKAGE_LIST]   # list of package names to check

results = {}
for pkg in packages:
    try:
        url = f"https://pypi.org/pypi/{pkg}/json"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        latest = data["info"]["version"]
        results[pkg] = {"latest": latest, "status": "ok"}
    except Exception as e:
        results[pkg] = {"latest": None, "status": f"error: {e}"}

for pkg, info in results.items():
    print(f"{pkg}: {info['latest']} ({info['status']})")
```

Run it with the tap's virtual env Python:
```
VENV_PY = "C:\...\virtual_envs\TAP_NAME\Scripts\python.exe"
```

If the virtual env is not found, fall back to the system Python for the version
check (do NOT reinstall using system Python — only use it for the PyPI query).

---

## Step 3 — Build the Upgrade Plan

Compare current pin vs latest version for each package.

Print the plan in a table:

```
Package              Current     Latest      Action
─────────────────────────────────────────────────────
singer-python        6.1.1       6.1.1       ✅ already latest
requests             2.32.4      2.32.5      ⬆️  upgrade
backoff              2.2.1       2.2.1       ✅ already latest
parameterized        0.9.0       0.9.0       ✅ already latest
```

Mark packages as one of:
- `✅ already latest` — no change needed
- `⬆️  upgrade` — will update pin
- `⚠️  check` — latest version is older than current (yanked/pre-release?) — leave unchanged
- `⏭️  skipped` — in skipPackages list
- `❓ unpinned` — no `==` pin; report but do not add a pin automatically

If `${input:dryRun}` is `yes` → **print the plan and stop here**.

---

## Step 4 — Update `setup.py`

For every package marked `⬆️  upgrade`:

1. Read the current `setup.py` content.
2. Use exact string replacement to update the version pin:
   - `"requests==2.32.4"` → `"requests==2.32.5"`
   - Preserve quoting style (single vs double quotes) exactly.
3. Write the updated `setup.py` back.

After all replacements, re-read `setup.py` and confirm each updated line is correct.

**Never:**
- Change indentation or whitespace outside the replaced string
- Remove comments
- Convert `>=` / `<=` / `~=` pins to `==` pins
- Add pins to packages that were previously unpinned

---

## Step 5 — Reinstall the Tap

Using the tap's virtual env pip:

```powershell
$vpy = "...\virtual_envs\TAP_NAME\Scripts\python.exe"
& $vpy -m pip install -e . --quiet
```

Verify the tap is still importable:

```powershell
& $vpy -c "import TAP_MODULE; print('import OK')"
```

If import fails, **revert setup.py** to its original content and report the failure.

---

## Step 6 — Run Unit Tests  *(only if `runTests` = `yes`)*

```powershell
& $vpy -m pytest tests/unittests/ -v --tb=short
```

**Success gate:** All tests pass (exit code 0).

If any tests fail:
1. Show the failing test names and short tracebacks.
2. Determine whether the failure is caused by a package API change (e.g. a
   method renamed in a newer version of `requests`).
3. If caused by the upgrade, revert only that package's pin in `setup.py`,
   reinstall, and re-run tests.
4. Report which packages were reverted and why.

---

## Final Report

Print a summary:

```
╔══════════════════════════════════════════════════════════════════════╗
║         Singer Tap — Package Upgrade Report                          ║
╚══════════════════════════════════════════════════════════════════════╝

Tap        : TAP_NAME
setup.py   : TAP_DIR/setup.py
Dry run    : yes / no

────────────────────────────────────────────────────────┐
 PACKAGE UPGRADE SUMMARY
────────────────────────────────────────────────────────┘

 Package              Old version   New version   Status
 ──────────────────────────────────────────────────────
 singer-python        6.1.1         6.1.1         ✅ already latest
 requests             2.32.4        2.32.5         ⬆️  upgraded
 backoff              2.2.1         2.2.1         ✅ already latest
 parameterized        0.9.0         0.9.0         ✅ already latest

 Packages upgraded  : 1
 Already up to date : 3
 Skipped            : 0
 Errors             : 0

────────────────────────────────────────────────────────┐
 TEST RESULTS  (after upgrade)
────────────────────────────────────────────────────────┘
 Tests run      : <N>
 Passed         : <N>
 Failed         : 0
 Result         : ✅ ALL TESTS PASSED

────────────────────────────────────────────────────────
 OVERALL: ✅ UPGRADE COMPLETE — setup.py updated and tests passing
          Changes left unstaged — use the commit/release prompts to proceed.
────────────────────────────────────────────────────────
```

---

## Important Rules

- Only upgrade packages that use **exact `==` pins** — never touch `>=`, `~=`, `<=`, or unpinned entries.
- Always run a post-upgrade import check before running tests.
- If a package upgrade breaks the test suite, revert that specific package and document the reason.
- Never upgrade packages in place; always write the full updated `setup.py` back atomically.
- When `dryRun=yes`, make **zero file system changes** — only print what would happen.
