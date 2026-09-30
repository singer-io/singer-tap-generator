# Singer.io Security Audit — Handoff Notes

Context for maintaining `audit_singer_security.py` (v2.0.0, 2026-09-30).
User-facing setup/run documentation lives in [README.md](README.md).

## Goal

Actionable, Jira-ready security remediation inventory for every `singer-io`
tap: discover repos → identify taps → pull Dependabot + code-scanning alerts →
cross-reference manifests, lock files, PyPI, CISA KEV and open PRs/issues →
prioritize → one Excel workbook. Never silently skip a tap.

## Current state

- Single self-contained script + offline pytest suite
  (`test_audit_singer_security.py`, 23 tests) + `requirements.txt`.
- Last full-org run (2026-09-30, ~3 min, ~1,200 API calls): 215 repos,
  197 taps, 190 analyzed, 7 archived (skipped by design), 0 failed,
  42 taps with open alerts, 203 open alerts (33 High / 160 Medium / 10 Low),
  58 grouped Jira tickets.
- **Validated** against the org Security Overview CSV export of the same day
  (`--validate-csv`): 380/380 repo/tool pairs matched on alert numbers and
  severities.

## Architecture (top to bottom in the file)

helpers → dataclasses → `GitHubClient` (retries, pagination, primary +
secondary rate limits, categorized error strings) → `PyPIClient` /
`ThreatIntelClient` (KEV) → `TapDetector` → static manifest + lock-file
parsers (AST for setup.py; never executes code) → `DependencyAnalyzer` →
`VulnerabilityAnalyzer` (scope, relationship, current version, fix-allowed,
breaking-change risk, priority, Jira candidacy) → `JiraTicketGenerator`
(one ticket per tap + package/rule) → `SingerSecurityAuditor` (per tap) →
summaries → `ExportValidator` → `ReportGenerator` (column-spec driven) → CLI.

## Changes vs. v1

- Code-scanning (CodeQL) alerts included; ecosystem column added.
- Runtime/development scope and direct/transitive relationship from the
  Dependabot payload + manifests; package names PEP 503-normalized.
- Lock files (`poetry.lock`, `Pipfile.lock`, `uv.lock`) give real current
  versions for transitive deps.
- Existing remediation: only OPEN PRs/issues; matches advisory ids, package +
  remediation keyword in title, or Dependabot branch names (v1 matched any
  historical PR by substring, which hid real work from Jira).
- Jira tickets grouped per tap + package targeting the max fixed version.
- New sheets: Overview (metadata/reconciliation), Severity Summary, Package
  Summary, Not Audited, Validation. Excel-illegal characters and >32k cells
  sanitized; timezone-aware datetimes converted.
- Transient failures retried in an extra pass; `--tap` lookup failures are
  recorded instead of dropped; token also read from `GH_TOKEN` / gh CLI.

## Known limitations / next steps

1. Breaking-change risk is a version heuristic only (no changelog parsing).
2. Only open alerts; no historical trend between runs (a JSON snapshot +
   diff would enable "new since last audit").
3. REST only; GraphQL could cut request count if the org grows a lot.
4. Secret-scanning alerts intentionally excluded (admin-only, sensitive).
5. Empty repositories (e.g. `tap-testt`) show a manifest listing error in
   the Analysis Log; harmless.
