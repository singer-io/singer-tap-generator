---
mode: agent
description: Create or update unit tests for a Singer tap with maximum coverage. Handles cloning, branching, test generation, and terminal validation automatically.
inputs:
  - id: tapNameOrUrl
    description: "Tap name or full Git URL  e.g. tap-workday-raas  OR  https://github.com/singer-io/tap-workday-raas"
    type: promptString
  - id: tapDirectory
    description: "Full local path where the tap exists or should be cloned  e.g. C:\\Users\\user_name\\Documents\\workspace\\taps\\tap-workday-raas"
    type: promptString
  - id: parentBranch
    description: "Parent branch to base new branch on  e.g. master or main  (leave blank to auto-detect)"
    type: promptString
    default: ""
  - id: branchStrategy
    description: "Enter 'new' to create a new branch from parent, or 'existing' to use an existing branch"
    type: promptString
    default: "new"
  - id: branchName
    description: "Branch name — if strategy=new provide new name e.g. add-unittests; if strategy=existing provide the branch name to checkout"
    type: promptString
---

# Singer Tap Unit Test Generator

You are an expert in Singer tap development and Python unit testing. Your job is to create or update unit tests for a Singer tap with maximum coverage of its main functionality. Follow every step below in order without skipping.

**Inputs from user:**
- Tap name or URL: `${input:tapNameOrUrl}`
- Tap local directory: `${input:tapDirectory}`
- Parent branch: `${input:parentBranch}` *(blank = auto-detect)*
- Branch strategy: `${input:branchStrategy}` *(`new` = create new branch, `existing` = use existing)*
- Branch name: `${input:branchName}`

---

## Step 1 � Prepare the Repository

### 1a � Check if the tap directory exists

Run in terminal and check the output:
```bash
test -d "${input:tapDirectory}" && echo "EXISTS" || echo "NOT_FOUND"
```

### 1b � If directory does NOT exist, clone the tap

If `${input:tapNameOrUrl}` is a full URL use it directly. If it is just a tap name (no `http`), derive:
`https://github.com/singer-io/${input:tapNameOrUrl}`

```bash
git clone <resolved_url> "${input:tapDirectory}"
```

### 1c � Detect parent branch if blank

If `${input:parentBranch}` is blank, run:
```bash
cd "${input:tapDirectory}" && git remote show origin | grep "HEAD branch" | awk '{print $NF}'
```
Use the output as the parent branch for all subsequent steps.

### 1d � Fetch latest and set up branch

Always fetch first:
```bash
cd "${input:tapDirectory}" && git fetch origin
```

**If `${input:branchStrategy}` is `new`:**
```bash
cd "${input:tapDirectory}"
git checkout <parent_branch>
git pull origin <parent_branch>
git checkout -b "${input:branchName}"
```

**If `${input:branchStrategy}` is `existing`:**
```bash
cd "${input:tapDirectory}"
git checkout "${input:branchName}"
git pull origin <parent_branch>
```

---

## Step 2 � Analyse the Tap

Read the tap source code thoroughly before writing any tests. Focus on:

1. **`tap_<name>/__init__.py`** � `main()`, `discover()`, `sync()` entry points
2. **`client.py`** � HTTP client, authentication, request/retry logic
3. **`discover.py`** � catalog / stream discovery, schema generation
4. **`sync.py`** � data extraction, bookmarking, incremental sync
5. **`streams.py`** or `streams/` directory � all stream definitions
6. **`schemas/`** � JSON schema files
7. **`setup.py`** � package name, dependencies, entry points
8. **`tests/` or `tests/unittests/`** � existing tests: understand what is already covered and what is missing

List all: public functions/methods, error/exception paths, config fields used, Singer output calls.

---

## Step 3 � Create / Update Unit Tests

### Directory structure (match existing tap conventions):
```
tests/
  unittests/
    __init__.py          # empty, create if missing
    test_client.py       # HTTP client, auth, error handling
    test_discovery.py    # schema generation, catalog building
    test_sync.py         # sync logic, bookmarks, record output
    test_<stream>.py     # per-stream tests if complexity warrants it
```

If the tap already has test files, **ADD** to them � never delete existing tests.

### Standard patterns:

**Imports:**
```python
import json
import unittest
from unittest.mock import patch, MagicMock, call
import requests
```

**Config helper:**
```python
def _make_config(**kwargs):
    base = {"username": "user", "password": "pass"}
    base.update(kwargs)
    return base
```

**HTTP error helper:**
```python
def _make_http_error(status_code, body=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    err = requests.exceptions.HTTPError(response=resp)
    err.response = resp
    return err
```

**Test class naming:**
- `TestDiscover<Feature>` � discovery / schema tests
- `TestSync<Feature>` � sync / record output tests
- `TestClient<Feature>` � HTTP / auth / retry tests
- `Test<Stream>Stream` � per-stream tests

### Must-cover areas (create tests for ALL):

| Area | What to test |
|---|---|
| Discovery | Happy path returns correct stream list; schema properties correct; missing/extra fields handled |
| Config validation | Required fields missing raise clear errors; optional fields have correct defaults |
| HTTP errors | 4xx raises with message; 5xx raises or retries; connection errors handled |
| Authentication | Correct auth headers/params sent; auth failure raises meaningful error |
| Sync � full table | All records yielded; `write_schema` called once; `write_record` called per record |
| Sync � incremental | Bookmark read correctly; only records after bookmark synced; bookmark written after sync |
| Singer output | `write_schema`, `write_record`, `write_state` called with correct args |
| Edge cases | Empty response; zero records; malformed response; unexpected fields |
| Error isolation | One failing stream does not crash others (if applicable) |

### Mocking rules:
- Always mock HTTP calls � never make real network calls
- Patch at the import location: `@patch("tap_name.client.requests.get")`
- Use `@patch` decorators, not context manager form, unless nesting is needed

---

## Step 4 � Install Dependencies and Run Tests

```bash
cd "${input:tapDirectory}"
pip install -e ".[dev]" 2>/dev/null || pip install -e .
```

Run tests:
```bash
cd "${input:tapDirectory}"
python -m pytest tests/unittests/ -v 2>&1
```

If `pytest` is not available, fall back to `nosetests tests/unittests/ -v`.

**On failure:** Read the full terminal error, fix the failing test(s), and re-run. Repeat until all pass. Fix autonomously � do not ask the user.

Common causes:
- Wrong patch path (patch where the function is *used*, not where it is *defined*)
- Missing `return_value` on a mock
- Import errors � check `setup.py` for the correct package name

---

## Step 5 � Coverage Report

```bash
cd "${input:tapDirectory}"
pip install pytest-cov -q
python -m pytest tests/unittests/ --cov=tap_<name> --cov-report=term-missing -v 2>&1
```

Add tests for any uncovered critical lines in `client.py`, `discover.py`, and `sync.py`.

---

## Step 6 � Final Summary

After all tests pass, print:

```
? Unit tests complete for <tap_name>

Branch: ${input:branchName}
Tests created/updated:
  - tests/unittests/test_client.py    (<N> tests)
  - tests/unittests/test_discovery.py (<N> tests)
  - tests/unittests/test_sync.py      (<N> tests)

All <total_N> tests passing.

Coverage:
  - client.py:    XX%
  - discover.py:  XX%
  - sync.py:      XX%

Next steps:
  1. Review tests and verify assertions match expected behaviour
  2. Run real discovery: tap-<name> -c config.json -d
  3. Commit and push: git push origin ${input:branchName}
```

---

## Important Rules

- Never make real HTTP calls in tests � always mock
- Never delete existing tests � only add or fix
- Always pull latest from parent branch before creating/switching branches
- If any terminal command fails, read the error and fix it � do not ask the user
- One assertion per test method � keep tests focused
- Test docstrings should describe *what* is verified, not *how*
