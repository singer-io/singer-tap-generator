#!/usr/bin/env python3
"""
Singer.io Security Vulnerability Audit
======================================

Audits every Singer tap repository in the ``singer-io`` GitHub organization
for known security vulnerabilities and produces ONE Excel workbook that the
team can use to review, prioritize and raise Jira tickets.

Data sources (all read-only):

* GitHub Dependabot alerts REST API   - vulnerable dependencies (CVE/GHSA,
  severity, CVSS, CWE, EPSS, vulnerable range, first patched version,
  runtime/development scope, direct/transitive relationship).
* GitHub code-scanning (CodeQL) alerts - source/workflow findings
  (disable with ``--skip-code-scanning``).
* Repository manifests (setup.py, setup.cfg, requirements*.txt,
  pyproject.toml, Pipfile) - declared version constraints.
* Open pull requests / issues        - remediation already in flight.
* PyPI JSON API                      - latest released versions.
* CISA Known Exploited Vulnerabilities catalog - actively exploited CVEs.

Setup and usage are documented in README.md next to this script. Quick start::

    pip install -r requirements.txt
    export GITHUB_TOKEN=ghp_xxx          # or GH_TOKEN / --github-token / gh CLI login
    python audit_singer_security.py                          # whole org
    python audit_singer_security.py --tap tap-github tap-ms-dynamics   # quick check
    python audit_singer_security.py --validate-csv org_security_alerts_export.csv

Workbook tabs: Overview, Severity Summary, Tap Summary, Vulnerabilities,
Jira Candidates, Package Summary, Dependency Details, Not Audited,
Analysis Log and (with ``--validate-csv``) Validation.
"""
import argparse
import ast
import base64
import configparser
import csv
import datetime as dt
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.utils import canonicalize_name
    from packaging.version import InvalidVersion, Version
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(f"ERROR: missing required package ({exc}). Install with: pip install -r requirements.txt\n")
    sys.exit(1)

try:
    import openpyxl
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    HAVE_OPENPYXL = True
except ImportError:  # pragma: no cover
    HAVE_OPENPYXL = False
    ILLEGAL_CHARACTERS_RE = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")

try:
    import tomllib
except ImportError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None

__version__ = "2.0.0"

LOGGER = logging.getLogger("singer_security_audit")

GITHUB_API = "https://api.github.com"
PYPI_API = "https://pypi.org/pypi/{name}/json"
KEV_FEED = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
DEFAULT_CONFIG_PATH = Path("tmp/configs/config.json")
DEFAULT_REPORT_NAME = "singer_tap_security_summary.xlsx"
EXCEL_CELL_LIMIT = 32000

RUNTIME, DEVELOPMENT = "Runtime", "Development"
DIRECT, TRANSITIVE, UNKNOWN = "Direct", "Transitive", "Unknown"
TOOL_DEPENDABOT, TOOL_CODEQL = "Dependabot", "CodeQL"

MANIFEST_CANDIDATES = (
    "setup.py", "setup.cfg", "pyproject.toml", "Pipfile", "requirements.txt",
    "requirements-dev.txt", "requirements_dev.txt", "dev-requirements.txt",
    "requirements-test.txt", "requirements_test.txt", "test-requirements.txt",
)
LOCKFILE_CANDIDATES = ("poetry.lock", "Pipfile.lock", "uv.lock")
DEV_EXTRA_NAMES = {"dev", "develop", "development", "test", "tests", "testing", "lint", "linting", "docs", "qa", "ci"}

SEVERITIES = ("Critical", "High", "Medium", "Low", "Informational")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}
SEVERITY_NORMALIZE = {
    "critical": "Critical", "high": "High", "moderate": "Medium", "medium": "Medium", "low": "Low",
    # code-scanning rule severities when no security_severity_level is set
    "error": "High", "warning": "Medium", "note": "Low",
}
PRIORITIES = ("P0 - Immediate", "P1 - High", "P2 - Normal", "P3 - Low", "P4 - Informational")
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}
JIRA_PRIORITY = dict(zip(PRIORITIES, ("Highest", "High", "Medium", "Low", "Lowest")))
PRIORITY_GUIDANCE = {
    "P0 - Immediate": "Critical severity or actively exploited (CISA KEV) runtime issue - fix immediately",
    "P1 - High": "High severity runtime issue, or Medium with high exploit probability (EPSS >= 10%) - fix in current sprint",
    "P2 - Normal": "Medium severity runtime / High severity development-only issue - schedule in next sprint",
    "P3 - Low": "Low severity runtime / Medium severity development-only issue - routine maintenance",
    "P4 - Informational": "Low-risk development-only or informational finding - batch with routine dependency updates",
}

STATUS_VULNERABLE = "Vulnerabilities found"
STATUS_CLEAN = "No open vulnerabilities"
STATUS_ARCHIVED = "Skipped - archived/disabled"
STATUS_MANUAL = "Manual review required"
STATUS_FAILED = "Failed"
STATUS_NOT_TAP = "Skipped - not a tap"
TRANSIENT_ERROR_PREFIXES = ("network_error", "timeout", "server_error", "rate_limited")
REMEDIATION_KEYWORDS = ("bump", "upgrade", "update", "pin", "security", "vulnerab", "cve-", "ghsa-", "fix")

SEVERITY_FILLS = {"Critical": "FF9999", "High": "FFC7A0", "Medium": "FFEB9C", "Low": "C6EFCE", "Informational": "E7E6E6"}
PRIORITY_FILLS = dict(zip(PRIORITIES, ("FF9999", "FFC7A0", "FFEB9C", "C6EFCE", "E7E6E6")))
STATUS_FILLS = {
    STATUS_VULNERABLE: "FFC7A0", STATUS_CLEAN: "C6EFCE", STATUS_ARCHIVED: "E7E6E6",
    STATUS_MANUAL: "FFEB9C", STATUS_FAILED: "FF9999", STATUS_NOT_TAP: "F2F2F2",
}
YES_NO_FILLS = {"Yes": "FFC7A0", "No": "C6EFCE"}


# ============================================================================
# Small helpers
# ============================================================================

def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_github_datetime(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_package(name: str) -> str:
    return canonicalize_name(name or "")


def parse_version(value: Optional[str]) -> Optional[Version]:
    if not value:
        return None
    try:
        return Version(str(value).strip())
    except InvalidVersion:
        return None


def parse_vulnerable_range(range_text: str) -> Optional[SpecifierSet]:
    """Converts a GitHub advisory range (e.g. ``>= 2.0, < 2.5`` or ``= 1.0``) to a SpecifierSet."""
    if not range_text:
        return None
    parts = []
    for part in range_text.split(","):
        part = part.strip()
        if not part:
            continue
        match = re.match(r"^(<=|>=|==|!=|<|>|=)\s*(\S+)$", part)
        if not match:
            return None
        op, version = match.groups()
        parts.append(f"{'==' if op == '=' else op}{version}")
    try:
        return SpecifierSet(",".join(parts))
    except InvalidSpecifier:
        return None


def version_in_range(version: Optional[str], range_text: str) -> Optional[bool]:
    spec, parsed = parse_vulnerable_range(range_text), parse_version(version)
    if spec is None or parsed is None:
        return None
    return spec.contains(parsed, prereleases=True)


def max_version(values: Iterable[str]) -> str:
    parsed = [(parse_version(v), v) for v in values if v]
    valid = [p for p in parsed if p[0] is not None]
    if valid:
        return max(valid, key=lambda p: p[0])[1]
    return parsed[0][1] if parsed else ""


def join_unique(values: Iterable[str], sep: str = "\n") -> str:
    seen, out = set(), []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return sep.join(out)


def excel_safe(value):
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
        if len(value) > EXCEL_CELL_LIMIT:
            value = value[:EXCEL_CELL_LIMIT] + " ... [truncated]"
    elif isinstance(value, dt.datetime) and value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)  # Excel has no timezone support
    return value


def is_transient_error(error: str) -> bool:
    return bool(error) and error.startswith(TRANSIENT_ERROR_PREFIXES)


def highest_severity(severities: Iterable[str]) -> str:
    ranked = sorted((s for s in severities if s in SEVERITY_RANK), key=SEVERITY_RANK.get)
    return ranked[0] if ranked else "None"


def highest_priority(priorities: Iterable[str]) -> str:
    ranked = sorted((p for p in priorities if p in PRIORITY_RANK), key=PRIORITY_RANK.get)
    return ranked[0] if ranked else "None"


# ============================================================================
# Data model
# ============================================================================

@dataclass
class RepoInfo:
    name: str
    full_name: str
    html_url: str
    archived: bool = False
    is_fork: bool = False
    disabled: bool = False
    default_branch: str = "master"
    description: str = ""
    visibility: str = ""
    pushed_at: Optional[dt.datetime] = None
    topics: List[str] = field(default_factory=list)
    is_tap: bool = False
    tap_detection_reason: str = ""

    @property
    def security_url(self) -> str:
        return f"{self.html_url}/security"

    @property
    def lifecycle(self) -> str:
        return "Archived" if self.archived else ("Disabled" if self.disabled else "Active")


def repo_from_api(item: dict, org: str) -> RepoInfo:
    name = item.get("name", "")
    return RepoInfo(
        name=name, full_name=item.get("full_name") or f"{org}/{name}",
        html_url=item.get("html_url") or f"https://github.com/{org}/{name}",
        archived=bool(item.get("archived")), is_fork=bool(item.get("fork")), disabled=bool(item.get("disabled")),
        default_branch=item.get("default_branch") or "master", description=item.get("description") or "",
        visibility=item.get("visibility") or ("private" if item.get("private") else "public"),
        pushed_at=parse_github_datetime(item.get("pushed_at")), topics=item.get("topics") or [],
    )


@dataclass
class DependencyDeclaration:
    tap_name: str
    package: str
    scope: str                 # Runtime / Development
    manifest: str
    specifier: str = ""        # normalized PEP 440 specifier, e.g. ">=2.20,<3"
    pinned_version: str = ""   # set only for an exact == pin
    baseline_version: str = "" # pinned version, else highest lower bound
    raw: str = ""
    note: str = ""

    @property
    def key(self) -> str:
        return normalize_package(self.package)


@dataclass
class PyPIInfo:
    status: str
    latest_version: Optional[str]
    error: Optional[str] = None


@dataclass
class RepoActivity:
    prs: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    error: str = ""


@dataclass
class Vulnerability:
    tap_name: str
    repository: str
    repository_url: str
    security_url: str
    tool: str
    alert_number: Optional[int]
    alert_url: str
    alert_state: str = "open"
    vulnerability_id: str = ""
    ghsa: str = ""
    cve: str = ""
    ghsa_url: str = ""
    cve_url: str = ""
    ecosystem: str = ""
    package: str = ""
    rule_id: str = ""
    location: str = ""
    dependency_scope: str = ""
    relationship: str = ""
    manifest: str = ""
    declared_specifier: str = ""
    current_version: str = ""
    current_version_vulnerable: str = ""
    vulnerable_version_range: str = ""
    fixed_version: str = ""
    fix_available: str = ""
    fix_allowed_by_current_spec: str = ""
    severity: str = "Informational"
    cvss_score: Optional[float] = None
    cvss_vector: str = ""
    cwe: str = ""
    epss_percentage: Optional[float] = None
    epss_percentile: Optional[float] = None
    known_exploited: str = "Unknown"
    exploitability: str = ""
    summary: str = ""
    description: str = ""
    technical_impact: str = ""
    recommended_action: str = ""
    upgrade_required: str = ""
    code_changes_required: str = ""
    breaking_change_risk: str = ""
    existing_issue_urls: str = ""
    existing_pr_urls: str = ""
    dependabot_pr: str = "No"
    alert_created: Optional[dt.datetime] = None
    alert_age_days: Optional[int] = None
    advisory_published: Optional[dt.datetime] = None
    recommended_priority: str = "P4 - Informational"
    jira_candidate: str = "No"
    jira_candidate_reason: str = ""
    jira_ref: str = ""
    references: str = ""

    @property
    def group_key(self) -> Tuple[str, str, str]:
        if self.tool == TOOL_DEPENDABOT:
            return (self.tap_name, self.tool, normalize_package(self.package))
        return (self.tap_name, self.tool, self.rule_id)


@dataclass
class TapSummary:
    tap_name: str
    repository_url: str
    security_url: str
    lifecycle: str
    last_pushed: Optional[dt.datetime]
    analysis_status: str
    dependabot_status: str
    code_scanning_status: str
    total_alerts: int = 0
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    informational: int = 0
    runtime_alerts: int = 0
    development_alerts: int = 0
    code_scanning_alerts: int = 0
    fix_available: int = 0
    known_exploited: int = 0
    open_remediation_prs: int = 0
    highest_severity: str = "None"
    highest_cvss: Optional[float] = None
    oldest_alert_days: Optional[int] = None
    recommended_priority: str = "None"
    jira_tickets: int = 0
    primary_recommended_action: str = ""
    notes: str = ""


@dataclass
class JiraTicket:
    ref: str
    tap: str
    issue_type: str
    summary: str
    priority: str
    jira_priority: str
    severity: str
    labels: str
    tool: str
    package_or_rule: str
    dependency_scope: str
    current_version: str
    target_version: str
    alerts_covered: int
    advisories: str
    alert_urls: str
    existing_prs: str
    recommended_action: str
    code_changes_required: str
    breaking_change_risk: str
    description: str
    acceptance_criteria: str
    references: str


@dataclass
class DependencyDetailRow:
    tap: str
    dependency: str
    relationship: str
    scope: str
    manifest: str
    declared_specifier: str
    current_version: str
    latest_pypi_version: str
    fixed_security_version: str
    vulnerable: str
    open_alerts: int
    highest_severity: str
    outdated: str
    upgrade_required: str
    notes: str


@dataclass
class PackageSummaryRow:
    package: str
    ecosystem: str
    alerts: int
    taps_affected: int
    highest_severity: str
    highest_priority: str
    fixed_version_needed: str
    runtime_alerts: int
    development_alerts: int
    advisories: str
    taps: str


@dataclass
class AnalysisLogEntry:
    repository: str
    repository_url: str
    identified_as_tap: str
    tap_detection_reason: str
    lifecycle: str
    dependabot_status: str
    code_scanning_status: str
    dependency_status: str
    vulnerability_scan_successful: str
    vulnerabilities_found: int
    analysis_status: str
    error: str
    attempts: int
    duration_seconds: Optional[float]
    timestamp: dt.datetime


@dataclass
class NotAuditedRow:
    repository: str
    repository_url: str
    analysis_status: str
    reason: str
    next_step: str


@dataclass
class ValidationRow:
    repository: str
    tool: str
    export_open_alerts: int
    script_open_alerts: Optional[int]
    matched: int
    only_in_export: str
    only_in_script: str
    severity_mismatches: str
    result: str
    notes: str


# ============================================================================
# GitHubClient - REST API with retries, pagination, rate-limit + error handling
# ============================================================================

class GitHubAPIError(Exception):
    pass


class GitHubClient:
    """All methods return ``(data, error)``; ``error`` is a string prefixed
    with a machine-readable category (``not_found``, ``forbidden``,
    ``disabled``, ``authentication_error``, ``rate_limited``,
    ``server_error``, ``network_error``, ``timeout``...)."""

    def __init__(self, token: Optional[str] = None, timeout: int = 30, retry_count: int = 5,
                 max_rate_limit_wait: int = 3900, max_rate_limit_retries: int = 6):
        self.timeout = timeout
        self.max_rate_limit_wait = max_rate_limit_wait
        self.max_rate_limit_retries = max_rate_limit_retries
        self._lock = threading.Lock()
        self._request_count = 0
        self.last_rate_limit: Dict[str, str] = {}
        self.session = requests.Session()
        retries = Retry(total=retry_count, backoff_factor=1.5, status_forcelist=[500, 502, 503, 504],
                        allowed_methods=["GET"], raise_on_status=False)
        self.session.mount("https://", HTTPAdapter(max_retries=retries, pool_maxsize=32))
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "User-Agent": f"singer-tap-security-auditor/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._request_count

    def _request(self, url: str, params: Optional[dict] = None) -> requests.Response:
        full_url = url if url.startswith("http") else f"{GITHUB_API}{url}"
        attempt = 0
        while True:
            with self._lock:
                self._request_count += 1
            try:
                resp = self.session.get(full_url, params=params, timeout=self.timeout)
            except requests.Timeout as exc:
                raise GitHubAPIError(f"timeout: {exc}") from exc
            except requests.RequestException as exc:
                raise GitHubAPIError(f"network_error: {exc}") from exc
            self._remember_rate_limit(resp)
            if resp.status_code in (403, 429) and self._is_rate_limited(resp):
                attempt += 1
                if attempt > self.max_rate_limit_retries:
                    return resp
                wait = min(self._rate_limit_wait(resp, attempt), self.max_rate_limit_wait)
                LOGGER.warning("GitHub rate limit hit on %s; sleeping %.0fs (retry %d/%d)",
                               url, wait, attempt, self.max_rate_limit_retries)
                time.sleep(wait)
                continue
            return resp

    def _remember_rate_limit(self, resp: requests.Response) -> None:
        if "X-RateLimit-Remaining" in resp.headers:
            with self._lock:
                self.last_rate_limit = {k: resp.headers.get(f"X-RateLimit-{k}", "")
                                        for k in ("Limit", "Remaining", "Reset", "Resource")}

    @staticmethod
    def _is_rate_limited(resp: requests.Response) -> bool:
        if resp.status_code == 429 or resp.headers.get("X-RateLimit-Remaining") == "0":
            return True
        return "rate limit" in (resp.text or "").lower()  # includes secondary rate limits

    @staticmethod
    def _rate_limit_wait(resp: requests.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(1.0, float(retry_after) + 1)
            except ValueError:
                pass
        if resp.headers.get("X-RateLimit-Remaining") == "0" and resp.headers.get("X-RateLimit-Reset"):
            try:
                return max(1.0, float(resp.headers["X-RateLimit-Reset"]) - time.time() + 2)
            except ValueError:
                pass
        return 60.0 * attempt  # secondary rate limit without guidance: back off progressively

    @staticmethod
    def _error_message(resp: requests.Response) -> str:
        try:
            return str((resp.json() or {}).get("message") or "")[:300]
        except ValueError:
            return (resp.text or "")[:300]

    def _interpret(self, resp: requests.Response):
        if 200 <= resp.status_code < 300:
            try:
                return resp.json(), None
            except ValueError as exc:
                return None, f"invalid_json: {exc}"
        message = self._error_message(resp)
        lowered = message.lower()
        if resp.status_code == 401:
            return None, f"authentication_error: {message or 'bad credentials'}"
        if resp.status_code in (403, 429):
            if "rate limit" in lowered:
                return None, f"rate_limited: {message}"
            if "disabled" in lowered:
                return None, f"disabled: {message}"
            return None, f"forbidden: {message or 'insufficient permissions'}"
        if resp.status_code == 404:
            return None, f"not_found: {message or 'not found'}"
        if resp.status_code >= 500:
            return None, f"server_error: HTTP {resp.status_code}"
        return None, f"http_{resp.status_code}: {message}"

    def get_json(self, url: str, params: Optional[dict] = None):
        try:
            return self._interpret(self._request(url, params))
        except GitHubAPIError as exc:
            return None, str(exc)

    def get_paginated(self, url: str, params: Optional[dict] = None, max_pages: int = 200) -> Tuple[List[dict], Optional[str]]:
        items: List[dict] = []
        next_url: Optional[str] = url
        next_params: Optional[dict] = dict(params or {}, per_page=100)
        pages = 0
        while next_url and pages < max_pages:
            pages += 1
            try:
                resp = self._request(next_url, next_params)
            except GitHubAPIError as exc:
                return items, str(exc)
            next_params = None  # the "next" link already carries the query string
            data, error = self._interpret(resp)
            if error:
                return items, error
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict) and isinstance(data.get("items"), list):
                items.extend(data["items"])
            else:
                return items, f"unexpected_response: {next_url}"
            next_url = resp.links.get("next", {}).get("url")
        return items, None

    # ---- endpoints ----
    def get_authenticated_user(self):
        return self.get_json("/user")

    def get_rate_limit(self) -> dict:
        data, _ = self.get_json("/rate_limit")
        return ((data or {}).get("resources") or {}).get("core") or {}

    def list_org_repos(self, org: str):
        return self.get_paginated(f"/orgs/{org}/repos", params={"type": "all", "sort": "full_name"})

    def get_repo(self, owner: str, repo: str):
        return self.get_json(f"/repos/{owner}/{repo}")

    def get_dependabot_alerts(self, owner: str, repo: str):
        return self.get_paginated(f"/repos/{owner}/{repo}/dependabot/alerts", params={"state": "open"})

    def get_code_scanning_alerts(self, owner: str, repo: str):
        return self.get_paginated(f"/repos/{owner}/{repo}/code-scanning/alerts", params={"state": "open"})

    def list_root_files(self, owner: str, repo: str, ref: str) -> Tuple[Optional[List[str]], Optional[str]]:
        data, error = self.get_json(f"/repos/{owner}/{repo}/contents", params={"ref": ref})
        if error:
            return None, error
        if not isinstance(data, list):
            return None, "unexpected_response: repository root listing"
        return [item.get("name", "") for item in data if item.get("type") == "file"], None

    def get_file_contents(self, owner: str, repo: str, path: str, ref: str) -> Tuple[Optional[str], Optional[str]]:
        data, error = self.get_json(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
        if error:
            return None, error
        if not isinstance(data, dict) or "content" not in data:
            return None, "not_found: no file content"
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace"), None
        except (ValueError, TypeError) as exc:
            return None, f"decode_error: {exc}"

    def list_open_pulls(self, owner: str, repo: str):
        return self.get_paginated(f"/repos/{owner}/{repo}/pulls", params={"state": "open"})

    def list_open_issues(self, owner: str, repo: str):
        items, error = self.get_paginated(f"/repos/{owner}/{repo}/issues", params={"state": "open"})
        return [i for i in items if "pull_request" not in i], error  # /issues also returns PRs


# ============================================================================
# PyPI + CISA KEV clients
# ============================================================================

class PyPIClient:
    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._cache: Dict[str, PyPIInfo] = {}
        self._lock = threading.Lock()
        self.session = requests.Session()
        retries = Retry(total=4, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504],
                        allowed_methods=["GET"], raise_on_status=False)
        self.session.mount("https://", HTTPAdapter(max_retries=retries, pool_maxsize=32))
        self.session.headers.update({"User-Agent": f"singer-tap-security-auditor/{__version__}"})

    def get_latest_version(self, package_name: str) -> PyPIInfo:
        key = normalize_package(package_name)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = self._fetch(key)
        with self._lock:
            self._cache[key] = result
        return result

    def _fetch(self, package_name: str) -> PyPIInfo:
        try:
            resp = self.session.get(PYPI_API.format(name=package_name), timeout=self.timeout)
        except requests.RequestException as exc:
            return PyPIInfo("error", None, f"PyPI request failed: {exc}")
        if resp.status_code == 404:
            return PyPIInfo("not_found", None, "Not found on PyPI")
        if resp.status_code != 200:
            return PyPIInfo("error", None, f"PyPI returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            return PyPIInfo("error", None, f"Invalid JSON from PyPI: {exc}")
        latest = self.select_latest_version(data)
        return PyPIInfo("ok", latest) if latest else PyPIInfo("error", None, "No usable releases on PyPI")

    @staticmethod
    def select_latest_version(data: dict) -> Optional[str]:
        candidates = []
        for version_str, files in (data.get("releases") or {}).items():
            if not files or all(f.get("yanked") for f in files):
                continue
            parsed = parse_version(version_str)
            if parsed is not None:
                candidates.append(parsed)
        stable = [v for v in candidates if not v.is_prerelease and not v.is_devrelease]
        pool = stable or candidates
        if pool:
            return str(max(pool))
        return (data.get("info") or {}).get("version") or None


class ThreatIntelClient:
    """CISA KEV lookup. EPSS is already embedded in GitHub advisory payloads."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self._kev_ids: Optional[set] = None
        self.status = "Not loaded"

    def load(self) -> None:
        try:
            resp = requests.get(KEV_FEED, timeout=self.timeout,
                                headers={"User-Agent": f"singer-tap-security-auditor/{__version__}"})
            resp.raise_for_status()
            vulns = resp.json().get("vulnerabilities") or []
            self._kev_ids = {v.get("cveID") for v in vulns if v.get("cveID")}
            self.status = f"Loaded ({len(self._kev_ids)} CVEs)"
        except (requests.RequestException, ValueError) as exc:
            self.status = f"Unavailable ({exc.__class__.__name__}) - 'Known Exploited' reported as Unknown"
            LOGGER.warning("Could not load CISA KEV feed: %s", exc)

    def is_known_exploited(self, cve_id: str) -> str:
        if not cve_id or self._kev_ids is None:
            return "Unknown"
        return "Yes" if cve_id in self._kev_ids else "No"


# ============================================================================
# Repository discovery + tap detection
# ============================================================================

class TapDetector:
    """A repository is a tap if it follows the ``tap-*`` naming convention or
    carries a ``singer-tap`` topic. Everything else is still recorded in the
    Analysis Log so nothing is silently dropped."""

    @staticmethod
    def classify(repo: RepoInfo, explicit: bool = False) -> RepoInfo:
        if explicit:
            repo.is_tap, repo.tap_detection_reason = True, "explicitly requested via --tap"
        elif repo.name.startswith("tap-"):
            repo.is_tap, repo.tap_detection_reason = True, "name starts with 'tap-'"
        elif "singer-tap" in (repo.topics or []):
            repo.is_tap, repo.tap_detection_reason = True, "tagged with 'singer-tap' topic"
        else:
            repo.is_tap, repo.tap_detection_reason = False, "name/topic does not match tap convention"
        return repo


# ============================================================================
# Static dependency manifest parsing (never executes tap code)
# ============================================================================

class SetupPyParseError(Exception):
    pass


def _resolve_name(node: ast.AST, symtable: Dict[str, ast.AST], seen: Optional[set] = None) -> ast.AST:
    seen = seen if seen is not None else set()
    if isinstance(node, ast.Name):
        if node.id in seen:
            raise SetupPyParseError(f"circular reference resolving '{node.id}'")
        if node.id not in symtable:
            raise SetupPyParseError(f"could not resolve variable '{node.id}'")
        seen.add(node.id)
        return _resolve_name(symtable[node.id], symtable, seen)
    return node


def _extract_string_list(node: ast.AST, symtable: Dict[str, ast.AST]) -> List[str]:
    node = _resolve_name(node, symtable)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = []
        for element in node.elts:
            try:
                resolved = _resolve_name(element, symtable)
            except SetupPyParseError:
                continue
            if isinstance(resolved, ast.Constant) and isinstance(resolved.value, str):
                items.append(resolved.value)
        return items
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _extract_string_list(node.left, symtable) + _extract_string_list(node.right, symtable)
    raise SetupPyParseError(f"unsupported dependency list expression: {ast.dump(node)[:80]}")


def _extract_extras_dict(node: ast.AST, symtable: Dict[str, ast.AST]) -> Dict[str, List[str]]:
    node = _resolve_name(node, symtable)
    if not isinstance(node, ast.Dict):
        raise SetupPyParseError("extras_require is not a dict literal")
    result: Dict[str, List[str]] = {}
    for key_node, value_node in zip(node.keys, node.values):
        if key_node is None or value_node is None:
            continue
        try:
            resolved_key = _resolve_name(key_node, symtable)
            if isinstance(resolved_key, ast.Constant) and isinstance(resolved_key.value, str):
                result[resolved_key.value] = _extract_string_list(value_node, symtable)
        except SetupPyParseError:
            continue
    return result


def _find_setup_call(tree: ast.AST) -> Optional[ast.Call]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "setup") or (isinstance(func, ast.Attribute) and func.attr == "setup"):
                return node
    return None


def _keyword(call: ast.Call, name: str) -> Optional[ast.AST]:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def extras_scope(extra_name: str) -> str:
    return DEVELOPMENT if extra_name.strip().lower() in DEV_EXTRA_NAMES else RUNTIME


def build_declaration(tap_name: str, scope: str, manifest: str, raw: str, note: str = "") -> Optional[DependencyDeclaration]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        requirement = Requirement(raw)
    except InvalidRequirement as exc:
        match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", raw)
        if not match:
            return None
        return DependencyDeclaration(tap_name, match.group(1), scope, manifest, raw=raw,
                                     note=join_unique([note, f"Unparsable requirement: {exc}"], "; "))
    pinned, lower = "", None
    for spec in requirement.specifier:
        version = parse_version(spec.version.replace(".*", ""))
        if spec.operator in ("==", "===") and "*" not in spec.version:
            pinned = spec.version
        elif spec.operator in (">=", "~=", ">") and version is not None and (lower is None or version > lower):
            lower = version
    if requirement.url:
        note = join_unique([note, f"Installed from URL: {requirement.url}"], "; ")
    return DependencyDeclaration(
        tap_name=tap_name, package=requirement.name, scope=scope, manifest=manifest,
        specifier=str(requirement.specifier), pinned_version=pinned,
        baseline_version=pinned or (str(lower) if lower is not None else ""), raw=raw, note=note,
    )


def parse_setup_py_text(source: str, tap_name: str) -> List[DependencyDeclaration]:
    source = source.lstrip("\ufeff")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SetupPyParseError(f"setup.py has a syntax error: {exc}") from exc
    symtable: Dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symtable[target.id] = node.value
    setup_call = _find_setup_call(tree)
    if setup_call is None:
        raise SetupPyParseError("no setup(...) call found")

    declarations: List[DependencyDeclaration] = []

    def add_list(keyword: str, scope: str, note: str = "") -> None:
        node = _keyword(setup_call, keyword)
        if node is None:
            return
        try:
            for raw in _extract_string_list(node, symtable):
                decl = build_declaration(tap_name, scope, "setup.py", raw, note)
                if decl:
                    declarations.append(decl)
        except SetupPyParseError as exc:
            LOGGER.warning("%s: could not fully parse setup.py %s: %s", tap_name, keyword, exc)

    add_list("install_requires", RUNTIME)
    add_list("tests_require", DEVELOPMENT, "tests_require")
    add_list("setup_requires", DEVELOPMENT, "setup_requires (build-time)")
    extras_node = _keyword(setup_call, "extras_require")
    if extras_node is not None:
        try:
            for extra, entries in _extract_extras_dict(extras_node, symtable).items():
                for raw in entries:
                    decl = build_declaration(tap_name, extras_scope(extra), "setup.py", raw, f"extras_require[{extra}]")
                    if decl:
                        declarations.append(decl)
        except SetupPyParseError as exc:
            LOGGER.warning("%s: could not fully parse extras_require: %s", tap_name, exc)
    return declarations


def parse_setup_cfg_text(text: str, tap_name: str) -> List[DependencyDeclaration]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    declarations = []

    def lines(value: str) -> List[str]:
        return [ln.split("#", 1)[0].strip() for ln in value.splitlines() if ln.split("#", 1)[0].strip()]

    if parser.has_option("options", "install_requires"):
        for raw in lines(parser.get("options", "install_requires")):
            decl = build_declaration(tap_name, RUNTIME, "setup.cfg", raw)
            if decl:
                declarations.append(decl)
    if parser.has_section("options.extras_require"):
        for extra, value in parser.items("options.extras_require"):
            for raw in lines(value):
                decl = build_declaration(tap_name, extras_scope(extra), "setup.cfg", raw, f"extras_require[{extra}]")
                if decl:
                    declarations.append(decl)
    return declarations


def parse_requirements_text(text: str, tap_name: str, manifest: str) -> List[DependencyDeclaration]:
    lowered = manifest.lower()
    scope = DEVELOPMENT if ("dev" in lowered or "test" in lowered) else RUNTIME
    declarations = []
    for line in text.splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-", "git+", "http://", "https://")):
            continue
        decl = build_declaration(tap_name, scope, manifest, line)
        if decl:
            declarations.append(decl)
    return declarations


def _poetry_to_pep440(spec: str) -> str:
    spec = (spec or "").strip()
    if not spec or spec == "*":
        return ""
    if spec[0] in "^~" and spec[1:2] != "=":
        return f">={spec[1:]}"
    if spec[0].isdigit():
        return f"=={spec}"
    return spec


def parse_pyproject_text(text: str, tap_name: str) -> List[DependencyDeclaration]:
    if tomllib is None:
        raise ValueError("tomllib/tomli not available to parse pyproject.toml")
    data = tomllib.loads(text)
    declarations = []

    def add(raw: str, scope: str, note: str = "") -> None:
        decl = build_declaration(tap_name, scope, "pyproject.toml", raw, note)
        if decl:
            declarations.append(decl)

    project = data.get("project") or {}
    for raw in project.get("dependencies") or []:
        add(raw, RUNTIME)
    for extra, entries in (project.get("optional-dependencies") or {}).items():
        for raw in entries or []:
            add(raw, extras_scope(extra), f"optional-dependencies[{extra}]")

    poetry = (data.get("tool") or {}).get("poetry") or {}
    groups = [("dependencies", RUNTIME, poetry.get("dependencies") or {}),
              ("dev-dependencies", DEVELOPMENT, poetry.get("dev-dependencies") or {})]
    for group_name, group in (poetry.get("group") or {}).items():
        groups.append((f"group.{group_name}", extras_scope(group_name), (group or {}).get("dependencies") or {}))
    for label, scope, deps in groups:
        for name, spec in deps.items():
            if name.lower() == "python":
                continue
            version = spec if isinstance(spec, str) else (spec.get("version", "") if isinstance(spec, dict) else "")
            add(f"{name}{_poetry_to_pep440(version)}", scope, f"poetry {label}")
    return declarations


def parse_pipfile_text(text: str, tap_name: str) -> List[DependencyDeclaration]:
    if tomllib is None:
        raise ValueError("tomllib/tomli not available to parse Pipfile")
    data = tomllib.loads(text)
    declarations = []
    for section, scope in (("packages", RUNTIME), ("dev-packages", DEVELOPMENT)):
        for name, spec in (data.get(section) or {}).items():
            version = spec if isinstance(spec, str) else ((spec or {}).get("version", "") if isinstance(spec, dict) else "")
            version = "" if version in ("*", None) else version
            decl = build_declaration(tap_name, scope, "Pipfile", f"{name}{version}")
            if decl:
                declarations.append(decl)
    return declarations


def parse_lockfile_text(name: str, text: str) -> Dict[str, str]:
    """Returns {normalized package: resolved version} from poetry.lock / uv.lock / Pipfile.lock."""
    locked: Dict[str, str] = {}
    if name == "Pipfile.lock":
        data = json.loads(text)
        for section in ("default", "develop"):
            for pkg, info in (data.get(section) or {}).items():
                version = str((info or {}).get("version") or "").lstrip("=")
                if version:
                    locked.setdefault(normalize_package(pkg), version)
        return locked
    if tomllib is None:
        raise ValueError(f"tomllib/tomli not available to parse {name}")
    for package in tomllib.loads(text).get("package") or []:
        if package.get("name") and package.get("version"):
            locked.setdefault(normalize_package(package["name"]), str(package["version"]))
    return locked


MANIFEST_PARSERS: Dict[str, Callable[[str, str], List[DependencyDeclaration]]] = {
    "setup.py": parse_setup_py_text,
    "setup.cfg": parse_setup_cfg_text,
    "pyproject.toml": parse_pyproject_text,
    "Pipfile": parse_pipfile_text,
}


class DependencyAnalyzer:
    def __init__(self, client: GitHubClient, org: str, local_repos_dir: Optional[Path] = None):
        self.client = client
        self.org = org
        self.local_repos_dir = local_repos_dir

    def analyze(self, repo: RepoInfo) -> Tuple[List[DependencyDeclaration], Dict[str, str], List[str], str]:
        """Returns (declarations, locked_versions, files_parsed, error)."""
        local_dir = self.local_repos_dir / repo.name if self.local_repos_dir else None
        if local_dir is not None and local_dir.is_dir():
            root_files = [p.name for p in local_dir.iterdir() if p.is_file()]
        else:
            local_dir = None
            root_files, error = self.client.list_root_files(self.org, repo.name, repo.default_branch)
            if error:
                return [], {}, [], f"manifest_listing_failed: {error}"
        present = [m for m in MANIFEST_CANDIDATES if m in (root_files or [])]
        lockfiles = [m for m in LOCKFILE_CANDIDATES if m in (root_files or [])]
        if not present and not lockfiles:
            return [], {}, [], "no_manifest_found: no setup.py/setup.cfg/pyproject.toml/Pipfile/requirements*.txt in repo root"

        declarations: List[DependencyDeclaration] = []
        locked: Dict[str, str] = {}
        parsed, errors = [], []
        for name in present + lockfiles:
            text, error = self._read(repo, name, local_dir)
            if error or text is None:
                errors.append(f"{name}: {error}")
                continue
            try:
                if name in LOCKFILE_CANDIDATES:
                    for pkg, version in parse_lockfile_text(name, text).items():
                        locked.setdefault(pkg, version)
                elif name in MANIFEST_PARSERS:
                    declarations.extend(MANIFEST_PARSERS[name](text, repo.name))
                else:
                    declarations.extend(parse_requirements_text(text, repo.name, name))
                parsed.append(name + (" (local clone)" if local_dir is not None else ""))
            except Exception as exc:  # a malformed manifest must never abort the tap
                errors.append(f"{name}: parse_error: {exc}")
        return declarations, locked, parsed, "; ".join(errors)

    def _read(self, repo: RepoInfo, name: str, local_dir: Optional[Path]) -> Tuple[Optional[str], Optional[str]]:
        if local_dir is None:
            return self.client.get_file_contents(self.org, repo.name, name, repo.default_branch)
        try:
            return (local_dir / name).read_text(encoding="utf-8", errors="replace"), None
        except OSError as exc:
            return None, f"read_error: {exc}"


def index_declarations(declarations: Sequence[DependencyDeclaration]) -> Dict[str, List[DependencyDeclaration]]:
    index: Dict[str, List[DependencyDeclaration]] = defaultdict(list)
    for decl in declarations:
        index[decl.key].append(decl)
    return index


# ============================================================================
# VulnerabilityAnalyzer - turns raw alerts into analyzed, prioritized findings
# ============================================================================

class VulnerabilityAnalyzer:
    def __init__(self, threat_intel: ThreatIntelClient, now: Optional[dt.datetime] = None):
        self.threat_intel = threat_intel
        self.now = now or utc_now()

    # ---- Dependabot ----
    def analyze_dependabot_alert(self, repo: RepoInfo, alert: dict, deps_index: Dict[str, List[DependencyDeclaration]],
                                 dependency_error: str, activity: RepoActivity,
                                 locked_versions: Optional[Dict[str, str]] = None) -> Vulnerability:
        advisory = alert.get("security_advisory") or {}
        sec_vuln = alert.get("security_vulnerability") or {}
        dependency = alert.get("dependency") or {}
        package_info = sec_vuln.get("package") or dependency.get("package") or {}
        package = package_info.get("name") or "unknown"
        ecosystem = package_info.get("ecosystem") or ""
        ghsa = advisory.get("ghsa_id") or ""
        cve = advisory.get("cve_id") or next(
            (i.get("value") for i in advisory.get("identifiers") or [] if i.get("type") == "CVE"), "") or ""
        severity = SEVERITY_NORMALIZE.get((sec_vuln.get("severity") or advisory.get("severity") or "").lower(), "Informational")
        cvss_score, cvss_vector = self.extract_cvss(advisory)
        epss_pct, epss_percentile = self.extract_epss(advisory)
        vulnerable_range = sec_vuln.get("vulnerable_version_range") or ""
        fixed = (sec_vuln.get("first_patched_version") or {}).get("identifier") or ""
        manifest = dependency.get("manifest_path") or ""

        declaration, locked = None, ""
        if ecosystem == "pip":
            declaration = self.pick_declaration(deps_index.get(normalize_package(package), []), manifest)
            locked = (locked_versions or {}).get(normalize_package(package), "")
        scope = self.dependency_scope(declaration, (dependency.get("scope") or "").lower())
        relationship = self.relationship(declaration, (dependency.get("relationship") or "").lower(), bool(dependency_error))
        current_version, current_vulnerable = self.current_version(declaration, vulnerable_range, locked)
        fix_allowed = self.fix_allowed(declaration, fixed)
        breaking = self.breaking_change_risk(declaration, fixed, locked)
        code_changes = self.code_changes_required(package, fixed, relationship, fix_allowed, breaking)

        issue_urls, pr_urls, dependabot_pr = self.find_existing_remediation(activity, package, [ghsa, cve])
        known_exploited = self.threat_intel.is_known_exploited(cve)
        priority = self.recommended_priority(severity, scope, known_exploited, epss_pct)
        action = self.recommended_action(package, fixed, scope, relationship, declaration, manifest, pr_urls,
                                         f"https://github.com/advisories/{ghsa}" if ghsa else "", bool(locked))
        state = alert.get("state") or "open"
        jira_candidate, jira_reason = self.jira_candidacy(state, severity, priority, bool(fixed), pr_urls, issue_urls)
        created = parse_github_datetime(alert.get("created_at"))
        refs = [r.get("url") for r in advisory.get("references") or [] if r.get("url")]

        return Vulnerability(
            tap_name=repo.name, repository=repo.full_name, repository_url=repo.html_url,
            security_url=repo.security_url, tool=TOOL_DEPENDABOT, alert_number=alert.get("number"),
            alert_url=alert.get("html_url") or "", alert_state=state,
            vulnerability_id=cve or ghsa or f"dependabot-{alert.get('number')}", ghsa=ghsa, cve=cve,
            ghsa_url=f"https://github.com/advisories/{ghsa}" if ghsa else "",
            cve_url=f"https://nvd.nist.gov/vuln/detail/{cve}" if cve else "",
            ecosystem=ecosystem, package=package, dependency_scope=scope, relationship=relationship,
            manifest=manifest or (declaration.manifest if declaration else ""),
            declared_specifier=(declaration.raw if declaration else "Not declared"),
            current_version=current_version, current_version_vulnerable=current_vulnerable,
            vulnerable_version_range=vulnerable_range, fixed_version=fixed or "None published",
            fix_available="Yes" if fixed else "No", fix_allowed_by_current_spec=fix_allowed,
            severity=severity, cvss_score=cvss_score, cvss_vector=cvss_vector,
            cwe=", ".join(c.get("cwe_id", "") for c in advisory.get("cwes") or [] if c.get("cwe_id")),
            epss_percentage=epss_pct, epss_percentile=epss_percentile, known_exploited=known_exploited,
            exploitability=self.exploitability_text(known_exploited, epss_pct, epss_percentile),
            summary=advisory.get("summary") or "", description=advisory.get("description") or "",
            technical_impact=self.technical_impact(scope, relationship),
            recommended_action=action, upgrade_required="Yes" if fixed else "No fix published",
            code_changes_required=code_changes, breaking_change_risk=breaking,
            existing_issue_urls="\n".join(issue_urls), existing_pr_urls="\n".join(pr_urls),
            dependabot_pr="Yes" if dependabot_pr else "No",
            alert_created=created, alert_age_days=(self.now - created).days if created else None,
            advisory_published=parse_github_datetime(advisory.get("published_at")),
            recommended_priority=priority, jira_candidate=jira_candidate, jira_candidate_reason=jira_reason,
            references="\n".join(refs[:15]),
        )

    # ---- Code scanning ----
    def analyze_code_scanning_alert(self, repo: RepoInfo, alert: dict, activity: RepoActivity) -> Vulnerability:
        rule = alert.get("rule") or {}
        instance = alert.get("most_recent_instance") or {}
        location = instance.get("location") or {}
        tool_name = (alert.get("tool") or {}).get("name") or TOOL_CODEQL
        rule_id = rule.get("id") or rule.get("name") or "unknown-rule"
        severity = SEVERITY_NORMALIZE.get(
            (rule.get("security_severity_level") or rule.get("severity") or "").lower(), "Informational")
        cwes = sorted({f"CWE-{int(m)}" for tag in rule.get("tags") or [] for m in re.findall(r"cwe-(\d+)", tag, re.I)})
        language = ""
        try:
            language = json.loads(instance.get("environment") or "{}").get("language", "")
        except (ValueError, AttributeError):
            pass
        path = location.get("path") or ""
        where = f"{path}:{location.get('start_line')}" if path and location.get("start_line") else path
        issue_urls, pr_urls, _ = self.find_existing_remediation(activity, None, [rule_id])
        priority = self.recommended_priority(severity, RUNTIME, "Unknown", None)
        state = alert.get("state") or "open"
        if state != "open":
            jira = ("No", f"Alert state is '{state}'")
        elif severity in ("Critical", "High", "Medium"):
            jira = ("Yes", f"Open {severity.lower()} severity code-scanning finding")
        else:
            jira = ("No", "Low-severity code-scanning note - batch into routine maintenance")
        created = parse_github_datetime(alert.get("created_at"))
        description = rule.get("full_description") or rule.get("description") or ""
        action = (f"Fix {tool_name} finding `{rule_id}` at {where or 'the reported location'}: {description}"
                  if not pr_urls else f"Review and merge open PR(s) addressing `{rule_id}`: {pr_urls[0]}")
        return Vulnerability(
            tap_name=repo.name, repository=repo.full_name, repository_url=repo.html_url,
            security_url=repo.security_url, tool=tool_name, alert_number=alert.get("number"),
            alert_url=alert.get("html_url") or "", alert_state=state, vulnerability_id=f"{tool_name}:{rule_id}",
            ecosystem=f"code ({language})" if language else "code", rule_id=rule_id, location=where,
            dependency_scope="N/A", relationship="N/A", manifest=path, declared_specifier="N/A",
            current_version="N/A", current_version_vulnerable="N/A", fixed_version="N/A (code change)",
            fix_available="N/A", fix_allowed_by_current_spec="N/A", severity=severity, cwe=", ".join(cwes),
            known_exploited="N/A", exploitability="Static analysis finding - exploitability depends on context",
            summary=rule.get("description") or "", description=description,
            technical_impact="Finding in the tap's own source code or CI workflow configuration",
            recommended_action=action, upgrade_required="No (code/config change)",
            code_changes_required="Yes - source or workflow change required", breaking_change_risk="Low (localized change)",
            existing_issue_urls="\n".join(issue_urls), existing_pr_urls="\n".join(pr_urls),
            alert_created=created, alert_age_days=(self.now - created).days if created else None,
            recommended_priority=priority, jira_candidate=jira[0], jira_candidate_reason=jira[1],
            references=alert.get("html_url") or "",
        )

    # ---- helpers (static so they can be unit tested in isolation) ----
    @staticmethod
    def extract_cvss(advisory: dict) -> Tuple[Optional[float], str]:
        # Unrated CVSS axes come back as score 0.0 with a null vector, so only trust entries with a vector.
        severities = advisory.get("cvss_severities") or {}
        for key in ("cvss_v4", "cvss_v3"):
            entry = severities.get(key) or {}
            if entry.get("score") is not None and entry.get("vector_string"):
                return float(entry["score"]), entry["vector_string"]
        legacy = advisory.get("cvss") or {}
        if legacy.get("score") is not None and legacy.get("vector_string"):
            return float(legacy["score"]), legacy["vector_string"]
        return None, ""

    @staticmethod
    def extract_epss(advisory: dict) -> Tuple[Optional[float], Optional[float]]:
        epss = advisory.get("epss") or {}
        if isinstance(epss, list):
            epss = epss[0] if epss else {}
        try:
            pct = float(epss["percentage"]) if epss.get("percentage") is not None else None
            percentile = float(epss["percentile"]) if epss.get("percentile") is not None else None
        except (TypeError, ValueError):
            return None, None
        return pct, percentile

    @staticmethod
    def pick_declaration(candidates: List[DependencyDeclaration], manifest_path: str) -> Optional[DependencyDeclaration]:
        if not candidates:
            return None
        manifest_name = os.path.basename(manifest_path or "")
        return sorted(candidates, key=lambda d: (d.scope != RUNTIME, d.manifest != manifest_name))[0]

    @staticmethod
    def dependency_scope(declaration: Optional[DependencyDeclaration], github_scope: str) -> str:
        if declaration is not None:
            return declaration.scope
        return DEVELOPMENT if github_scope == "development" else RUNTIME

    @staticmethod
    def relationship(declaration: Optional[DependencyDeclaration], github_relationship: str, manifests_unreadable: bool) -> str:
        if declaration is not None or github_relationship == "direct":
            return DIRECT
        if github_relationship == "transitive":
            return TRANSITIVE
        return UNKNOWN if manifests_unreadable else TRANSITIVE

    @staticmethod
    def current_version(declaration: Optional[DependencyDeclaration], vulnerable_range: str,
                        locked: str = "") -> Tuple[str, str]:
        if locked:
            hit = version_in_range(locked, vulnerable_range)
            return f"{locked} (lock file)", {True: "Yes", False: "No"}.get(hit, "Unknown")
        if declaration is None:
            return "Not declared (resolved at install time)", "Unknown - transitive, resolved at install time"
        if declaration.pinned_version:
            hit = version_in_range(declaration.pinned_version, vulnerable_range)
            return declaration.pinned_version, {True: "Yes", False: "No"}.get(hit, "Unknown")
        if declaration.baseline_version:
            hit = version_in_range(declaration.baseline_version, vulnerable_range)
            label = f"Unpinned (minimum {declaration.baseline_version})"
            if hit is True:
                return label, "Possibly - minimum allowed version is vulnerable"
            if hit is False:
                return label, "No - minimum allowed version is patched"
            return label, "Unknown (unpinned)"
        return "Unpinned (no constraint)", "Unknown (unpinned)"

    @staticmethod
    def fix_allowed(declaration: Optional[DependencyDeclaration], fixed: str) -> str:
        if not fixed:
            return "N/A (no fix published)"
        if declaration is None:
            return "Unknown (transitive)"
        if not declaration.specifier:
            return "Yes (unconstrained)"
        fixed_version = parse_version(fixed)
        try:
            spec = SpecifierSet(declaration.specifier)
        except InvalidSpecifier:
            return "Unknown (unparsable constraint)"
        if fixed_version is None:
            return "Unknown (unparsable fixed version)"
        return "Yes" if spec.contains(fixed_version, prereleases=True) else "No - constraint must be changed"

    @staticmethod
    def breaking_change_risk(declaration: Optional[DependencyDeclaration], fixed: str, locked: str = "") -> str:
        if not fixed:
            return "N/A (no fix published)"
        if declaration is None and not locked:
            return "Unknown (transitive - depends on parent package)"
        base = parse_version(locked) or parse_version(declaration.baseline_version if declaration else "")
        target = parse_version(fixed)
        if base is None or target is None:
            return "Unknown (no declared version baseline)"
        if base >= target:
            return "None (current/minimum version already >= fixed version)"
        if base.major >= 2000:  # calendar-versioned (e.g. certifi 2024.7.4): a "major" bump is just a new release
            return f"Low (calendar-versioned release {base} -> {target})"
        if target.major > base.major:
            return f"High (major version bump {base} -> {target})"
        if target.minor > base.minor:
            return f"Medium (minor version bump {base} -> {target})"
        return f"Low (patch version bump {base} -> {target})"

    @staticmethod
    def code_changes_required(package: str, fixed: str, relationship: str, fix_allowed: str, breaking: str) -> str:
        if not fixed:
            return "N/A - no fix published; mitigation may require code changes"
        risk = breaking.split(" ", 1)[0]
        tail = {"High": "major version bump - review changelog, tap code changes likely",
                "Medium": "minor version bump - review changelog, code changes possible",
                "Low": "patch version bump - code changes unlikely"}.get(risk, "review changelog")
        if relationship != DIRECT:
            if risk in ("High", "Medium", "Low"):
                return f"Lock/manifest-only expected: relock or pin `{package}>={fixed}`; {tail} (only if the tap imports it directly)"
            return (f"Manifest-only expected: upgrade the parent package or add an explicit `{package}>={fixed}` "
                    f"constraint; tap code changes only if the parent package needs a major upgrade")
        if breaking.startswith("None"):
            return "Manifest-only: constraint already permits the fix - reinstall/relock and optionally raise the floor"
        if fix_allowed.startswith("Yes"):
            return f"Manifest-only: raise the minimum to >= {fixed}; {tail}"
        return f"Manifest change required (current constraint excludes the fix); {tail}"

    @staticmethod
    def technical_impact(scope: str, relationship: str) -> str:
        if scope == DEVELOPMENT:
            return "Development/test-only dependency: not installed with the tap at runtime; risk limited to CI/developer environments"
        if relationship == DIRECT:
            return "Runtime dependency declared by the tap: vulnerable code ships with and executes inside the tap process"
        return ("Runtime transitive dependency installed via another package: present in the tap's runtime environment; "
                "exploitability depends on whether the tap exercises the vulnerable code path")

    @staticmethod
    def find_existing_remediation(activity: RepoActivity, package: Optional[str],
                                  identifiers: Sequence[str]) -> Tuple[List[str], List[str], bool]:
        """Matches OPEN issues/PRs that reference the advisory id, or whose
        title names the package together with a remediation keyword, or
        Dependabot branches for the package."""
        ids = [i.lower() for i in identifiers if i]
        package_re = re.compile(rf"(?<![\w.-]){re.escape(package)}(?![\w-])", re.I) if package else None
        # Dependabot branches look like dependabot/pip/<package>-<version>
        branch_re = re.compile(rf"^dependabot/[^/]+/(.*/)?{re.escape(normalize_package(package))}-\d") if package else None

        def matches(item: dict, is_pr: bool) -> bool:
            title = item.get("title") or ""
            text = f"{title}\n{(item.get('body') or '')[:20000]}".lower()
            if any(i in text for i in ids):
                return True
            if package_re is None:
                return False
            if package_re.search(title) and any(k in title.lower() for k in REMEDIATION_KEYWORDS):
                return True
            if is_pr and branch_re is not None:
                ref = re.sub(r"[-_.]+", "-", ((item.get("head") or {}).get("ref") or "").lower())
                if branch_re.search(ref):
                    return True
            return False

        issue_urls = [i.get("html_url", "") for i in activity.issues if matches(i, False)]
        matched_prs = [p for p in activity.prs if matches(p, True)]
        pr_urls = [p.get("html_url", "") for p in matched_prs]
        dependabot = any("dependabot" in ((p.get("user") or {}).get("login") or "").lower() for p in matched_prs)
        return issue_urls, pr_urls, dependabot

    @staticmethod
    def exploitability_text(known_exploited: str, epss_pct: Optional[float], epss_percentile: Optional[float]) -> str:
        if known_exploited == "Yes":
            return "Actively exploited (listed in CISA KEV catalog)"
        if epss_pct is None:
            return "No known exploitation; no EPSS score available"
        percentile = f", {epss_percentile * 100:.0f}th percentile" if epss_percentile is not None else ""
        level = "High" if epss_pct >= 0.10 else ("Moderate" if epss_pct >= 0.01 else "Low")
        return f"{level} exploit probability (EPSS {epss_pct * 100:.2f}% in next 30 days{percentile})"

    @staticmethod
    def recommended_priority(severity: str, scope: str, known_exploited: str, epss_pct: Optional[float]) -> str:
        rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}.get(severity, 4)
        if known_exploited == "Yes":
            rank = 0
        elif epss_pct is not None and epss_pct >= 0.10 and rank > 1:
            rank -= 1  # meaningful real-world exploit probability
        if scope == DEVELOPMENT:
            rank = min(rank + 1, 4)
        return PRIORITIES[rank]

    @staticmethod
    def recommended_action(package: str, fixed: str, scope: str, relationship: str,
                           declaration: Optional[DependencyDeclaration], manifest: str,
                           pr_urls: List[str], advisory_url: str, locked: bool = False) -> str:
        if pr_urls and fixed:
            action = (f"Review and merge the open PR that addresses `{package}` ({pr_urls[0]}); confirm the "
                      f"resolved version is >= {fixed}, run the test suites and release a new tap version.")
        elif not fixed:
            action = (f"No patched release of `{package}` is available yet. Monitor {advisory_url or 'the advisory'}, "
                      f"assess whether the tap uses the affected functionality, and consider mitigation or replacing the dependency.")
        elif relationship == DIRECT and declaration is not None:
            target = f"=={fixed}" if declaration.pinned_version else f">={fixed}"
            action = (f"Update `{package}` in {declaration.manifest} from `{declaration.raw}` to `{package}{target}`; "
                      f"reinstall, run unit/integration/tap-tester suites and release a new tap version.")
        elif locked:
            lock_name = os.path.basename(manifest) or "the lock file"
            relock = {"poetry.lock": f"`poetry update {package}`", "Pipfile.lock": f"`pipenv update {package}`",
                      "uv.lock": f"`uv lock --upgrade-package {package}`"}.get(lock_name, "a relock")
            action = (f"`{package}` is a transitive dependency locked in {lock_name}. Run {relock} so it resolves to "
                      f">= {fixed} (upgrade the parent package if its constraint blocks this), commit the lock file, "
                      f"run the test suites and release a new tap version.")
        else:
            action = (f"`{package}` is a transitive dependency (reported via {manifest or 'the dependency graph'}). Upgrade "
                      f"the parent package that pulls it in, or add an explicit `{package}>={fixed}` constraint to "
                      f"install_requires; reinstall, verify `pip show {package}` reports >= {fixed}, run the test suites "
                      f"and release a new tap version.")
        return f"[Dev-only] {action}" if scope == DEVELOPMENT else action

    @staticmethod
    def jira_candidacy(state: str, severity: str, priority: str, fix_available: bool,
                       pr_urls: List[str], issue_urls: List[str]) -> Tuple[str, str]:
        if state != "open":
            return "No", f"Alert state is '{state}'"
        if not fix_available:
            if severity in ("Critical", "High"):
                return "Yes", "No patched version published - track mitigation/replacement"
            return "No", "No patched version published - monitor the advisory"
        if priority == "P4 - Informational":
            return "No", "Low-risk development-only finding - batch into routine dependency maintenance"
        if pr_urls:
            return "Yes", "Open PR already proposes a fix - ticket to review, merge and release"
        if issue_urls:
            return "Yes", "Tracked in a GitHub issue but not fixed - schedule the fix in Jira"
        return "Yes", f"Open {severity.lower()} severity alert with a published fix"


# ============================================================================
# Jira candidate generation (one ticket per tap + package / tap + rule)
# ============================================================================

class JiraTicketGenerator:
    @staticmethod
    def generate(vulnerabilities: List[Vulnerability]) -> List[JiraTicket]:
        groups: Dict[Tuple[str, str, str], List[Vulnerability]] = defaultdict(list)
        for vuln in vulnerabilities:
            if vuln.jira_candidate == "Yes":
                groups[vuln.group_key].append(vuln)

        ordered = sorted(groups.values(), key=lambda vs: (
            PRIORITY_RANK[highest_priority(v.recommended_priority for v in vs)],
            SEVERITY_RANK.get(highest_severity(v.severity for v in vs), 9), vs[0].tap_name.lower(), vs[0].group_key[2]))
        tickets = []
        for index, vulns in enumerate(ordered, 1):
            ref = f"SEC-{index:03d}"
            for v in vulns:
                v.jira_ref = ref
            tickets.append(JiraTicketGenerator._build(ref, vulns))
        return tickets

    @staticmethod
    def _build(ref: str, vulns: List[Vulnerability]) -> JiraTicket:
        vulns = sorted(vulns, key=lambda v: (SEVERITY_RANK.get(v.severity, 9), -(v.cvss_score or 0)))
        first = vulns[0]
        tap = first.tap_name
        severity = highest_severity(v.severity for v in vulns)
        priority = highest_priority(v.recommended_priority for v in vulns)
        n = len(vulns)
        plural = "s" if n > 1 else ""
        pr_urls = join_unique(u for v in vulns for u in v.existing_pr_urls.splitlines())
        alert_urls = join_unique(v.alert_url for v in vulns)
        advisories = join_unique((" / ".join(x for x in (v.cve, v.ghsa) if x) or v.vulnerability_id) for v in vulns)
        scope = join_unique((v.dependency_scope for v in vulns), ", ")

        if first.tool == TOOL_DEPENDABOT:
            fixed_versions = [v.fixed_version for v in vulns if v.fix_available == "Yes"]
            target = max_version(fixed_versions)
            # remediation text must target the version that clears ALL grouped alerts
            anchor = next((v for v in vulns if target and v.fixed_version == target), first)
            if target:
                summary = f"[{tap}] Security: upgrade {first.package} to >= {target} ({n} {severity} Dependabot alert{plural})"
            else:
                summary = f"[{tap}] Security: mitigate {first.package} vulnerability - no fix published ({severity})"
            subject, labels = first.package, f"security, dependabot, {tap}"
            table = "||Alert||Advisory||Severity||CVSS||Vulnerable range||Fixed in||\n" + "\n".join(
                f"|[#{v.alert_number}|{v.alert_url}]|{v.cve or v.ghsa} ({v.ghsa})|{v.severity}|"
                f"{v.cvss_score if v.cvss_score is not None else 'n/a'}|{v.vulnerable_version_range or 'n/a'}|{v.fixed_version}|"
                for v in vulns)
            dependency_block = (
                f"h2. Vulnerable dependency\n*{first.package}* ({first.ecosystem}) - {first.dependency_scope} / {first.relationship}\n"
                f"* Manifest: {first.manifest or 'n/a'}\n* Declared constraint: {first.declared_specifier}\n"
                f"* Current version: {first.current_version}\n* Target version: {'>= ' + target if target else 'none published'}\n"
                f"* Fix allowed by current constraint: {anchor.fix_allowed_by_current_spec}\n\n")
            testing = ("Reinstall the tap in a clean virtualenv, run unit tests, integration tests and tap-tester, "
                       "and verify discovery + sync still succeed.")
            acceptance = (f"* `{first.package}` resolves to {'>= ' + target if target else 'a non-vulnerable version or is replaced'} "
                          f"in the tap's install\n* All listed Dependabot alerts are closed as fixed\n"
                          f"* Unit, integration and tap-tester suites pass\n* A new tap version is released with a CHANGELOG entry")
        else:
            target = "N/A"
            anchor = first
            summary = f"[{tap}] Security: fix {first.tool} finding {first.rule_id} ({n} {severity} alert{plural})"
            subject, labels = first.rule_id, f"security, code-scanning, {tap}"
            table = "||Alert||Rule||Severity||Location||\n" + "\n".join(
                f"|[#{v.alert_number}|{v.alert_url}]|{v.rule_id}|{v.severity}|{v.location or 'n/a'}|" for v in vulns)
            dependency_block = f"h2. Finding\n{first.summary}\n\n{first.description}\n\n"
            testing = "Re-run code scanning on the default branch and the tap's test suites."
            acceptance = ("* The code-scanning alerts listed above are closed as fixed on the default branch\n"
                          "* Tap test suites / CI pass")

        references = join_unique([first.security_url] + [v.ghsa_url for v in vulns] + [v.cve_url for v in vulns]
                                 + [r for v in vulns for r in v.references.splitlines()][:10])
        description = (
            f"h2. Problem\n{n} open security alert{plural} ({severity} severity, priority {priority}) reported by GitHub "
            f"{first.tool} for *{tap}* ([{first.repository}|{first.repository_url}]).\n\n"
            f"h2. Security impact\n{first.summary or 'See advisories below.'}\n{first.technical_impact}\n"
            f"Exploitability: {first.exploitability}\n\n"
            f"{dependency_block}h2. Alerts covered\n{table}\n\n"
            f"h2. Recommended remediation\n{anchor.recommended_action}\n\n"
            f"h2. Expected code changes\n{anchor.code_changes_required}\nBreaking-change risk: {anchor.breaking_change_risk}\n\n"
            + (f"h2. Existing remediation\n{pr_urls}\n\n" if pr_urls else "")
            + f"h2. Testing required\n{testing}\n\nh2. Acceptance criteria\n{acceptance}\n\nh2. References\n{references}"
        )
        return JiraTicket(
            ref=ref, tap=tap, issue_type="Task", summary=summary[:250], priority=priority,
            jira_priority=JIRA_PRIORITY.get(priority, "Medium"), severity=severity, labels=labels, tool=first.tool,
            package_or_rule=subject, dependency_scope=scope, current_version=first.current_version,
            target_version=f">= {target}" if target and target != "N/A" else target or "None published",
            alerts_covered=n, advisories=advisories, alert_urls=alert_urls, existing_prs=pr_urls,
            recommended_action=anchor.recommended_action, code_changes_required=anchor.code_changes_required,
            breaking_change_risk=anchor.breaking_change_risk, description=description,
            acceptance_criteria=acceptance, references=references,
        )


# ============================================================================
# Per-tap audit
# ============================================================================

class TapAuditResult:
    def __init__(self, repo: RepoInfo):
        self.repo = repo
        self.vulnerabilities: List[Vulnerability] = []
        self.declarations: List[DependencyDeclaration] = []
        self.locked_versions: Dict[str, str] = {}
        self.dependency_details: List[DependencyDetailRow] = []
        self.manifests: List[str] = []
        self.analysis_status = STATUS_FAILED
        self.status_detail = ""
        self.dependabot_status = "Not checked"
        self.code_scanning_status = "Not checked"
        self.dependency_status = "Not checked"
        self.scan_successful = False
        self.error = ""
        self.transient = False
        self.attempts = 0
        self.duration: Optional[float] = None
        self.timestamp = utc_now()


def describe_access_error(error: str) -> str:
    if error.startswith("disabled"):
        return "Disabled for repository"
    if error.startswith(("forbidden", "authentication_error")):
        return "No access (token lacks permission)"
    if error.startswith("not_found"):
        return "Not available (not found / not enabled)"
    return f"Error: {error}"


def next_step_for(result: TapAuditResult) -> str:
    error = result.error or ""
    if result.analysis_status == STATUS_ARCHIVED:
        return "Archived/disabled repo - re-run with --include-archived if it is still deployed"
    if "disabled" in result.dependabot_status.lower():
        return "Enable Dependabot alerts (Settings > Code security) and re-run with --tap " + result.repo.name
    if "forbidden" in error or "authentication_error" in error or "No access" in result.dependabot_status:
        return "Re-run with a token that has 'repo' + 'security_events' scope (or the org security manager role)"
    if "not_found" in error:
        return "Verify the repository name/exists and is visible to the token"
    if result.transient:
        return f"Transient error - re-run: python audit_singer_security.py --tap {result.repo.name}"
    return "Review the error in the Analysis Log and re-run for this tap"


class SingerSecurityAuditor:
    def __init__(self, org: str, github: GitHubClient, pypi: PyPIClient, threat_intel: ThreatIntelClient,
                 local_repos_dir: Optional[Path], include_archived: bool, include_code_scanning: bool):
        self.org = org
        self.github = github
        self.pypi = pypi
        self.dependency_analyzer = DependencyAnalyzer(github, org, local_repos_dir)
        self.vulnerability_analyzer = VulnerabilityAnalyzer(threat_intel)
        self.include_archived = include_archived
        self.include_code_scanning = include_code_scanning

    def audit_tap(self, repo: RepoInfo, attempts: int = 1) -> TapAuditResult:
        started = time.monotonic()
        result = TapAuditResult(repo)
        result.attempts = attempts
        try:
            self._audit(repo, result)
        except Exception as exc:  # a single tap must never abort the run
            LOGGER.exception("%s: unexpected failure during audit", repo.name)
            result.analysis_status, result.status_detail = STATUS_FAILED, "Unexpected exception"
            result.error = f"unexpected_error: {exc.__class__.__name__}: {exc}"
            result.scan_successful = False
        result.duration = round(time.monotonic() - started, 1)
        result.timestamp = utc_now()
        return result

    def _audit(self, repo: RepoInfo, result: TapAuditResult) -> None:
        if (repo.archived or repo.disabled) and not self.include_archived:
            result.analysis_status = STATUS_ARCHIVED
            result.status_detail = f"Repository is {repo.lifecycle.lower()}; deep scan skipped by design"
            result.dependabot_status = result.code_scanning_status = result.dependency_status = "Skipped"
            return

        errors: List[str] = []
        alerts, alerts_error = self.github.get_dependabot_alerts(self.org, repo.name)
        if alerts_error:
            result.dependabot_status = describe_access_error(alerts_error)
            errors.append(f"dependabot: {alerts_error}")
        else:
            result.dependabot_status = f"OK - {len(alerts)} open alert(s)"

        code_alerts: List[dict] = []
        if self.include_code_scanning:
            code_alerts, cs_error = self.github.get_code_scanning_alerts(self.org, repo.name)
            if cs_error:
                result.code_scanning_status = describe_access_error(cs_error)
                if not cs_error.startswith(("not_found", "disabled", "forbidden")):
                    errors.append(f"code_scanning: {cs_error}")  # "not enabled" is not an audit failure
                code_alerts = []
            else:
                result.code_scanning_status = f"OK - {len(code_alerts)} open alert(s)"
        else:
            result.code_scanning_status = "Skipped (--skip-code-scanning)"

        activity = RepoActivity()
        activity.prs, pr_error = self.github.list_open_pulls(self.org, repo.name)
        activity.issues, issue_error = self.github.list_open_issues(self.org, repo.name)
        activity.error = join_unique([f"open PRs: {pr_error}" if pr_error else "",
                                      f"open issues: {issue_error}" if issue_error else ""], "; ")
        if activity.error:
            errors.append(activity.error)

        declarations, locked, manifests, dep_error = self.dependency_analyzer.analyze(repo)
        result.declarations, result.locked_versions, result.manifests = declarations, locked, manifests
        lock_note = f"; {len(locked)} locked versions" if locked else ""
        if dep_error and not declarations and not locked:
            result.dependency_status = f"Unavailable: {dep_error}"
        elif dep_error:
            result.dependency_status = f"Partial ({', '.join(manifests)}): {dep_error}"
        else:
            result.dependency_status = f"OK - {len(declarations)} declared{lock_note} ({', '.join(manifests)})"
        if dep_error and not dep_error.startswith("no_manifest_found"):
            errors.append(f"dependencies: {dep_error}")

        deps_index = index_declarations(declarations)
        for alert in alerts:
            try:
                result.vulnerabilities.append(self.vulnerability_analyzer.analyze_dependabot_alert(
                    repo, alert, deps_index, dep_error if not declarations else "", activity, locked))
            except Exception:  # never let one malformed alert drop the others
                LOGGER.exception("%s: failed to analyze Dependabot alert #%s", repo.name, alert.get("number"))
                errors.append(f"dependabot alert #{alert.get('number')} could not be analyzed")
        for alert in code_alerts:
            try:
                result.vulnerabilities.append(self.vulnerability_analyzer.analyze_code_scanning_alert(repo, alert, activity))
            except Exception:
                LOGGER.exception("%s: failed to analyze code-scanning alert #%s", repo.name, alert.get("number"))
                errors.append(f"code-scanning alert #{alert.get('number')} could not be analyzed")

        result.dependency_details = self._dependency_details(repo, declarations, locked, result.vulnerabilities)
        result.error = "; ".join(errors)
        result.scan_successful = not alerts_error
        if alerts_error:
            result.transient = is_transient_error(alerts_error)
            result.analysis_status = STATUS_FAILED if result.transient else STATUS_MANUAL
            result.status_detail = f"Dependabot alerts unavailable: {result.dependabot_status}"
        elif result.vulnerabilities:
            result.analysis_status = STATUS_VULNERABLE
            result.status_detail = f"{len(result.vulnerabilities)} open alert(s)"
        else:
            result.analysis_status = STATUS_CLEAN
            result.status_detail = "No open Dependabot" + ("/code-scanning" if self.include_code_scanning else "") + " alerts"

    def _dependency_details(self, repo: RepoInfo, declarations: List[DependencyDeclaration], locked: Dict[str, str],
                            vulnerabilities: List[Vulnerability]) -> List[DependencyDetailRow]:
        alerts_by_pkg: Dict[str, List[Vulnerability]] = defaultdict(list)
        for v in vulnerabilities:
            if v.tool == TOOL_DEPENDABOT:
                alerts_by_pkg[normalize_package(v.package)].append(v)

        rows, seen = [], set()
        for decl in declarations:
            if (decl.key, decl.manifest, decl.scope) in seen:
                continue
            seen.add((decl.key, decl.manifest, decl.scope))
            rows.append(self._detail_row(repo, decl.package, DIRECT, decl.scope, decl.manifest, decl.raw,
                                         decl, alerts_by_pkg.get(decl.key, []), decl.note, locked.get(decl.key, "")))
        declared_keys = {d.key for d in declarations}
        for key, alerts in alerts_by_pkg.items():
            if key in declared_keys:
                continue
            first = alerts[0]
            rows.append(self._detail_row(repo, first.package, first.relationship, first.dependency_scope, first.manifest,
                                         "Not declared", None, alerts,
                                         "Not declared in the tap's manifests; pulled in by another package",
                                         locked.get(key, "") if first.ecosystem == "pip" else ""))
        return rows

    def _detail_row(self, repo, package, relationship, scope, manifest, specifier, decl, alerts, note,
                    locked: str = "") -> DependencyDetailRow:
        latest_info = self.pypi.get_latest_version(package)
        latest = latest_info.latest_version or ""
        fixed = max_version(v.fixed_version for v in alerts if v.fix_available == "Yes")
        if locked:
            current = f"{locked} (lock file)"
        elif decl is None:
            current = "Not declared (resolved at install time)"
        else:
            current = decl.pinned_version or (f"Unpinned (minimum {decl.baseline_version})" if decl.baseline_version else "Unpinned")
        return DependencyDetailRow(
            tap=repo.name, dependency=package, relationship=relationship, scope=scope, manifest=manifest,
            declared_specifier=specifier, current_version=current,
            latest_pypi_version=latest or f"({latest_info.error or 'unknown'})",
            fixed_security_version=fixed or ("None published" if alerts else "N/A"),
            vulnerable="Yes" if alerts else "No", open_alerts=len(alerts),
            highest_severity=highest_severity(v.severity for v in alerts) if alerts else "None",
            outdated=self.outdated(decl, latest, locked),
            upgrade_required="Yes" if fixed else ("No fix published" if alerts else "No"), notes=note,
        )

    @staticmethod
    def outdated(decl: Optional[DependencyDeclaration], latest: str, locked: str = "") -> str:
        latest_version = parse_version(latest)
        if latest_version is not None and parse_version(locked) is not None:
            locked_version = parse_version(locked)
            return f"Yes (locked {locked_version} < latest {latest_version})" if locked_version < latest_version else "No"
        if decl is None or latest_version is None:
            return "Unknown"
        if decl.pinned_version:
            pinned = parse_version(decl.pinned_version)
            if pinned is None:
                return "Unknown"
            return f"Yes (pinned {pinned} < latest {latest_version})" if pinned < latest_version else "No"
        if not decl.specifier:
            return "No (unconstrained)"
        try:
            allows = SpecifierSet(decl.specifier).contains(latest_version, prereleases=True)
        except InvalidSpecifier:
            return "Unknown"
        return "No (constraint allows latest)" if allows else f"Yes (constraint excludes latest {latest_version})"


# ============================================================================
# Summaries
# ============================================================================

def build_tap_summary(result: TapAuditResult, ticket_actions: Optional[Dict[str, str]] = None) -> TapSummary:
    repo, vulns = result.repo, result.vulnerabilities
    counts = {s: 0 for s in SEVERITIES}
    for v in vulns:
        counts[v.severity] = counts.get(v.severity, 0) + 1
    dependabot = [v for v in vulns if v.tool == TOOL_DEPENDABOT]
    cvss = [v.cvss_score for v in vulns if v.cvss_score is not None]
    ages = [v.alert_age_days for v in vulns if v.alert_age_days is not None]
    ticket_refs = {v.jira_ref for v in vulns if v.jira_ref}
    prs = {u for v in vulns for u in v.existing_pr_urls.splitlines() if u}
    if ticket_refs:
        top_ref = min(ticket_refs)  # refs are numbered in priority order
        action = f"{top_ref}: {(ticket_actions or {}).get(top_ref, '')}" + (
            f" (+{len(ticket_refs) - 1} more Jira candidate(s))" if len(ticket_refs) > 1 else "")
    elif vulns:
        action = "No Jira ticket needed now - low-risk or no-fix findings; see Vulnerabilities tab"
    elif result.analysis_status == STATUS_CLEAN:
        action = "No action required"
    else:
        action = next_step_for(result)
    notes = []
    if result.scan_successful and not result.declarations and not result.locked_versions:
        notes.append(f"Dependency manifests: {result.dependency_status}")
    if result.error and result.scan_successful:
        notes.append(f"Partial data: {result.error}")
    if result.analysis_status in (STATUS_MANUAL, STATUS_FAILED):
        notes.append(result.error or result.status_detail)
    return TapSummary(
        tap_name=repo.name, repository_url=repo.html_url, security_url=repo.security_url, lifecycle=repo.lifecycle,
        last_pushed=repo.pushed_at, analysis_status=result.analysis_status,
        dependabot_status=result.dependabot_status, code_scanning_status=result.code_scanning_status,
        total_alerts=len(vulns), critical=counts["Critical"], high=counts["High"], medium=counts["Medium"],
        low=counts["Low"], informational=counts["Informational"],
        runtime_alerts=sum(1 for v in dependabot if v.dependency_scope == RUNTIME),
        development_alerts=sum(1 for v in dependabot if v.dependency_scope == DEVELOPMENT),
        code_scanning_alerts=len(vulns) - len(dependabot),
        fix_available=sum(1 for v in dependabot if v.fix_available == "Yes"),
        known_exploited=sum(1 for v in vulns if v.known_exploited == "Yes"), open_remediation_prs=len(prs),
        highest_severity=highest_severity(v.severity for v in vulns), highest_cvss=max(cvss) if cvss else None,
        oldest_alert_days=max(ages) if ages else None,
        recommended_priority=highest_priority(v.recommended_priority for v in vulns) if vulns else "None",
        jira_tickets=len(ticket_refs), primary_recommended_action=action, notes="; ".join(n for n in notes if n),
    )


def build_package_summary(vulnerabilities: List[Vulnerability]) -> List[PackageSummaryRow]:
    groups: Dict[Tuple[str, str], List[Vulnerability]] = defaultdict(list)
    for v in vulnerabilities:
        if v.tool == TOOL_DEPENDABOT:
            groups[(v.ecosystem, normalize_package(v.package))].append(v)
    rows = []
    for (ecosystem, _), vulns in groups.items():
        taps = sorted({v.tap_name for v in vulns})
        rows.append(PackageSummaryRow(
            package=vulns[0].package, ecosystem=ecosystem, alerts=len(vulns), taps_affected=len(taps),
            highest_severity=highest_severity(v.severity for v in vulns),
            highest_priority=highest_priority(v.recommended_priority for v in vulns),
            fixed_version_needed=max_version(v.fixed_version for v in vulns if v.fix_available == "Yes") or "None published",
            runtime_alerts=sum(1 for v in vulns if v.dependency_scope == RUNTIME),
            development_alerts=sum(1 for v in vulns if v.dependency_scope == DEVELOPMENT),
            advisories=join_unique((v.cve or v.ghsa for v in vulns), ", "), taps=", ".join(taps),
        ))
    return sorted(rows, key=lambda r: (SEVERITY_RANK.get(r.highest_severity, 9), -r.taps_affected, r.package.lower()))


def build_analysis_log(results: List[TapAuditResult], non_taps: List[RepoInfo]) -> List[AnalysisLogEntry]:
    entries = []
    for r in results:
        entries.append(AnalysisLogEntry(
            repository=r.repo.full_name, repository_url=r.repo.html_url, identified_as_tap="Yes",
            tap_detection_reason=r.repo.tap_detection_reason, lifecycle=r.repo.lifecycle,
            dependabot_status=r.dependabot_status, code_scanning_status=r.code_scanning_status,
            dependency_status=r.dependency_status, vulnerability_scan_successful="Yes" if r.scan_successful else "No",
            vulnerabilities_found=len(r.vulnerabilities), analysis_status=r.analysis_status,
            error=r.error or ("" if r.scan_successful else r.status_detail), attempts=r.attempts,
            duration_seconds=r.duration, timestamp=r.timestamp,
        ))
    now = utc_now()
    for repo in non_taps:
        entries.append(AnalysisLogEntry(
            repository=repo.full_name, repository_url=repo.html_url, identified_as_tap="No",
            tap_detection_reason=repo.tap_detection_reason, lifecycle=repo.lifecycle, dependabot_status="Skipped",
            code_scanning_status="Skipped", dependency_status="Skipped", vulnerability_scan_successful="No",
            vulnerabilities_found=0, analysis_status=STATUS_NOT_TAP, error="", attempts=0, duration_seconds=None,
            timestamp=now,
        ))
    return sorted(entries, key=lambda e: e.repository.lower())


def build_not_audited(results: List[TapAuditResult]) -> List[NotAuditedRow]:
    rows = [NotAuditedRow(r.repo.full_name, r.repo.html_url, r.analysis_status,
                          r.error or r.status_detail, next_step_for(r))
            for r in results if not r.scan_successful]
    return sorted(rows, key=lambda r: (r.analysis_status != STATUS_FAILED, r.analysis_status, r.repository.lower()))


# ============================================================================
# Validation against a GitHub org security-overview CSV export
# ============================================================================

class ExportValidator:
    """Cross-checks the audit against the CSV that GitHub produces from
    Organization > Security > Overview > Export (open alerts = empty
    'Resolved At'). Differences are expected only for alerts opened/closed
    between the export and the audit run."""

    TOOL_MAP = {"dependabot": TOOL_DEPENDABOT, "codeql": TOOL_CODEQL}

    def __init__(self, csv_path: Path, org: str):
        self.csv_path = csv_path
        self.org = org
        self.open_alerts: Dict[Tuple[str, str], Dict[int, str]] = defaultdict(dict)
        self.rows_read = 0

    def load(self) -> None:
        with self.csv_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                self.rows_read += 1
                repository = (row.get("Repository") or "").strip()
                tool = self.TOOL_MAP.get((row.get("Tool") or "").strip().lower())
                if not tool or not repository.startswith(f"{self.org}/") or (row.get("Resolved At") or "").strip():
                    continue
                try:
                    number = int(row.get("Alert Number") or "")
                except ValueError:
                    continue
                severity = SEVERITY_NORMALIZE.get((row.get("Severity") or "").strip().lower(), "Informational")
                self.open_alerts[(repository.split("/", 1)[1], tool)][number] = severity

    def compare(self, results: List[TapAuditResult], include_code_scanning: bool,
                full_org_scope: bool = True) -> Tuple[List[ValidationRow], Dict[str, int]]:
        rows: List[ValidationRow] = []
        stats = {"compared": 0, "match": 0, "mismatch": 0, "not_audited": 0}
        tools = [TOOL_DEPENDABOT] + ([TOOL_CODEQL] if include_code_scanning else [])
        by_name = {r.repo.name: r for r in results}
        for result in results:
            if not result.scan_successful:
                continue
            for tool in tools:
                expected = self.open_alerts.get((result.repo.name, tool), {})
                found = {v.alert_number: v.severity for v in result.vulnerabilities if v.tool == tool}
                stats["compared"] += 1
                matched = set(expected) & set(found)
                only_export, only_script = sorted(set(expected) - set(found)), sorted(set(found) - set(expected))
                sev_mismatch = sorted(n for n in matched if expected[n] != found[n])
                ok = not only_export and not only_script and not sev_mismatch
                stats["match" if ok else "mismatch"] += 1
                if not expected and not found:
                    continue  # both clean - counted but not listed
                rows.append(ValidationRow(
                    repository=result.repo.full_name, tool=tool, export_open_alerts=len(expected),
                    script_open_alerts=len(found), matched=len(matched),
                    only_in_export=", ".join(f"#{n}" for n in only_export),
                    only_in_script=", ".join(f"#{n}" for n in only_script),
                    severity_mismatches=", ".join(f"#{n} export={expected[n]} script={found[n]}" for n in sev_mismatch),
                    result="Match" if ok else "Mismatch",
                    notes="" if ok else "Alert(s) likely opened/closed between the export and this run - verify on the Security tab",
                ))
        for (name, tool), expected in sorted(self.open_alerts.items()):
            if not expected or (tool == TOOL_CODEQL and not include_code_scanning):
                continue
            result = by_name.get(name)
            if (result is not None and result.scan_successful) or (result is None and not full_org_scope):
                continue
            stats["not_audited"] += 1
            rows.append(ValidationRow(
                repository=f"{self.org}/{name}", tool=tool, export_open_alerts=len(expected), script_open_alerts=None,
                matched=0, only_in_export=", ".join(f"#{n}" for n in sorted(expected)), only_in_script="",
                severity_mismatches="", result="Not compared",
                notes=(f"Repository not deep-scanned: {result.analysis_status}" if result
                       else "Repository outside the audit scope (not a tap, or not selected via --tap)"),
            ))
        order = {"Mismatch": 0, "Not compared": 1, "Match": 2}
        return sorted(rows, key=lambda r: (order.get(r.result, 9), r.repository.lower(), r.tool)), stats


# ============================================================================
# Excel report
# ============================================================================

@dataclass
class Column:
    header: str
    getter: Callable[[object], object]
    width: int = 14
    kind: str = "text"  # text | wrap | url | int | float | pct | date
    fills: Optional[Dict[str, str]] = None


def col(header: str, attr: str, width: int = 14, kind: str = "text", fills: Optional[Dict[str, str]] = None) -> Column:
    return Column(header, lambda row, a=attr: getattr(row, a), width, kind, fills)


class ReportGenerator:
    HEADER_FILL = "1F3864"
    SECTION_FILL = "D9E1F2"

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---- generic helpers ----
    def _write_table(self, ws, columns: List[Column], rows: Sequence[object], start_row: int = 1,
                     freeze_col: int = 2, autofilter: bool = True) -> int:
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor=self.HEADER_FILL)
        for c_idx, column in enumerate(columns, 1):
            cell = ws.cell(row=start_row, column=c_idx, value=column.header)
            cell.font, cell.fill = header_font, header_fill
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            ws.column_dimensions[get_column_letter(c_idx)].width = column.width
        ws.row_dimensions[start_row].height = 32
        link_font = Font(color="0563C1", underline="single")
        wrap = Alignment(wrap_text=True, vertical="top")
        top = Alignment(vertical="top")
        fill_cache: Dict[str, PatternFill] = {}
        for r_idx, row in enumerate(rows, start_row + 1):
            for c_idx, column in enumerate(columns, 1):
                value = excel_safe(column.getter(row))
                cell = ws.cell(row=r_idx, column=c_idx, value=value)
                cell.alignment = wrap if column.kind == "wrap" else top
                if column.kind == "date" and value is not None:
                    cell.number_format = "yyyy-mm-dd"
                elif column.kind == "float" and value is not None:
                    cell.number_format = "0.0"
                elif column.kind == "pct" and value is not None:
                    cell.number_format = "0.00%"
                elif column.kind == "url" and isinstance(value, str) and value.startswith("http") and "\n" not in value:
                    cell.hyperlink, cell.font = value, link_font
                if column.fills and isinstance(value, str):
                    color = column.fills.get(value) or column.fills.get(value.split(" ", 1)[0])
                    if color:
                        cell.fill = fill_cache.setdefault(color, PatternFill("solid", fgColor=color))
        last_row = start_row + len(rows)
        if autofilter:
            ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(columns))}{max(last_row, start_row + 1)}"
        if freeze_col:
            ws.freeze_panes = ws.cell(row=start_row + 1, column=freeze_col)
        return last_row

    def _write_key_values(self, ws, start_row: int, title: str, pairs: Sequence[Tuple[str, object]]) -> int:
        cell = ws.cell(row=start_row, column=1, value=title)
        cell.font = Font(bold=True, size=12)
        cell.fill = PatternFill("solid", fgColor=self.SECTION_FILL)
        ws.cell(row=start_row, column=2).fill = PatternFill("solid", fgColor=self.SECTION_FILL)
        row = start_row + 1
        for key, value in pairs:
            ws.cell(row=row, column=1, value=key).font = Font(bold=True)
            value_cell = ws.cell(row=row, column=2, value=excel_safe(value))
            value_cell.alignment = Alignment(wrap_text=True, vertical="top")
            if isinstance(value, dt.datetime):
                value_cell.number_format = "yyyy-mm-dd hh:mm:ss"
            if isinstance(value, str) and value.startswith("http"):
                value_cell.hyperlink, value_cell.font = value, Font(color="0563C1", underline="single")
            row += 1
        return row + 1

    # ---- workbook ----
    def write(self, *, metadata: List[Tuple[str, object]], reconciliation: List[Tuple[str, object]],
              tap_summaries: List[TapSummary], vulnerabilities: List[Vulnerability], tickets: List[JiraTicket],
              package_rows: List[PackageSummaryRow], dependency_rows: List[DependencyDetailRow],
              not_audited: List[NotAuditedRow], analysis_log: List[AnalysisLogEntry],
              validation: Optional[Tuple[List[ValidationRow], Dict[str, int]]]) -> Path:
        if not HAVE_OPENPYXL:
            raise RuntimeError("openpyxl is required to write the report (pip install openpyxl)")
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        self._executive_summary(wb, metadata, reconciliation, tap_summaries, vulnerabilities, tickets,
                                package_rows, not_audited, validation)
        self._overview(wb, metadata, reconciliation, vulnerabilities, tickets)
        self._severity_summary(wb, vulnerabilities, tap_summaries)
        self._tap_summary(wb, tap_summaries)
        self._vulnerabilities(wb, vulnerabilities)
        self._jira(wb, tickets)
        self._packages(wb, package_rows)
        self._dependencies(wb, dependency_rows)
        self._not_audited(wb, not_audited)
        self._analysis_log(wb, analysis_log)
        if validation is not None:
            self._validation(wb, *validation)
        wb.properties.title = "Singer tap security audit"
        wb.properties.creator = f"audit_singer_security.py {__version__}"
        wb.save(self.path)
        LOGGER.info("Wrote %s", self.path)
        return self.path

    def _overview(self, wb, metadata, reconciliation, vulnerabilities, tickets):
        ws = wb.create_sheet("Overview")
        ws.sheet_properties.tabColor = self.HEADER_FILL
        ws.column_dimensions["A"].width = 38
        ws.column_dimensions["B"].width = 110
        title = ws.cell(row=1, column=1, value="Singer.io Tap Security Vulnerability Audit")
        title.font = Font(bold=True, size=16)
        row = self._write_key_values(ws, 3, "Execution metadata", metadata)
        row = self._write_key_values(ws, row, "Reconciliation (every tap is accounted for)", reconciliation)
        sev = {s: sum(1 for v in vulnerabilities if v.severity == s) for s in SEVERITIES}
        row = self._write_key_values(ws, row, "Findings", [
            ("Open alerts (total)", len(vulnerabilities)),
            *[(f"  {s}", sev[s]) for s in SEVERITIES],
            ("Taps with open alerts", len({v.tap_name for v in vulnerabilities})),
            ("Actively exploited (CISA KEV)", sum(1 for v in vulnerabilities if v.known_exploited == "Yes")),
            ("Jira candidate alerts", sum(1 for v in vulnerabilities if v.jira_candidate == "Yes")),
            ("Jira tickets recommended (grouped per tap + package/rule)", len(tickets)),
        ])
        row = self._write_key_values(ws, row, "Priority legend", list(PRIORITY_GUIDANCE.items()))
        self._write_key_values(ws, row, "How to use this workbook", [
            ("Executive Summary", "Final report: key findings, highest-risk taps, top packages, recommended actions"),
            ("Severity Summary", "Org-wide counts by severity/priority and the taps with the most severe findings"),
            ("Tap Summary", "One row per tap: severity counts, highest priority, primary recommended action"),
            ("Vulnerabilities", "One row per open alert, grouped by tap then severity, with full traceability links"),
            ("Jira Candidates", "Ready-to-file tickets (one per tap + package/rule); columns map to Jira CSV import fields"),
            ("Package Summary", "Vulnerable packages across all taps - useful for org-wide bulk upgrades"),
            ("Dependency Details", "Every declared dependency per tap vs latest PyPI version and security fix"),
            ("Not Audited", "Taps that could not be fully audited, with the reason and next step"),
            ("Analysis Log", "Every discovered repository and what happened to it"),
            ("Validation", "Present when --validate-csv is used: audit vs GitHub org security export"),
        ])

    def _executive_summary(self, wb, metadata, reconciliation, tap_summaries: List[TapSummary],
                           vulnerabilities: List[Vulnerability], tickets: List[JiraTicket],
                           package_rows: List[PackageSummaryRow], not_audited: List[NotAuditedRow], validation):
        ws = wb.create_sheet("Executive Summary")
        ws.sheet_properties.tabColor = "C00000"
        meta, recon = dict(metadata), dict(reconciliation)
        width = 10  # columns spanned by merged text rows

        def text_row(row: int, value: str, bold: bool = False, size: int = 11, fill: Optional[str] = None) -> int:
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
            cell = ws.cell(row=row, column=1, value=excel_safe(value))
            cell.font = Font(bold=bold, size=size)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if fill:
                cell.fill = PatternFill("solid", fgColor=fill)
            ws.row_dimensions[row].height = max(15, 15 * (1 + len(value) // 150))
            return row + 1

        def section(row: int, title: str) -> int:
            return text_row(row + 1, title, bold=True, size=12, fill=self.SECTION_FILL)

        def ns(**kwargs):
            return type("Row", (), kwargs)()

        total = len(vulnerabilities)
        sev = {s: sum(1 for v in vulnerabilities if v.severity == s) for s in SEVERITIES}
        taps_affected = len({v.tap_name for v in vulnerabilities})
        taps_total = recon.get("Singer taps identified", len(tap_summaries)) or 0
        pct = lambda part, whole: f"{(100 * part / whole):.0f}%" if whole else "0%"
        dependabot = [v for v in vulnerabilities if v.tool == TOOL_DEPENDABOT]
        runtime = sum(1 for v in dependabot if v.dependency_scope == RUNTIME)
        fixable = sum(1 for v in dependabot if v.fix_available == "Yes")
        kev = sum(1 for v in vulnerabilities if v.known_exploited == "Yes")
        by_priority = {p: [t for t in tickets if t.priority == p] for p in PRIORITIES}
        with_pr = sum(1 for t in tickets if t.existing_prs)

        row = text_row(1, "Singer.io Tap Security Vulnerability Audit - Executive Summary", bold=True, size=16)
        row = text_row(row, f"Audit date: {meta.get('Audit date (UTC)', '')} (UTC)   |   Scope: {meta.get('Scope', '')}   |   "
                            f"Organization: {meta.get('GitHub organization', '')}   |   Tool version: {meta.get('Script version', '')}")

        findings = [
            f"{taps_total} Singer taps identified out of {recon.get('Repositories discovered', '?')} repositories; "
            f"{recon.get('Taps successfully analyzed', '?')} fully audited, "
            f"{recon.get('Taps archived/disabled (skipped by design)', 0)} archived taps skipped by design, "
            f"{recon.get('Taps failed (see Not Audited)', 0)} failed, {recon.get('Taps requiring manual review', 0)} need manual review.",
            f"{taps_affected} taps ({pct(taps_affected, taps_total)}) have {total} open security alerts: "
            f"{sev['Critical']} Critical, {sev['High']} High, {sev['Medium']} Medium, {sev['Low']} Low. "
            f"{kev or 'None'} {'is' if kev == 1 else 'are'} listed in the CISA Known Exploited Vulnerabilities catalog.",
            f"{pct(runtime, len(dependabot))} of dependency alerts are in runtime dependencies and "
            f"{pct(fixable, len(dependabot))} already have a published fix - this is mainly an upgrade backlog.",
        ]
        if package_rows:
            top = max(package_rows, key=lambda p: p.alerts)
            findings.append(f"`{top.package}` accounts for {top.alerts} alerts ({pct(top.alerts, total)}) across "
                            f"{top.taps_affected} taps; upgrading it to >= {top.fixed_version_needed} clears them.")
        findings.append(f"{len(tickets)} Jira tickets recommended (one per tap + package/rule): "
                        + ", ".join(f"{len(ts)} x {p.split(' - ')[0]}" for p, ts in by_priority.items() if ts)
                        + f". {with_pr} already have an open PR that only needs review/merge.")
        if validation is not None:
            stats = validation[1]
            findings.append(f"Validation against the GitHub org security export: {stats['match']}/{stats['compared']} "
                            f"repo/tool pairs match ({stats['mismatch']} mismatches).")
        row = section(row, "Key findings")
        for i, finding in enumerate(findings, 1):
            row = text_row(row, f"{i}. {finding}")

        tickets_by_tap: Dict[str, List[JiraTicket]] = defaultdict(list)
        for t in tickets:
            tickets_by_tap[t.tap].append(t)
        risky = [t for t in tap_summaries if t.recommended_priority in PRIORITIES[:2]]
        if not risky:
            risky = [t for t in tap_summaries if t.total_alerts][:10]
        risky.sort(key=lambda t: (PRIORITY_RANK.get(t.recommended_priority, 9), -t.critical, -t.high, -t.total_alerts))
        row = section(row, "Highest-risk taps")
        row = self._write_table(ws, [
            col("Tap", "tap"), col("Alerts", "alerts"), col("Critical", "critical"), col("High", "high"),
            col("Medium", "medium"), col("Low", "low"), col("Priority", "priority", fills=PRIORITY_FILLS),
            col("Jira Tickets", "jira"), col("Open PRs", "prs"), col("Upgrades needed (current -> target)", "upgrades", kind="wrap"),
        ], [ns(tap=t.tap_name, alerts=t.total_alerts, critical=t.critical, high=t.high, medium=t.medium, low=t.low,
               priority=t.recommended_priority, jira=t.jira_tickets, prs=t.open_remediation_prs,
               upgrades="; ".join(f"{j.package_or_rule} {j.current_version} -> {j.target_version}"
                                  for j in tickets_by_tap.get(t.tap_name, [])))
            for t in risky], start_row=row, freeze_col=0, autofilter=False) + 1

        row = section(row, "Top vulnerable packages across the org")
        row = self._write_table(ws, [
            col("Package", "package"), col("Alerts", "alerts"), col("Taps Affected", "taps_affected"),
            col("Highest Severity", "highest_severity", fills=SEVERITY_FILLS),
            col("Highest Priority", "highest_priority", fills=PRIORITY_FILLS),
            col("Minimum Safe Version", "fixed_version_needed"), col("Runtime Alerts", "runtime_alerts"),
            col("Dev Alerts", "development_alerts"), col("Advisories", "advisories"), col("Affected Taps", "taps", kind="wrap"),
        ], sorted(package_rows, key=lambda p: (-p.alerts, p.package.lower()))[:15],
            start_row=row, freeze_col=0, autofilter=False) + 1

        actions = []
        for priority, group in by_priority.items():
            if not group:
                continue
            taps = sorted({t.tap for t in group})
            if PRIORITY_RANK[priority] <= 1:
                detail = "; ".join(t.summary.split("] ", 1)[-1].replace("Security: ", "") + f" [{t.tap}]"
                                   + (" - open PR exists" if t.existing_prs else "") for t in group)
            else:
                common = defaultdict(set)
                for t in group:
                    common[t.package_or_rule].add(t.tap)
                detail = "Most common: " + ", ".join(f"{pkg} ({len(ts)} tap{'s' if len(ts) != 1 else ''})" for pkg, ts in
                                                     sorted(common.items(), key=lambda kv: -len(kv[1]))[:5])
            prs = sum(1 for t in group if t.existing_prs)
            actions.append(ns(priority=priority, tickets=len(group), taps=len(taps),
                              refs=f"{group[0].ref} - {group[-1].ref}", prs=prs,
                              guidance=PRIORITY_GUIDANCE[priority], detail=detail))
        no_fix = [p for p in package_rows if p.fixed_version_needed == "None published"]
        if no_fix:
            actions.append(ns(priority="Monitor", tickets=0, taps=sum(p.taps_affected for p in no_fix), refs="", prs=0,
                              guidance="No patched release published - monitor advisories",
                              detail="; ".join(f"{p.package}: {p.taps}" for p in no_fix)))
        if not_audited:
            actions.append(ns(priority="Not audited", tickets=0, taps=len(not_audited), refs="", prs=0,
                              guidance="See the Not Audited sheet for the reason and next step",
                              detail=", ".join(r.repository.split("/")[-1] for r in not_audited)))
        row = section(row, "Recommended actions")
        row = self._write_table(ws, [
            col("Priority", "priority", fills=PRIORITY_FILLS), col("Jira Tickets", "tickets"), col("Taps", "taps"),
            col("Ticket Refs", "refs"), col("With Open PR", "prs"), col("Guidance", "guidance", kind="wrap"),
            col("Details", "detail", kind="wrap"),
        ], actions, start_row=row, freeze_col=0, autofilter=False) + 1

        row = section(row, "Coverage and validation")
        coverage = list(reconciliation) + [("GitHub API requests", meta.get("GitHub API requests made", "")),
                                           ("Duration", meta.get("Duration", ""))]
        self._write_table(ws, [col("Check", "check"), col("Result", "result", kind="wrap")],
                          [ns(check=k.strip(), result=v) for k, v in coverage], start_row=row, freeze_col=0, autofilter=False)

        for c, w in enumerate((28, 12, 12, 12, 16, 16, 18, 45, 60, 60), 1):
            ws.column_dimensions[get_column_letter(c)].width = w
        ws.sheet_view.showGridLines = False

    def _severity_summary(self, wb, vulnerabilities: List[Vulnerability], tap_summaries: List[TapSummary]):
        ws = wb.create_sheet("Severity Summary")

        @dataclass
        class SevRow:
            label: str
            total: int
            dependabot: int
            code_scanning: int
            runtime: int
            development: int
            direct: int
            transitive: int
            fix_available: int
            no_fix: int
            kev: int
            taps: int
            jira: int

        def make(label: str, vulns: List[Vulnerability]) -> SevRow:
            dep = [v for v in vulns if v.tool == TOOL_DEPENDABOT]
            return SevRow(label, len(vulns), len(dep), len(vulns) - len(dep),
                          sum(1 for v in dep if v.dependency_scope == RUNTIME),
                          sum(1 for v in dep if v.dependency_scope == DEVELOPMENT),
                          sum(1 for v in dep if v.relationship == DIRECT),
                          sum(1 for v in dep if v.relationship != DIRECT),
                          sum(1 for v in dep if v.fix_available == "Yes"),
                          sum(1 for v in dep if v.fix_available == "No"),
                          sum(1 for v in vulns if v.known_exploited == "Yes"),
                          len({v.tap_name for v in vulns}), sum(1 for v in vulns if v.jira_candidate == "Yes"))

        columns = [col("Severity / Priority", "label", 22, fills={**SEVERITY_FILLS, **PRIORITY_FILLS}),
                   col("Total Alerts", "total", 12), col("Dependabot", "dependabot", 12),
                   col("Code Scanning", "code_scanning", 13), col("Runtime", "runtime", 10),
                   col("Development", "development", 12), col("Direct", "direct", 10),
                   col("Transitive / Unknown", "transitive", 13), col("Fix Available", "fix_available", 12),
                   col("No Fix Published", "no_fix", 12), col("Known Exploited", "kev", 12),
                   col("Taps Affected", "taps", 12), col("Jira Candidate Alerts", "jira", 14)]
        by_sev = [make(s, [v for v in vulnerabilities if v.severity == s]) for s in SEVERITIES]
        by_sev.append(make("Total", vulnerabilities))
        ws.cell(row=1, column=1, value="By severity").font = Font(bold=True, size=12)
        end = self._write_table(ws, columns, by_sev, start_row=2, freeze_col=0, autofilter=False)
        for c in range(1, len(columns) + 1):
            ws.cell(row=end, column=c).font = Font(bold=True)

        start = end + 3
        ws.cell(row=start - 1, column=1, value="By recommended priority").font = Font(bold=True, size=12)
        by_pri = [make(p, [v for v in vulnerabilities if v.recommended_priority == p]) for p in PRIORITIES]
        end = self._write_table(ws, columns, by_pri, start_row=start, freeze_col=0, autofilter=False)

        start = end + 3
        ws.cell(row=start - 1, column=1, value="Tap x severity matrix (taps with open alerts, most severe first)").font = Font(bold=True, size=12)
        matrix_cols = [col("Tap", "tap_name", 30), col("Critical", "critical", 10), col("High", "high", 10),
                       col("Medium", "medium", 10), col("Low", "low", 10), col("Informational", "informational", 12),
                       col("Total Alerts", "total_alerts", 12), col("Highest Severity", "highest_severity", 14, fills=SEVERITY_FILLS),
                       col("Recommended Priority", "recommended_priority", 16, fills=PRIORITY_FILLS),
                       col("Jira Tickets", "jira_tickets", 12)]
        affected = sorted((t for t in tap_summaries if t.total_alerts),
                          key=lambda t: (SEVERITY_RANK.get(t.highest_severity, 9), -t.critical, -t.high, -t.total_alerts, t.tap_name))
        self._write_table(ws, matrix_cols, affected, start_row=start, freeze_col=0, autofilter=False)
        ws.column_dimensions["A"].width = 30

    def _tap_summary(self, wb, rows: List[TapSummary]):
        ws = wb.create_sheet("Tap Summary")
        rows = sorted(rows, key=lambda t: (PRIORITY_RANK.get(t.recommended_priority, 9), SEVERITY_RANK.get(t.highest_severity, 9),
                                           -t.total_alerts, t.tap_name.lower()))
        self._write_table(ws, [
            col("Tap Name", "tap_name", 28), col("Repository URL", "repository_url", 40, "url"),
            col("Security URL", "security_url", 44, "url"), col("Active/Archived", "lifecycle", 12),
            col("Last Pushed", "last_pushed", 12, "date"),
            col("Analysis Status", "analysis_status", 22, fills=STATUS_FILLS),
            col("Dependabot", "dependabot_status", 22), col("Code Scanning", "code_scanning_status", 22),
            col("Total Alerts", "total_alerts", 9, "int"), col("Critical", "critical", 8, "int"),
            col("High", "high", 8, "int"), col("Medium", "medium", 8, "int"), col("Low", "low", 8, "int"),
            col("Informational", "informational", 10, "int"), col("Runtime Alerts", "runtime_alerts", 9, "int"),
            col("Dev Alerts", "development_alerts", 9, "int"), col("Code Scanning Alerts", "code_scanning_alerts", 10, "int"),
            col("Fix Available", "fix_available", 9, "int"), col("Known Exploited", "known_exploited", 9, "int"),
            col("Open Remediation PRs", "open_remediation_prs", 11, "int"),
            col("Highest Severity", "highest_severity", 12, fills=SEVERITY_FILLS),
            col("Highest CVSS", "highest_cvss", 9, "float"), col("Oldest Alert (days)", "oldest_alert_days", 10, "int"),
            col("Recommended Priority", "recommended_priority", 16, fills=PRIORITY_FILLS),
            col("Jira Tickets", "jira_tickets", 9, "int"),
            col("Primary Recommended Action", "primary_recommended_action", 70, "wrap"), col("Notes", "notes", 50, "wrap"),
        ], rows)

    def _vulnerabilities(self, wb, rows: List[Vulnerability]):
        ws = wb.create_sheet("Vulnerabilities")
        rows = sorted(rows, key=lambda v: (v.tap_name.lower(), SEVERITY_RANK.get(v.severity, 9),
                                           PRIORITY_RANK.get(v.recommended_priority, 9), -(v.cvss_score or 0), v.package, v.alert_number or 0))
        self._write_table(ws, [
            col("Tap Name", "tap_name", 24), col("Severity", "severity", 11, fills=SEVERITY_FILLS),
            col("Recommended Priority", "recommended_priority", 16, fills=PRIORITY_FILLS),
            col("Jira Candidate", "jira_candidate", 9, fills=YES_NO_FILLS), col("Jira Candidate Ref", "jira_ref", 10),
            col("Tool", "tool", 11), col("Alert #", "alert_number", 7, "int"), col("Alert URL", "alert_url", 42, "url"),
            col("CVE", "cve", 16), col("GHSA", "ghsa", 21), col("Package / Rule", "package", 18),
            col("Ecosystem", "ecosystem", 9), col("Rule ID", "rule_id", 18), col("Dependency Scope", "dependency_scope", 12),
            col("Direct/Transitive", "relationship", 11), col("Manifest", "manifest", 16),
            col("Declared Constraint", "declared_specifier", 22), col("Current Version", "current_version", 18),
            col("Current Version Vulnerable?", "current_version_vulnerable", 18),
            col("Vulnerable Version Range", "vulnerable_version_range", 18), col("Fixed Version", "fixed_version", 12),
            col("Fix Available", "fix_available", 9), col("Fix Allowed by Current Constraint", "fix_allowed_by_current_spec", 18),
            col("CVSS Score", "cvss_score", 8, "float"), col("CVSS Vector", "cvss_vector", 30), col("CWE", "cwe", 14),
            col("EPSS (30-day probability)", "epss_percentage", 11, "pct"), col("EPSS Percentile", "epss_percentile", 10, "pct"),
            col("Known Exploited (CISA KEV)", "known_exploited", 11), col("Exploitability", "exploitability", 36, "wrap"),
            col("Summary", "summary", 45, "wrap"), col("Technical Impact", "technical_impact", 45, "wrap"),
            col("Recommended Action", "recommended_action", 60, "wrap"), col("Upgrade Required", "upgrade_required", 11),
            col("Code Changes Required", "code_changes_required", 45, "wrap"),
            col("Breaking Change Risk", "breaking_change_risk", 28, "wrap"),
            col("Existing Open PR(s)", "existing_pr_urls", 40, "url"), col("Dependabot PR", "dependabot_pr", 9),
            col("Existing Open Issue(s)", "existing_issue_urls", 40, "url"),
            col("Jira Candidate Reason", "jira_candidate_reason", 40, "wrap"),
            col("Alert Created", "alert_created", 12, "date"), col("Alert Age (days)", "alert_age_days", 9, "int"),
            col("Advisory Published", "advisory_published", 12, "date"), col("Alert State", "alert_state", 9),
            col("Location", "location", 24), col("GHSA URL", "ghsa_url", 40, "url"), col("CVE URL", "cve_url", 40, "url"),
            col("Repository URL", "repository_url", 40, "url"), col("Security URL", "security_url", 44, "url"),
            col("References", "references", 50), col("Description", "description", 60),
        ], rows, freeze_col=3)

    def _jira(self, wb, rows: List[JiraTicket]):
        ws = wb.create_sheet("Jira Candidates")
        self._write_table(ws, [
            col("Ref", "ref", 9), col("Tap", "tap", 24), col("Issue Type", "issue_type", 8),
            col("Summary", "summary", 60, "wrap"), col("Priority", "priority", 16, fills=PRIORITY_FILLS),
            col("Jira Priority", "jira_priority", 9), col("Severity", "severity", 10, fills=SEVERITY_FILLS),
            col("Labels", "labels", 26), col("Tool", "tool", 11), col("Package / Rule", "package_or_rule", 20),
            col("Dependency Scope", "dependency_scope", 12), col("Current Version", "current_version", 18),
            col("Target Version", "target_version", 12), col("Alerts Covered", "alerts_covered", 8, "int"),
            col("CVE / GHSA", "advisories", 34, "wrap"), col("Alert URLs", "alert_urls", 44, "wrap"),
            col("Existing Open PR(s)", "existing_prs", 40, "url"),
            col("Recommended Action", "recommended_action", 60, "wrap"),
            col("Code Changes Required", "code_changes_required", 45, "wrap"),
            col("Breaking Change Risk", "breaking_change_risk", 28, "wrap"),
            col("Acceptance Criteria", "acceptance_criteria", 50, "wrap"), col("References", "references", 50),
            col("Description (Jira wiki markup)", "description", 80),
        ], rows, freeze_col=3)

    def _packages(self, wb, rows: List[PackageSummaryRow]):
        ws = wb.create_sheet("Package Summary")
        self._write_table(ws, [
            col("Package", "package", 22), col("Ecosystem", "ecosystem", 10), col("Open Alerts", "alerts", 10, "int"),
            col("Taps Affected", "taps_affected", 10, "int"), col("Highest Severity", "highest_severity", 12, fills=SEVERITY_FILLS),
            col("Highest Priority", "highest_priority", 16, fills=PRIORITY_FILLS),
            col("Minimum Safe Version (all advisories)", "fixed_version_needed", 16),
            col("Runtime Alerts", "runtime_alerts", 10, "int"), col("Dev Alerts", "development_alerts", 10, "int"),
            col("CVE / GHSA", "advisories", 50, "wrap"), col("Affected Taps", "taps", 80, "wrap"),
        ], rows)

    def _dependencies(self, wb, rows: List[DependencyDetailRow]):
        ws = wb.create_sheet("Dependency Details")
        rows = sorted(rows, key=lambda d: (d.tap.lower(), d.vulnerable != "Yes", d.dependency.lower()))
        self._write_table(ws, [
            col("Tap", "tap", 24), col("Dependency", "dependency", 22), col("Direct/Transitive", "relationship", 11),
            col("Scope", "scope", 11), col("Manifest", "manifest", 16), col("Declared Constraint", "declared_specifier", 26),
            col("Current Version", "current_version", 20), col("Latest PyPI Version", "latest_pypi_version", 14),
            col("Fixed Security Version", "fixed_security_version", 14),
            col("Vulnerable?", "vulnerable", 10, fills=YES_NO_FILLS), col("Open Alerts", "open_alerts", 8, "int"),
            col("Highest Severity", "highest_severity", 12, fills=SEVERITY_FILLS), col("Outdated?", "outdated", 30),
            col("Upgrade Required", "upgrade_required", 12, fills=YES_NO_FILLS), col("Notes", "notes", 45, "wrap"),
        ], rows)

    def _not_audited(self, wb, rows: List[NotAuditedRow]):
        ws = wb.create_sheet("Not Audited")
        self._write_table(ws, [
            col("Repository", "repository", 34), col("Repository URL", "repository_url", 42, "url"),
            col("Analysis Status", "analysis_status", 26, fills=STATUS_FILLS), col("Reason", "reason", 70, "wrap"),
            col("Suggested Next Step", "next_step", 70, "wrap"),
        ], rows)

    def _analysis_log(self, wb, rows: List[AnalysisLogEntry]):
        ws = wb.create_sheet("Analysis Log")
        self._write_table(ws, [
            col("Repository", "repository", 34), col("Repository URL", "repository_url", 40, "url"),
            col("Identified as Tap?", "identified_as_tap", 9), col("Tap Detection Reason", "tap_detection_reason", 30),
            col("Active/Archived", "lifecycle", 11), col("Dependabot Alerts", "dependabot_status", 24),
            col("Code Scanning", "code_scanning_status", 24), col("Dependency Manifests", "dependency_status", 40, "wrap"),
            col("Vulnerability Scan Successful?", "vulnerability_scan_successful", 11),
            col("Vulnerabilities Found", "vulnerabilities_found", 10, "int"),
            col("Analysis Status", "analysis_status", 24, fills=STATUS_FILLS), col("Error", "error", 60, "wrap"),
            col("Attempts", "attempts", 8, "int"), col("Duration (s)", "duration_seconds", 9, "float"),
            col("Timestamp (UTC)", "timestamp", 12, "date"),
        ], rows)

    def _validation(self, wb, rows: List[ValidationRow], stats: Dict[str, int]):
        ws = wb.create_sheet("Validation")
        ws.cell(row=1, column=1, value=(
            f"Repo/tool pairs compared: {stats['compared']} | matched: {stats['match']} | mismatched: {stats['mismatch']} "
            f"| export repos with open alerts not deep-scanned: {stats['not_audited']}")).font = Font(bold=True)
        self._write_table(ws, [
            col("Repository", "repository", 34), col("Tool", "tool", 11),
            col("Open Alerts in Export", "export_open_alerts", 10, "int"),
            col("Open Alerts Found by Script", "script_open_alerts", 10, "int"), col("Matched", "matched", 9, "int"),
            col("Only in Export", "only_in_export", 30, "wrap"), col("Only in Script", "only_in_script", 30, "wrap"),
            col("Severity Mismatches", "severity_mismatches", 30, "wrap"),
            col("Result", "result", 12, fills={"Match": "C6EFCE", "Mismatch": "FF9999", "Not compared": "FFEB9C"}),
            col("Notes", "notes", 60, "wrap"),
        ], rows, start_row=3)


# ============================================================================
# CLI
# ============================================================================

def setup_logging(verbose: bool, log_file: Optional[Path]) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    if log_file is not None:
        file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s %(message)s"))
        root.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def load_token(cli_token: Optional[str], config_path: Path) -> Tuple[Optional[str], str]:
    """Token lookup order: --github-token, GITHUB_TOKEN, GH_TOKEN, config file, gh CLI login."""
    if cli_token:
        return cli_token, "--github-token argument"
    for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(env_name):
            return os.environ[env_name], f"{env_name} environment variable"
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            token = data.get("github_token") or data.get("api_key") or data.get("token")
            if token:
                return token, f"config file ({config_path})"
        except (OSError, ValueError) as exc:
            LOGGER.warning("Could not read %s: %s", config_path, exc)
    if shutil.which("gh"):
        try:
            proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15, check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip(), "GitHub CLI (gh auth token)"
        except (OSError, subprocess.SubprocessError):
            pass
    # Older gh releases have no `gh auth token`; they store the token in hosts.yml.
    hosts_file = Path(os.environ.get("GH_CONFIG_DIR") or Path.home() / ".config" / "gh") / "hosts.yml"
    if hosts_file.is_file():
        try:
            match = re.search(r"^github\.com:\s*\n(?:[ \t]+.*\n)*?[ \t]+oauth_token:\s*(\S+)",
                              hosts_file.read_text(encoding="utf-8"), re.M)
            if match:
                return match.group(1), f"GitHub CLI login ({hosts_file})"
        except OSError:
            pass
    return None, "none (unauthenticated)"


def sanitized_command_line(argv: Sequence[str]) -> str:
    out, redact_next = [], False
    for arg in argv:
        if redact_next:
            out.append("***")
            redact_next = False
        elif arg == "--github-token":
            out.append(arg)
            redact_next = True
        elif arg.startswith("--github-token="):
            out.append("--github-token=***")
        else:
            out.append(arg)
    return " ".join(out)


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit singer-io tap repositories for security vulnerabilities and produce an Excel report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--org", default="singer-io", help="GitHub organization to audit")
    parser.add_argument("--tap", nargs="+", default=None, metavar="REPO",
                        help="Audit only these repositories (skips org-wide discovery)")
    parser.add_argument("--output", default="./security-audit", help="Output directory")
    parser.add_argument("--output-file", default=DEFAULT_REPORT_NAME, help="Workbook file name inside --output")
    parser.add_argument("--github-token", default=None, help="GitHub token (prefer GITHUB_TOKEN env var)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Optional JSON config holding a token")
    parser.add_argument("--include-archived", action="store_true", help="Also deep-scan archived/disabled repos")
    parser.add_argument("--skip-code-scanning", action="store_true", help="Do not fetch code-scanning (CodeQL) alerts")
    parser.add_argument("--local-repos-dir", default=None,
                        help="Read manifests from local clones (<dir>/<tap>/) instead of the GitHub API")
    parser.add_argument("--validate-csv", default=None,
                        help="GitHub org security overview CSV export to cross-check results against")
    parser.add_argument("--max-workers", type=int, default=4, help="Parallel taps")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout (seconds)")
    parser.add_argument("--retry-count", type=int, default=5, help="HTTP retries for 5xx/network errors")
    parser.add_argument("--retry-failed-passes", type=int, default=1,
                        help="Extra passes re-auditing taps that failed with transient errors")
    parser.add_argument("--fail-on-errors", action="store_true",
                        help="Exit non-zero if any tap failed or requires manual review")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def discover_repositories(args, github: GitHubClient) -> Tuple[List[RepoInfo], List[TapAuditResult], str]:
    """Returns (repos, results for explicitly requested repos that could not be fetched, discovery error)."""
    if args.tap:
        repos, missing = [], []
        for name in dict.fromkeys(args.tap):
            data, error = github.get_repo(args.org, name)
            if error or not data:
                LOGGER.error("Could not fetch %s/%s: %s", args.org, name, error)
                placeholder = TapDetector.classify(RepoInfo(name=name, full_name=f"{args.org}/{name}",
                                                            html_url=f"https://github.com/{args.org}/{name}"), explicit=True)
                result = TapAuditResult(placeholder)
                result.analysis_status = STATUS_FAILED if is_transient_error(error or "") else STATUS_MANUAL
                result.status_detail = "Repository could not be fetched"
                result.error = f"repository_lookup: {error}"
                result.transient = is_transient_error(error or "")
                result.dependabot_status = result.code_scanning_status = result.dependency_status = "Not checked"
                missing.append(result)
                continue
            repos.append(TapDetector.classify(repo_from_api(data, args.org), explicit=True))
        return repos, missing, ""
    raw, error = github.list_org_repos(args.org)
    repos = sorted((TapDetector.classify(repo_from_api(item, args.org)) for item in raw), key=lambda r: r.name.lower())
    LOGGER.info("Discovered %d repositories in '%s'%s", len(repos), args.org, f" (INCOMPLETE: {error})" if error else "")
    return repos, [], error or ""


def run_audits(auditor: SingerSecurityAuditor, taps: List[RepoInfo], max_workers: int, retry_passes: int) -> List[TapAuditResult]:
    results: Dict[str, TapAuditResult] = {}
    with ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="audit") as pool:
        futures = {pool.submit(auditor.audit_tap, repo): repo for repo in taps}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()  # audit_tap never raises
            results[result.repo.name] = result
            LOGGER.info("[%d/%d] %s: %s (%s)", i, len(taps), result.repo.name, result.analysis_status, result.status_detail)

    for attempt in range(2, retry_passes + 2):
        retry = [r.repo for r in results.values() if r.transient]
        if not retry:
            break
        LOGGER.info("Retry pass %d: re-auditing %d tap(s) that failed with transient errors", attempt - 1, len(retry))
        time.sleep(10)
        for repo in retry:
            result = auditor.audit_tap(repo, attempts=attempt)
            results[repo.name] = result
            LOGGER.info("[retry] %s: %s (%s)", repo.name, result.analysis_status, result.status_detail)
    return list(results.values())


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.verbose, output_dir / "audit_run.log")
    if not HAVE_OPENPYXL:
        LOGGER.error("openpyxl is required to write the Excel report. Install with: pip install -r requirements.txt")
        return 2
    started, started_clock = utc_now(), time.monotonic()

    token, token_source = load_token(args.github_token, Path(args.config))
    github = GitHubClient(token=token, timeout=args.timeout, retry_count=args.retry_count)
    login = "(unauthenticated)"
    if token:
        user, error = github.get_authenticated_user()
        if error:
            LOGGER.error("GitHub token from %s was rejected: %s", token_source, error)
            return 2
        login = (user or {}).get("login", "unknown")
        LOGGER.info("Authenticated to GitHub as %s (token from %s)", login, token_source)
    else:
        LOGGER.warning("No GitHub token found - running unauthenticated (60 requests/hour, Dependabot alerts unavailable). "
                       "Set GITHUB_TOKEN or log in with the gh CLI.")
    rate = github.get_rate_limit()
    if rate:
        LOGGER.info("GitHub core rate limit: %s/%s remaining", rate.get("remaining"), rate.get("limit"))

    threat_intel = ThreatIntelClient(timeout=args.timeout)
    threat_intel.load()
    pypi = PyPIClient(timeout=args.timeout)
    local_dir = Path(args.local_repos_dir) if args.local_repos_dir else None
    if local_dir is not None and not local_dir.is_dir():
        LOGGER.warning("--local-repos-dir %s does not exist; reading manifests from GitHub", local_dir)
        local_dir = None

    all_repos, lookup_failures, discovery_error = discover_repositories(args, github)
    taps = [r for r in all_repos if r.is_tap]
    non_taps = [r for r in all_repos if not r.is_tap]
    LOGGER.info("%d tap(s) to audit; %d non-tap repositories recorded and skipped", len(taps), len(non_taps))
    if rate and rate.get("remaining") is not None and rate["remaining"] < len(taps) * 10:
        LOGGER.warning("Only %s API requests remain (~%d needed); the run will pause when the limit resets",
                       rate["remaining"], len(taps) * 10)

    auditor = SingerSecurityAuditor(args.org, github, pypi, threat_intel, local_dir,
                                    args.include_archived, not args.skip_code_scanning)
    results = run_audits(auditor, taps, args.max_workers, args.retry_failed_passes) + lookup_failures
    results.sort(key=lambda r: r.repo.name.lower())

    vulnerabilities = [v for r in results for v in r.vulnerabilities]
    tickets = JiraTicketGenerator.generate(vulnerabilities)  # also stamps jira_ref onto vulnerabilities
    tap_summaries = [build_tap_summary(r, {t.ref: t.recommended_action for t in tickets}) for r in results]
    dependency_rows = [d for r in results for d in r.dependency_details]

    validation = None
    if args.validate_csv:
        validator = ExportValidator(Path(args.validate_csv), args.org)
        try:
            validator.load()
            validation = validator.compare(results, not args.skip_code_scanning, full_org_scope=not args.tap)
            LOGGER.info("Validation against %s: %s", args.validate_csv, validation[1])
        except (OSError, csv.Error) as exc:
            LOGGER.error("Could not read validation CSV %s: %s", args.validate_csv, exc)

    by_status = defaultdict(list)
    for r in results:
        by_status[r.analysis_status].append(r)
    analyzed = [r for r in results if r.scan_successful]
    accounted = sum(len(by_status[s]) for s in (STATUS_VULNERABLE, STATUS_CLEAN, STATUS_ARCHIVED, STATUS_MANUAL, STATUS_FAILED))
    expected_taps = len(taps) + len(lookup_failures)
    reconciled = accounted == expected_taps and not discovery_error

    finished = utc_now()
    final_rate = github.get_rate_limit()
    metadata = [
        ("Audit date (UTC)", started.strftime("%Y-%m-%d")),
        ("Started (UTC)", started), ("Finished (UTC)", finished),
        ("Duration", f"{time.monotonic() - started_clock:.0f} seconds"),
        ("GitHub organization", args.org),
        ("Scope", f"Explicit repositories: {', '.join(args.tap)}" if args.tap else "All repositories in the organization"),
        ("Archived repositories", "Deep-scanned" if args.include_archived else "Recorded but not deep-scanned"),
        ("Code scanning (CodeQL)", "Skipped" if args.skip_code_scanning else "Included"),
        ("Alert states included", "Open only"),
        ("Authenticated as", login), ("Token source", token_source),
        ("GitHub API requests made", github.request_count),
        ("GitHub rate limit remaining at end", f"{final_rate.get('remaining', '?')}/{final_rate.get('limit', '?')}"),
        ("CISA KEV catalog", threat_intel.status),
        ("Manifest source", f"Local clones in {local_dir}" if local_dir else "GitHub Contents API (default branch)"),
        ("Validation CSV", args.validate_csv or "Not provided"),
        ("Script version", __version__),
        ("Python / platform", f"{platform.python_version()} on {platform.platform()}"),
        ("Command line", sanitized_command_line(sys.argv if argv is None else ["audit_singer_security.py", *argv])),
        ("Data sources", "GitHub Dependabot alerts API; GitHub code-scanning alerts API; repository manifests; "
                         "open PRs/issues; PyPI JSON API; CISA KEV catalog"),
    ]
    reconciliation = [
        ("Repositories discovered", len(all_repos) + len(lookup_failures)),
        ("Non-tap repositories (recorded, skipped)", len(non_taps)),
        ("Singer taps identified", expected_taps),
        ("Taps successfully analyzed", len(analyzed)),
        ("  with open alerts", len(by_status[STATUS_VULNERABLE])),
        ("  without open alerts", len(by_status[STATUS_CLEAN])),
        ("Taps requiring manual review", len(by_status[STATUS_MANUAL])),
        ("Taps failed (see Not Audited)", len(by_status[STATUS_FAILED])),
        ("Taps archived/disabled (skipped by design)", len(by_status[STATUS_ARCHIVED])),
        ("Reconciliation", "OK - every tap accounted for" if reconciled
         else f"GAP - {accounted} of {expected_taps} taps accounted for; discovery error: {discovery_error or 'none'}"),
    ]
    if validation is not None:
        stats = validation[1]
        reconciliation.append(("Validation vs org export",
                               f"{stats['match']}/{stats['compared']} repo/tool pairs match; {stats['mismatch']} mismatch; "
                               f"{stats['not_audited']} export entries not deep-scanned"))

    report_path = ReportGenerator(output_dir / args.output_file).write(
        metadata=metadata, reconciliation=reconciliation, tap_summaries=tap_summaries,
        vulnerabilities=vulnerabilities, tickets=tickets, package_rows=build_package_summary(vulnerabilities),
        dependency_rows=dependency_rows, not_audited=build_not_audited(results),
        analysis_log=build_analysis_log(results, non_taps), validation=validation)

    sev = {s: sum(1 for v in vulnerabilities if v.severity == s) for s in SEVERITIES}
    print("=" * 60)
    print(f"SINGER.IO SECURITY AUDIT  ({started:%Y-%m-%d %H:%M} UTC, v{__version__})")
    print("=" * 60)
    for key, value in reconciliation:
        print(f"{key + ':':<46} {value}")
    print("\nOpen alerts by severity:")
    for s in SEVERITIES:
        print(f"  {s + ':':<44} {sev[s]}")
    print(f"{'Jira tickets recommended:':<46} {len(tickets)}")
    print(f"{'GitHub API requests:':<46} {github.request_count}")
    print(f"\nReport: {report_path}\nLog:    {output_dir / 'audit_run.log'}")

    if not reconciled:
        LOGGER.error("Audit has unresolved gaps (discovery error or unaccounted taps) - see Analysis Log.")
        return 1
    if args.fail_on_errors and (by_status[STATUS_FAILED] or by_status[STATUS_MANUAL]):
        LOGGER.error("%d tap(s) failed or need manual review (--fail-on-errors).",
                     len(by_status[STATUS_FAILED]) + len(by_status[STATUS_MANUAL]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
