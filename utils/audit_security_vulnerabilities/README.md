# Singer Tap Security Vulnerability Audit

`audit_singer_security.py` audits every Singer tap repository in the
[`singer-io`](https://github.com/singer-io) GitHub organization for known
security vulnerabilities and writes **one Excel workbook** that the team can
use to review, prioritize and raise Jira tickets.

## What it does

1. Discovers every repository in the org (or only the ones passed with `--tap`)
   and classifies taps (`tap-*` name or `singer-tap` topic). Non-taps are
   recorded in the *Analysis Log* - nothing is silently dropped.
2. For each tap (in parallel) it fetches:
   - open **Dependabot alerts** (CVE/GHSA, severity, CVSS, CWE, EPSS,
     vulnerable range, first patched version, runtime/development scope,
     direct/transitive relationship);
   - open **code-scanning (CodeQL) alerts** (skip with `--skip-code-scanning`);
   - dependency manifests on the default branch (`setup.py`, `setup.cfg`,
     `pyproject.toml`, `Pipfile`, `requirements*.txt`) and lock files
     (`poetry.lock`, `Pipfile.lock`, `uv.lock`) - parsed statically, tap code
     is never executed;
   - open PRs / issues, to detect remediation already in flight.
3. Enriches findings with the latest PyPI version and the CISA Known Exploited
   Vulnerabilities (KEV) catalog.
4. Works out, per alert: current version, whether the current constraint
   already allows the fix, breaking-change risk, expected code changes,
   recommended action, priority (P0-P4) and Jira candidacy.
5. Groups Jira candidates into **one ticket per tap + package** (or tap + code
   scanning rule), targeting the version that clears *all* grouped alerts.
6. Handles GitHub rate limits (primary and secondary), retries 5xx/network
   errors, re-audits taps that failed transiently, and records every tap that
   could not be audited together with the reason and next step.

## Setup

Python 3.8+ (3.11+ recommended).

```bash
cd utils/audit_security_vulnerabilities
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### GitHub token

Dependabot and code-scanning alerts require an authenticated token. Use a
classic PAT with the `repo` + `security_events` scopes (or `public_repo` +
`security_events` for public repos only), or a fine-grained token with
read access to *Dependabot alerts*, *Code scanning alerts*, *Contents*,
*Pull requests*, *Issues* and *Metadata*. An org member with the security
manager role sees the same data as the org Security Overview page.

The token is looked up in this order (it is never written to the report or log):

1. `--github-token <token>`
2. `GITHUB_TOKEN` or `GH_TOKEN` environment variable
3. `--config` JSON file (default `tmp/configs/config.json`) with `{"github_token": "..."}`
4. an existing GitHub CLI login (`gh auth login`)

```bash
export GITHUB_TOKEN=ghp_xxx          # PowerShell: $env:GITHUB_TOKEN = "ghp_xxx"
```

## Running

```bash
# Whole organization (~200 taps, a few minutes, ~1,500 API requests)
python audit_singer_security.py

# A few taps only (fast; good for testing changes)
python audit_singer_security.py --tap tap-github tap-ms-dynamics tap-pinterest-ads

# Cross-check results against a GitHub org security overview CSV export
#   (Organization > Security > Overview > Export CSV)
python audit_singer_security.py --validate-csv organization_security_overview_alerts_singer-io_<date>.csv
```

| Option | Default | Purpose |
|---|---|---|
| `--org` | `singer-io` | Organization to audit |
| `--tap REPO [REPO ...]` | all taps | Audit only these repositories |
| `--output` | `./security-audit` | Output directory |
| `--output-file` | `singer_tap_security_summary.xlsx` | Workbook name |
| `--include-archived` | off | Deep-scan archived/disabled repos too |
| `--skip-code-scanning` | off | Do not fetch CodeQL alerts |
| `--local-repos-dir DIR` | off | Read manifests from local clones `DIR/<tap>/` instead of the API |
| `--validate-csv FILE` | off | Adds a *Validation* sheet comparing results with an org export |
| `--max-workers` | 4 | Taps audited in parallel |
| `--timeout` / `--retry-count` | 30 / 5 | HTTP timeout and retries |
| `--retry-failed-passes` | 1 | Re-audit passes for transient failures |
| `--fail-on-errors` | off | Non-zero exit if any tap failed / needs manual review |
| `--verbose` | off | Debug logging |

Outputs in `--output`:

- `singer_tap_security_summary.xlsx` - the report
- `audit_run.log` - full run log

Exit codes: `0` success, `1` reconciliation gap (or `--fail-on-errors`
triggered), `2` setup problem (missing `openpyxl`, rejected token).

## The workbook

Every table has a frozen header, auto-filters, severity/priority colouring and
clickable GitHub/advisory links.

| Sheet | Contents |
|---|---|
| **Overview** | Audit date, execution metadata (scope, user, token source, API usage, script version, command line), reconciliation, headline counts, priority legend |
| **Severity Summary** | Counts by severity and by priority (runtime vs dev, direct vs transitive, fix available, KEV, taps affected, Jira candidates) and a tap x severity matrix |
| **Tap Summary** | One row per tap: status, severity counts, highest severity/CVSS, oldest alert, recommended priority, Jira tickets, primary recommended action |
| **Vulnerabilities** | One row per open alert, sorted by tap then severity: CVE/GHSA, package, scope, versions, fix, CVSS/EPSS/KEV, impact, action, breaking-change risk, existing PRs/issues, Jira candidacy and source URLs |
| **Jira Candidates** | One ready-to-file ticket per tap + package/rule (summary, priority, labels, description in Jira wiki markup, acceptance criteria, references). Columns map to Jira CSV import fields |
| **Package Summary** | Each vulnerable package across all taps - useful for org-wide bulk upgrades |
| **Dependency Details** | Every declared (and vulnerable transitive) dependency per tap vs latest PyPI version and security fix |
| **Not Audited** | Taps that could not be fully audited, with reason and suggested next step |
| **Analysis Log** | Every discovered repository and what happened to it |
| **Validation** | (with `--validate-csv`) per repo/tool comparison of alert numbers and severities against the org export |

### Priority model

| Priority | Rule |
|---|---|
| P0 - Immediate | Critical severity, or CVE listed in CISA KEV |
| P1 - High | High severity, or Medium with EPSS >= 10% |
| P2 - Normal | Medium severity |
| P3 - Low | Low severity |
| P4 - Informational | Anything else |

Development-only dependencies are lowered by one level. An alert is a Jira
candidate when it is open, has a published fix and is not P4; alerts without
a fix are candidates only when Critical/High. Code-scanning findings are
candidates at Medium severity and above.

## Validating results

1. Run with `--validate-csv` using a fresh org security overview export;
   the *Validation* sheet should show `Match` for every repo/tool pair
   (differences are only expected for alerts opened/closed between the export
   and the run).
2. Spot-check a few taps manually: open the tap's *Security URL* from the
   *Tap Summary* sheet and compare the open Dependabot alert count and
   severities with the *Vulnerabilities* sheet.
3. Check the *Overview* reconciliation says `OK - every tap accounted for`
   and review the *Not Audited* sheet.

## Tests

```bash
python -m pytest test_audit_singer_security.py -q
```

The tests are offline (no GitHub/PyPI calls) and cover version/range logic,
manifest and lock file parsing, vulnerability analysis, prioritization, Jira
grouping, workbook generation and export validation.

## Known limitations

- Only open alerts are reported (dismissed/fixed alerts are out of scope).
- Breaking-change risk is a version-number heuristic (major/minor/patch;
  calendar versions treated as low risk); it does not read changelogs.
- Existing PR/issue matching uses advisory ids, package names with a
  remediation keyword in the title, and Dependabot branch names - review
  matches before closing tickets.
- Secret-scanning alerts are not included (they need admin access and would
  put secret material into a spreadsheet).
