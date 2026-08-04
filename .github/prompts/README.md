# Singer Tap Prompt Workflows

This folder contains **VS Code Copilot agent prompt files** (`.prompt.md`) that automate common Singer tap development tasks. Each prompt is a self-contained workflow you can run from the Copilot Chat panel.

---

## How to Run a Prompt

1. Open **GitHub Copilot Chat** in VS Code (`Ctrl+Alt+I`).
2. Type `/` followed by the prompt name — e.g. `/singer-tap-dev-workflow`.
3. Copilot will ask for the required inputs (or read them from a JSON file).
4. The agent executes each step autonomously and reports results.

> **Tip:** For repeatable runs, fill in [`workflow-inputs.sample.json`](workflow-inputs.sample.json), save a copy next to your tap, and pass the path as `inputFile` when prompted.

---

## Prompts at a Glance

| Prompt file | Purpose | Key inputs |
|---|---|---|
| [`singer-tap-dev-workflow`](#1-singer-tap-dev-workflow) | **Master orchestrator** — runs any combination of tasks in one shot | `tapNameOrUrl`, `tapDirectory`, `tasks` |
| [`singer-tap-repo-setup`](#2-singer-tap-repo-setup) | Clone, create venv, install, and branch the tap | `tapNameOrUrl`, `tapDirectory`, `branchName` |
| [`singer-tap-upgrade-packages`](#3-singer-tap-upgrade-packages) | Upgrade pinned packages in `setup.py` to latest | `tapDirectory`, `skipPackages`, `dryRun` |
| [`singer-tap-unittests`](#4-singer-tap-unittests) | Generate / update unit tests with maximum coverage | `tapNameOrUrl`, `tapDirectory`, `branchName` |
| [`singer-tap-integration-tests`](#5-singer-tap-integration-tests) | Scaffold the full tap-tester integration suite (live or mock mode) | `tapDirectory`, `branchName`, `testMode` |
| [`singer-tap-schema-audit`](#6-singer-tap-schema-audit) | Deep-audit every JSON schema (Singer conventions) | `tapDirectory`, `streamsToAudit`, `fixIssues` |
| [`singer-tap-discovery-sync-test`](#7-singer-tap-discovery-sync-test) | End-to-end discovery + sync test (real creds or mock) | `tapNameOrUrl`, `tapDirectory`, `hasTestAccount` |
| [`singer-tap-new-stream`](#8-singer-tap-new-stream) | Scaffold a brand-new stream (schema + sync + tests) | `tapDirectory`, `streamName`, `apiEndpoint` |
| [`singer-tap-release-prep`](#9-singer-tap-release-prep) | Bump version, update CHANGELOG, create git tag | `tapDirectory`, `releaseType`, `releaseSummary` |
| [`singer-tap-commit-changes`](#10-singer-tap-commit-changes) | Stage and commit all workflow changes with a smart message | `tapDirectory`, `commitScope` |

---

## Detailed Reference

### 1. `singer-tap-dev-workflow`

**Master entry-point.** Orchestrates all other prompts in the correct order based on the `tasks` input. Use this instead of running individual prompts one by one.

**Inputs:**

| Input | Description | Default |
|---|---|---|
| `tapNameOrUrl` | Tap name or full Git URL e.g. `tap-klaviyo` or `https://github.com/singer-io/tap-klaviyo` | *(required)* |
| `tapDirectory` | Full local path to the tap folder | *(required)* |
| `tasks` | Comma-separated tasks to run: `setup`, `upgrade-packages`, `unit-tests`, `integration-tests`, `schema-audit`, `discovery-sync`, `release`, `commit` — or `all` | `all` |
| `branchName` | Working branch name | `feature/tap-improvements` |
| `parentBranch` | Parent branch to base work on (blank = auto-detect) | *(blank)* |
| `skipPackages` | Packages to skip during upgrade e.g. `singer-python,backoff` | *(blank)* |
| `dryRunUpgrade` | `yes` = show what would change, `no` = apply | `no` |
| `hasTestAccount` | `yes` = use real API credentials for discovery/sync | `no` |
| `configFilePath` | Path to `config.json` with credentials (only when `hasTestAccount=yes`) | *(blank)* |
| `releaseType` | `patch` \| `minor` \| `major` | `patch` |
| `releaseSummary` | One-line changelog entry for the release | *(blank)* |

**Example — run unit tests, integration tests and commit only:**
```
/singer-tap-dev-workflow
tapNameOrUrl: tap-monday
tapDirectory: C:\Users\you\workspace\taps\tap-monday
tasks: unit-tests,integration-tests,commit
branchName: add-unittests
```

---

### 2. `singer-tap-repo-setup`

Clones the tap (if not already present), creates a Python virtual environment, installs the tap in editable mode, and checks out / creates the working branch.

**Inputs:**

| Input | Description | Default |
|---|---|---|
| `tapNameOrUrl` | Tap name or full Git URL | *(required)* |
| `tapDirectory` | Local path where the tap should live | *(required)* |
| `parentBranch` | Branch to base work on (blank = auto-detect) | *(blank)* |
| `branchStrategy` | `new` = create new branch \| `existing` = checkout existing | `new` |
| `branchName` | Branch name to create or checkout | *(required)* |

---

### 3. `singer-tap-upgrade-packages`

Reads `install_requires` from `setup.py`, resolves the latest PyPI version for each dependency, rewrites `setup.py`, reinstalls, and runs unit tests to confirm nothing broke.

**Inputs:**

| Input | Description | Default |
|---|---|---|
| `tapDirectory` | Full local path to the tap | *(required)* |
| `skipPackages` | Packages to leave untouched e.g. `singer-python,backoff` | *(blank)* |
| `dryRun` | `yes` = report only, `no` = apply changes | `no` |
| `runTests` | `yes` = run unit tests after upgrading | `yes` |
| `commitChanges` | `yes` = commit `setup.py` after successful upgrade | `no` |

---

### 4. `singer-tap-unittests`

Generates or updates unit tests for a Singer tap — covers client methods, stream sync logic, bookmarking, error handling, and schema output. Validates that all tests pass before finishing.

**Inputs:**

| Input | Description | Default |
|---|---|---|
| `tapNameOrUrl` | Tap name or full Git URL | *(required)* |
| `tapDirectory` | Full local path to the tap | *(required)* |
| `parentBranch` | Parent branch (blank = auto-detect) | *(blank)* |
| `branchStrategy` | `new` \| `existing` | `new` |
| `branchName` | Branch name for the test changes | *(required)* |

---

### 5. `singer-tap-integration-tests`

Scaffolds or updates the **full tap-tester integration test suite** for a Singer tap. Reads the tap source to automatically discover streams, primary keys, replication methods, and credential keys, then generates:
- `tests/base.py` — stream metadata, credentials, and shared helper methods
- `tests/test_discovery.py` — catalog structure validation
- `tests/test_all_fields.py` — all fields returned by the API
- `tests/test_automatic_fields.py` — minimum required fields
- `tests/test_bookmark.py` — incremental bookmark behaviour
- `tests/test_start_date.py` — start date filtering
- `tests/test_pagination.py` — pagination completeness
- `tests/test_interrupted_sync.py` — resuming after an interrupted sync

When `testMode=mock`, generates the same files using `unittest.mock.patch` on tap internals — no live API account or tap-tester installation required.

**Inputs:**

| Input | Description | Default |
|---|---|---|
| `tapDirectory` | Full local path to the tap | *(required)* |
| `branchStrategy` | `new` \| `existing` | `new` |
| `branchName` | Branch name for the integration test changes | *(required)* |
| `testMode` | `live` = real tap-tester account tests \| `mock` = mock-based tests with no live API | `live` |

> **Tip:** Use `testMode=mock` when you do not have a test account or are working in CI without credentials.

---

### 6. `singer-tap-schema-audit`

Audits every JSON schema file in the tap against Singer conventions: nullable fields, `key_properties`, replication keys, `$ref` resolution, and metadata. Produces an annotated report and can auto-fix safe issues.

**Inputs:**

| Input | Description | Default |
|---|---|---|
| `tapDirectory` | Full local path to the tap | *(required)* |
| `streamsToAudit` | Comma-separated stream names to audit (blank = all) | *(blank)* |
| `fixIssues` | `yes` = auto-fix safe issues, `no` = report only | `no` |

---

### 7. `singer-tap-discovery-sync-test`

Runs the full Singer pipeline end-to-end:
1. `--discover` → validates the catalog structure.
2. Sync run → validates SCHEMA, RECORD, and STATE messages.

Uses real credentials when `hasTestAccount=yes`, otherwise auto-generates mock data.

**Inputs:**

| Input | Description | Default |
|---|---|---|
| `tapNameOrUrl` | Tap name or full Git URL | *(required)* |
| `tapDirectory` | Full local path to the tap | *(required)* |
| `hasTestAccount` | `yes` = real creds, `no` = mock mode | `no` |
| `configFilePath` | Path to `config.json` (only when `hasTestAccount=yes`) | *(blank)* |
| `streamsToTest` | Comma-separated streams to test (blank = all) | *(blank)* |
| `startDate` | ISO 8601 start date for incremental streams | `2024-01-01T00:00:00Z` |

---

### 8. `singer-tap-new-stream`

Scaffolds a complete new stream inside an existing tap:
- Creates the JSON schema file (inferred from a sample API response or generated as a stub).
- Adds sync logic (incremental or full-table) following existing tap conventions.
- Registers the stream in the discovery / catalog layer.
- Generates a unit test skeleton.

**Inputs:**

| Input | Description | Default |
|---|---|---|
| `tapDirectory` | Full local path to the tap | *(required)* |
| `streamName` | Snake_case stream name e.g. `email_templates` | *(required)* |
| `apiEndpoint` | Full API URL for the stream | *(required)* |
| `replicationMethod` | `FULL_TABLE` \| `INCREMENTAL` | `FULL_TABLE` |
| `replicationKey` | Field used as bookmark for INCREMENTAL (blank for FULL_TABLE) | *(blank)* |
| `keyProperties` | Comma-separated primary key field(s) e.g. `id` | `id` |
| `sampleApiResponse` | Paste a sample JSON response to generate the schema (blank = stub) | *(blank)* |

---

### 9. `singer-tap-release-prep`

Bumps the version in `setup.py` / `pyproject.toml`, populates `CHANGELOG.md` with all commits since the last tag, and optionally creates an annotated git tag locally. Does **not** push or publish.

**Inputs:**

| Input | Description | Default |
|---|---|---|
| `tapDirectory` | Full local path to the tap | *(required)* |
| `releaseType` | `patch` \| `minor` \| `major` | `patch` |
| `releaseSummary` | One-line summary for the changelog heading | *(blank)* |
| `createGitTag` | `yes` = create local annotated tag, `no` = bump only | `yes` |

---

### 10. `singer-tap-commit-changes`

Detects all unstaged/staged changes in the tap, groups them by category (packages, unit tests, schemas, changelog, version, other), builds a descriptive commit message automatically, then stages and commits.

**Inputs:**

| Input | Description | Default |
|---|---|---|
| `tapDirectory` | Full local path to the tap | *(required)* |
| `commitScope` | Short scope for the commit subject e.g. `tap-monday` (blank = auto-detect) | *(blank)* |

---

## Using `workflow-inputs.sample.json`

[`workflow-inputs.sample.json`](workflow-inputs.sample.json) is a template you can copy next to any tap and fill in once — avoiding repeated prompts for the same values.

```jsonc
{
  "tapNameOrUrl": "tap-ms-teams",
  "tapDirectory": "C:\\Users\\you\\workspace\\taps\\tap-ms-teams",
  "tasks": "unit-tests,integration-tests,schema-audit,commit",
  "branchName": "gl-master",
  "parentBranch": "master",
  "branchStrategy": "existing",
  "testMode": "live",
  "skipPackages": "",
  "dryRunUpgrade": "no",
  "hasTestAccount": "no",
  "configFilePath": "",
  "releaseType": "patch",
  "releaseSummary": ""
}
```

Pass the file path as `inputFile` when the master workflow asks for it, and all fields will be pre-filled.

---

## Recommended Task Combinations

| Goal | `tasks` value |
|---|---|
| Full pipeline (first time) | `setup,upgrade-packages,unit-tests,integration-tests,schema-audit,commit` |
| Unit tests + integration tests + commit | `unit-tests,integration-tests,commit` |
| Just fix unit tests and commit | `unit-tests,commit` |
| Add integration tests only (mock, no account) | Run `singer-tap-integration-tests` with `testMode=mock` |
| Add integration tests (live account) | Run `singer-tap-integration-tests` with `testMode=live` |
| Audit schemas and auto-fix | `schema-audit` (set `fixIssues=yes`) |
| Add a new stream, then test | Run `singer-tap-new-stream` → then `discovery-sync` |
| Cut a release | `release,commit` |
| Upgrade dependencies only | `upgrade-packages` |
