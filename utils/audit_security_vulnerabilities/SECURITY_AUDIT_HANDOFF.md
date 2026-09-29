# Singer.io Security Audit — Handoff Notes

Context for continuing/maturing `audit_singer_security.py`. Written 2026-09-29.

## Goal

Produce an actionable, Jira-ready security remediation inventory for every
`singer-io` tap repository: discover all repos → identify taps → pull
GitHub's Dependabot vulnerability data → cross-reference declared Python
dependencies + PyPI + existing PRs/issues → prioritize → generate one Excel
workbook. Full original requirements are in the chat history that produced
this script; the short version is "never silently skip a tap, and make it
obvious what to file in Jira and why."

## Current state

- **Entry point / everything:** [audit_singer_security.py](audit_singer_security.py) — a single,
  self-contained script (no local package, no dependency on `audit_taps.py`).
  Read its module docstring first; it has full setup/run instructions.
- **Output:** one workbook, `<output>/singer_tap_security_summary.xlsx`, with
  5 tabs: `Tap Summary`, `Vulnerabilities`, `Jira Tickets`,
  `Dependency Details`, `Analysis Log`. CSV output was intentionally removed
  (an earlier iteration wrote 5 CSVs too — see `security-audit/*.csv` in the
  repo, which are now **stale leftovers** from that earlier run and can be
  deleted).
- Last full-org run (~197 taps) completed cleanly: 190 analyzed, 7
  archived/skipped-by-design, 0 manual-review, 0 skipped, 41 taps with
  vulnerabilities, 116 Jira tickets recommended. Runtime ~4-5 minutes with
  default `--max-workers 4`.
- Quick re-test loop: `python audit_singer_security.py --tap tap-ms-dynamics tap-mailjet --output tmp/test`
  (skips org-wide discovery, fast, uses real GitHub/PyPI data).

## Architecture (all in the one file, top to bottom)

`RepoInfo`/`Vulnerability`/etc. dataclasses → `GitHubClient` (REST, retries,
pagination, rate-limit handling) → `PyPIClient` → `ThreatIntelClient` (CISA
KEV only) → `RepositoryDiscovery` + `TapDetector` → `SecurityAnalyzer`
(Dependabot alerts + PR/issue activity) → inlined AST-based `setup.py` parser
+ `DependencyAnalyzer` (also handles `requirements*.txt`, `pyproject.toml`,
`Pipfile`) → `VulnerabilityAnalyzer` (the core "what does this mean" logic:
severity normalization, breaking-change/code-change heuristics, recommended
priority, Jira summary/description generation) → `JiraTicketGenerator` →
`AuditLogger` → `ReportGenerator` (openpyxl) → `SingerSecurityAuditor`
(orchestrates one tap's audit) → `main()`/CLI.

## Key data-source decisions & gotchas (don't relearn these the hard way)

- **Vulnerability source of truth = GitHub Dependabot Alerts REST API**
  (`/repos/{org}/{repo}/dependabot/alerts`), not HTML scraping of the org
  security overview page (that page isn't a stable/scrapable API surface).
- **CVSS gotcha:** `security_advisory.cvss_severities.cvss_v4` frequently
  comes back as `{"score": 0.0, "vector_string": None}` when the vuln
  genuinely isn't CVSSv4-rated — that's *not* a real 0.0 score. Always check
  `vector_string` is truthy before trusting a `cvss_v4`/`cvss_v3` entry, else
  fall back to the next one (`_extract_cvss`).
- **EPSS is already embedded** in the advisory JSON as
  `security_advisory.epss = {"percentage": ..., "percentile": ...}` — no
  need to call the external FIRST.org API (an earlier draft did; removed).
- Repository **security-advisories endpoint** (`/security-advisories`) was
  fetched in an earlier draft but dropped during consolidation — it's mostly
  empty for these repos (that endpoint is for advisories the repo maintainer
  authored, not consumed advisories) and added API calls for little value.
  Could be reinstated if a use case shows up.
- Dependency manifests are read from `./singer_tap_repos/<tap>/` first (fast,
  no API calls, works if you've already cloned taps via `audit_taps.py`),
  falling back to the GitHub Contents API otherwise.

## Known limitations / good next steps

1. **No ecosystem filtering on Dependabot alerts.** If a tap repo has a
   GitHub Actions workflow or an npm manifest, Dependabot alerts for those
   ecosystems would still show up and get (mis)matched against pip
   dependency declarations. Add a filter on `alert.dependency.package.ecosystem == "pip"`.
2. **Existing issue/PR matching is a naive substring heuristic**
   (`_find_existing_remediation`): checks if the package name or GHSA id
   appears in an issue/PR title. This will both miss real matches (different
   phrasing) and occasionally false-positive. A better approach: search PR
   file diffs for the manifest + package name, or use the GitHub Dependabot
   PR "dependency-name" label GitHub sometimes attaches.
3. **Breaking-change risk is major/minor version diff only** — doesn't look
   at actual changelogs/release notes. Could integrate with PyPI's
   `project_urls`/changelog links or a changelog-diffing service.
4. **No on-disk caching between runs.** Every run refetches everything from
   GitHub/PyPI. Fine at current scale (~200 repos, few minutes), but if this
   grows, consider an ETag/If-None-Match cache or a local SQLite results
   cache keyed by repo+alert id.
5. **No GraphQL usage.** Original spec asked for "REST or GraphQL"; only
   REST is implemented. GraphQL could reduce request count (e.g. one query
   per repo for alerts + PRs + issues combined) if rate limits become a
   concern at larger scale.
6. **Only pip/Python dependency parsing.** Fine since Singer taps are
   Python, but if the tool is ever pointed at a different org, `_parse_*`
   methods only understand `setup.py`/`requirements*.txt`/`pyproject.toml`/`Pipfile`.
7. **No automated tests.** Everything so far has been validated by live
   smoke tests against real `singer-io` repos (see session history), not a
   pytest suite. Worth adding unit tests around `VulnerabilityAnalyzer`
   (severity/priority/breaking-change logic) and the `setup.py` AST parser,
   since those are the parts most likely to silently regress.
8. **`--include-archived` deep-scans archived repos but nothing distinguishes
   "genuinely archived" from "disabled for other reasons"** beyond the
   `Active/Archived` column — fine for now, just noted.

## Security note (unrelated to the audit logic, but found during this work)

`_test_full_flow.py` has a real-looking GitHub token hardcoded inline
(duplicated from `tmp/configs/config.json`). It's not git-tracked (no `.git`
here / `tmp/` is gitignored) but is still bad practice sitting in a plaintext
script. Recommend: make that test load the token via `load_token()` like the
real script does, and rotate the token since it's been handled directly.

## If you pick this up next

- Read the module docstring in `audit_singer_security.py` first — it's kept
  up to date with actual CLI flags/behavior.
- Run `--tap <a few taps>` before any full-org run to validate changes
  quickly (full org run takes several minutes and burns GitHub API quota).
- The `Analysis Log` tab is the audit's own self-check — if you change
  anything in `SingerSecurityAuditor.audit_tap`, verify the reconciliation
  numbers printed at the end still add up (that's also the non-zero exit
  code condition in `main()`).
