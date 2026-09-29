#!/usr/bin/env python3
"""
Singer.io Security/Vulnerability Audit
=======================================

A single, self-contained script that audits every repository under the
``singer-io`` GitHub organization, identifies Singer taps, pulls GitHub's
Dependabot vulnerability data for each, cross-references declared Python
dependencies against PyPI + existing PRs/issues, and produces one Excel
workbook (multiple tabs/sheets inside a single ``.xlsx`` file) that is ready
to drive Jira ticket creation.

HOW TO RUN
----------
1. Install the required third-party packages::

       pip install requests packaging openpyxl

2. Provide a GitHub personal access token (strongly recommended: required to
   read Dependabot alerts at any real scale, and to avoid the 60 req/hour
   unauthenticated rate limit). Any ONE of the following works:

       # PowerShell
       $env:GITHUB_TOKEN = "ghp_xxx..."

       # bash/zsh
       export GITHUB_TOKEN="ghp_xxx..."

       # or pass it explicitly on the command line
       python audit_singer_security.py --github-token "ghp_xxx..."

       # or place it in tmp/configs/config.json (never commit this file):
       #   {"api_key": "ghp_xxx..."}

3. Run the audit (defaults to the whole ``singer-io`` org)::

       python audit_singer_security.py

   Useful variations::

       # Custom org / output folder
       python audit_singer_security.py --org singer-io --output ./security-audit

       # Only audit specific tap(s) - skips org-wide discovery (fast, for testing)
       python audit_singer_security.py --tap tap-ms-dynamics tap-mailjet

       # Also deep-scan archived/disabled repos (skipped by default)
       python audit_singer_security.py --include-archived

       # Tune concurrency / timeouts / retries
       python audit_singer_security.py --max-workers 8 --timeout 45 --retry-count 6

       # Verbose (debug) logging
       python audit_singer_security.py --verbose

OUTPUT
------
A single workbook, ``<output>/singer_tap_security_summary.xlsx``, containing
five tabs (sheets/pages): ``Tap Summary``, ``Vulnerabilities``,
``Jira Tickets``, ``Dependency Details``, and ``Analysis Log`` (the last one
proves that no repository was silently skipped).

WHAT DATA IT USES
-----------------
Vulnerability data comes from GitHub's Dependabot Alerts REST API (the same
structured data backing the org security overview / per-repo security
pages) rather than HTML scraping. Dependency versions are read from each
tap's ``setup.py`` / ``requirements*.txt`` / ``pyproject.toml`` / ``Pipfile``
(preferring an already-cloned checkout under ``./singer_tap_repos/<tap>`` if
present, falling back to the GitHub Contents API otherwise). Latest package
versions come from PyPI. CVSS/CWE/EPSS come directly from the GitHub
advisory payload.
"""
import argparse
import ast
import base64
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import requests
    from requests.adapters import HTTPAdapter
    from packaging.requirements import Requirement, InvalidRequirement
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version, InvalidVersion
except ImportError as exc:
    sys.stderr.write(f"ERROR: missing required package ({exc}). Install with: pip install requests packaging openpyxl\n")
    sys.exit(1)

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - very old urllib3
    from requests.packages.urllib3.util.retry import Retry

try:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.utils import get_column_letter
    HAVE_OPENPYXL = True
except ImportError:
    HAVE_OPENPYXL = False

LOGGER = logging.getLogger("singer_security_audit")

GITHUB_API = "https://api.github.com"
PYPI_API = "https://pypi.org/pypi/{name}/json"
KEV_FEED = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
DEFAULT_CONFIG_PATH = Path("tmp/configs/config.json")

MANIFEST_CANDIDATES = ["setup.py", "requirements.txt", "requirements-dev.txt", "dev-requirements.txt", "pyproject.toml", "Pipfile"]

SEVERITY_NORMALIZE = {"critical": "Critical", "high": "High", "moderate": "Medium", "medium": "Medium", "low": "Low"}
SEVERITY_SORT = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Informational": 4}
PRIORITY_SORT = {"P0 - Immediate": 0, "P1 - High": 1, "P2 - Normal": 2, "P3 - Low": 3, "P4 - Informational": 4}
SEVERITY_FILLS = {"Critical": "FFC7CE", "High": "FFD9B3", "Medium": "FFF2CC", "Low": "D9EAD3", "Informational": "E7E6E6"}
PRIORITY_FILLS = {
    "P0 - Immediate": "FFC7CE", "P1 - High": "FFD9B3", "P2 - Normal": "FFF2CC",
    "P3 - Low": "D9EAD3", "P4 - Informational": "E7E6E6",
}

_REQ_LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*(==|>=|<=|~=|!=|>|<)?\s*([A-Za-z0-9_.\-+!*]*)")


# ============================================================================
# Data model
# ============================================================================

@dataclass
class RepoInfo:
    name: str
    full_name: str
    html_url: str
    security_url: str
    archived: bool
    is_fork: bool
    disabled: bool
    default_branch: str
    description: str
    topics: List[str] = field(default_factory=list)
    is_tap: bool = False
    tap_detection_reason: str = ""


@dataclass
class DependencyDeclaration:
    tap_name: str
    package: str
    dep_type: str
    manifest: str
    current_version: str
    raw_specifier: str
    parse_note: str = ""


@dataclass
class PyPIInfo:
    status: str
    latest_version: Optional[str]
    error: Optional[str] = None


@dataclass
class RepoActivity:
    prs: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class Vulnerability:
    tap_name: str
    repository: str
    repository_url: str
    security_url: str
    vulnerability_id: str
    cve: str
    ghsa: str
    ghsa_url: str
    cve_url: str
    package: str
    dependency_type: str
    current_version: str
    vulnerable_version_range: str
    fixed_version: str
    severity: str
    cvss_score: Optional[float]
    cvss_vector: str
    cwe: str
    epss: str
    exploitability: str
    known_exploited: str
    description: str
    security_impact: str
    technical_impact: str
    recommended_action: str
    upgrade_required: str
    code_changes_required: str
    breaking_change_risk: str
    dependency_source: str
    evidence: str
    existing_issue: str
    existing_issue_url: str
    existing_pr: str
    existing_pr_url: str
    dependabot_pr: str
    remediation_already_available: str
    alert_state: str
    recommended_priority: str
    actionable: str
    recommended_jira_summary: str
    recommended_jira_description: str
    recommended_jira_priority: str
    suggested_acceptance_criteria: str
    status: str
    notes: str


@dataclass
class TapSummary:
    tap_name: str
    repository: str
    repository_url: str
    security_url: str
    active_archived: str
    security_status: str
    total_vulnerabilities: int
    critical: int
    high: int
    medium: int
    low: int
    informational: int
    actionable_issues: int
    highest_severity: str
    highest_cvss: Optional[float]
    recommended_priority: str
    primary_recommended_action: str
    jira_tickets_required: int
    analysis_status: str
    notes: str


@dataclass
class JiraTicket:
    tap: str
    jira_summary: str
    jira_priority: str
    severity: str
    cve_ghsa: str
    package: str
    current_version: str
    fixed_version: str
    recommended_action: str
    jira_description: str
    acceptance_criteria: str
    references: str


@dataclass
class DependencyDetailRow:
    tap: str
    dependency: str
    dependency_type: str
    current_version: str
    latest_pypi_version: str
    fixed_security_version: str
    vulnerable: str
    outdated: str
    direct_transitive: str
    manifest: str
    upgrade_required: str
    notes: str


@dataclass
class AnalysisLogEntry:
    repository: str
    discovered: str
    identified_as_tap: str
    security_page_accessible: str
    dependency_info_accessible: str
    vulnerability_scan_successful: str
    vulnerabilities_found: int
    analysis_status: str
    error: str
    timestamp: str


# ============================================================================
# GitHubClient - REST API: retries, pagination, rate-limit + error handling
# ============================================================================

class GitHubAPIError(Exception):
    pass


class GitHubClient:
    def __init__(self, token: Optional[str] = None, timeout: int = 30, retry_count: int = 5):
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        retries = Retry(
            total=retry_count, backoff_factor=1.5, status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"], raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries, pool_maxsize=32))
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "singer-tap-security-auditor",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.session.headers.update(headers)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        full_url = url if url.startswith("http") else f"{GITHUB_API}{url}"
        attempt = 0
        max_rate_limit_retries = 6
        while True:
            try:
                resp = self.session.request(method, full_url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                raise GitHubAPIError(f"Network error calling GitHub API ({method} {url}): {exc}") from exc
            if resp.status_code in (403, 429) and self._is_rate_limited(resp):
                attempt += 1
                if attempt > max_rate_limit_retries:
                    raise GitHubAPIError(f"Exceeded rate-limit retry budget calling {method} {url}")
                wait_seconds = self._rate_limit_wait(resp)
                LOGGER.warning("Rate limited (status %s) on %s %s; sleeping %.0fs (attempt %d/%d)",
                               resp.status_code, method, url, wait_seconds, attempt, max_rate_limit_retries)
                time.sleep(wait_seconds)
                continue
            return resp

    @staticmethod
    def _is_rate_limited(resp: requests.Response) -> bool:
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            return True
        if resp.headers.get("Retry-After"):
            return True
        return "rate limit" in resp.text.lower() if resp.text else False

    @staticmethod
    def _rate_limit_wait(resp: requests.Response) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(1.0, float(retry_after) + 1)
            except ValueError:
                pass
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return max(1.0, float(reset) - time.time() + 1)
            except ValueError:
                pass
        return 30.0

    def get(self, url: str, **kwargs) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def get_json(self, url: str, params: Optional[dict] = None) -> Tuple[Optional[dict], Optional[str]]:
        return self._interpret(self.get(url, params=params), url)

    def get_paginated(self, url: str, params: Optional[dict] = None) -> Tuple[List[dict], Optional[str]]:
        items: List[dict] = []
        next_url = url
        next_params = dict(params or {}, per_page=100)
        while next_url:
            resp = self.get(next_url, params=next_params)
            next_params = None
            data, error = self._interpret(resp, next_url)
            if error:
                return items, error
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict) and "items" in data:
                items.extend(data["items"])
            else:
                return items, f"Unexpected response shape from {next_url}"
            next_url = resp.links.get("next", {}).get("url")
        return items, None

    @staticmethod
    def _interpret(resp: requests.Response, url: str) -> Tuple[Optional[dict], Optional[str]]:
        if resp.status_code == 401:
            return None, "authentication_error: invalid or missing GitHub token"
        if resp.status_code == 403:
            if "rate limit" in (resp.text or "").lower():
                return None, "rate_limited: exceeded retry budget"
            return None, "forbidden: insufficient permissions to access this resource"
        if resp.status_code == 404:
            return None, "not_found"
        if resp.status_code >= 500:
            return None, f"server_error: HTTP {resp.status_code}"
        if resp.status_code >= 400:
            return None, f"http_{resp.status_code}: {resp.text[:200]}"
        try:
            return resp.json(), None
        except ValueError as exc:
            return None, f"invalid_json: {exc}"

    def list_org_repos(self, org: str) -> Tuple[List[dict], Optional[str]]:
        return self.get_paginated(f"/orgs/{org}/repos", params={"type": "all"})

    def get_repo(self, owner: str, repo: str) -> Tuple[Optional[dict], Optional[str]]:
        return self.get_json(f"/repos/{owner}/{repo}")

    def get_dependabot_alerts(self, owner: str, repo: str) -> Tuple[List[dict], Optional[str]]:
        return self.get_paginated(f"/repos/{owner}/{repo}/dependabot/alerts", params={"state": "open"})

    def get_file_contents(self, owner: str, repo: str, path: str) -> Tuple[Optional[str], Optional[str]]:
        data, error = self.get_json(f"/repos/{owner}/{repo}/contents/{path}")
        if error:
            return None, error
        if not data or "content" not in data:
            return None, "not_found"
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace"), None
        except Exception as exc:  # pragma: no cover
            return None, f"decode_error: {exc}"

    def list_pulls(self, owner: str, repo: str, state: str = "all") -> Tuple[List[dict], Optional[str]]:
        return self.get_paginated(f"/repos/{owner}/{repo}/pulls", params={"state": state})

    def list_issues(self, owner: str, repo: str, state: str = "all") -> Tuple[List[dict], Optional[str]]:
        return self.get_paginated(f"/repos/{owner}/{repo}/issues", params={"state": state})


# ============================================================================
# PyPIClient - latest-version lookups with retries + caching
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
        self.session.headers.update({"User-Agent": "singer-tap-security-auditor"})

    def get_latest_version(self, package_name: str) -> PyPIInfo:
        key = package_name.lower()
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = self._fetch(package_name)
        with self._lock:
            self._cache[key] = result
        return result

    def _fetch(self, package_name: str) -> PyPIInfo:
        url = PYPI_API.format(name=package_name)
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            return PyPIInfo("error", None, str(exc))
        if resp.status_code == 404:
            return PyPIInfo("not_found", None, "Package not found on PyPI")
        if resp.status_code != 200:
            return PyPIInfo("error", None, f"PyPI returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            return PyPIInfo("error", None, f"Invalid JSON from PyPI: {exc}")
        latest = self._select_latest_version(data)
        if latest is None:
            return PyPIInfo("error", None, "No usable releases found on PyPI")
        return PyPIInfo("ok", latest)

    @staticmethod
    def _select_latest_version(data: dict) -> Optional[str]:
        releases = data.get("releases") or {}
        candidates = []
        for version_str, files in releases.items():
            if not files or all(f.get("yanked") for f in files):
                continue
            try:
                candidates.append(Version(version_str))
            except InvalidVersion:
                continue
        stable = [v for v in candidates if not v.is_prerelease and not v.is_devrelease]
        pool = stable if stable else candidates
        if pool:
            return str(max(pool))
        return (data.get("info") or {}).get("version") or None


# ============================================================================
# ThreatIntelClient - best-effort CISA KEV lookup (EPSS comes from GitHub directly)
# ============================================================================

class ThreatIntelClient:
    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._kev_ids = None
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "singer-tap-security-auditor"})

    def is_known_exploited(self, cve_id: str) -> str:
        if not cve_id:
            return "Unknown"
        ids = self._load_kev()
        return "Unknown" if ids is None else ("Yes" if cve_id in ids else "No")

    def _load_kev(self):
        with self._lock:
            if self._kev_ids is not None:
                return self._kev_ids
        try:
            resp = self.session.get(KEV_FEED, timeout=self.timeout)
            if resp.status_code != 200:
                LOGGER.warning("Could not fetch CISA KEV feed: HTTP %s", resp.status_code)
                return None
            vulns = resp.json().get("vulnerabilities") or []
            ids = {v.get("cveID") for v in vulns if v.get("cveID")}
        except (requests.RequestException, ValueError) as exc:
            LOGGER.warning("Could not fetch CISA KEV feed: %s", exc)
            return None
        with self._lock:
            self._kev_ids = ids
        return ids


# ============================================================================
# RepositoryDiscovery + TapDetector
# ============================================================================

class RepositoryDiscovery:
    def __init__(self, client: GitHubClient, org: str):
        self.client = client
        self.org = org

    def discover(self) -> Tuple[List[RepoInfo], str]:
        raw, error = self.client.list_org_repos(self.org)
        repos = []
        for item in raw:
            name = item.get("name", "")
            repos.append(RepoInfo(
                name=name, full_name=item.get("full_name", f"{self.org}/{name}"),
                html_url=item.get("html_url", f"https://github.com/{self.org}/{name}"),
                security_url=f"https://github.com/{self.org}/{name}/security",
                archived=bool(item.get("archived")), is_fork=bool(item.get("fork")),
                disabled=bool(item.get("disabled")), default_branch=item.get("default_branch", "master"),
                description=item.get("description") or "", topics=item.get("topics") or [],
            ))
        repos.sort(key=lambda r: r.name.lower())
        LOGGER.info("Discovered %d repositories in org '%s'%s", len(repos), self.org, f" (partial: {error})" if error else "")
        return repos, (error or "")


class TapDetector:
    """A repo is treated as a "tap" when its name follows the ``tap-*``
    naming convention used across ``singer-io``, or carries a ``singer-tap``
    topic. Everything else is still recorded in the inventory/Analysis Log,
    just excluded from the tap-focused report sheets.
    """

    @staticmethod
    def classify(repo: RepoInfo) -> RepoInfo:
        if repo.name.startswith("tap-"):
            repo.is_tap, repo.tap_detection_reason = True, "name starts with 'tap-'"
        elif "singer-tap" in (repo.topics or []):
            repo.is_tap, repo.tap_detection_reason = True, "tagged with 'singer-tap' topic"
        else:
            repo.is_tap, repo.tap_detection_reason = False, "does not match tap naming/topic convention"
        return repo


# ============================================================================
# SecurityAnalyzer - Dependabot alerts + repo PR/issue activity
# ============================================================================

class SecurityAnalyzer:
    def __init__(self, client: GitHubClient):
        self.client = client

    def get_dependabot_alerts(self, owner: str, repo: str) -> Tuple[List[dict], str]:
        alerts, error = self.client.get_dependabot_alerts(owner, repo)
        return alerts, (error or "")

    def get_repo_activity(self, owner: str, repo: str) -> RepoActivity:
        prs, pr_error = self.client.list_pulls(owner, repo, state="all")
        issues, issue_error = self.client.list_issues(owner, repo, state="all")
        issues = [i for i in issues if "pull_request" not in i]  # /issues also returns PRs
        error = None
        if pr_error and pr_error != "not_found":
            error = f"prs: {pr_error}"
        if issue_error and issue_error != "not_found":
            error = f"{error}; issues: {issue_error}" if error else f"issues: {issue_error}"
        return RepoActivity(prs=prs, issues=issues, error=error)


# ============================================================================
# Static setup.py dependency parser (AST-based; never executes tap code)
# ============================================================================

class SetupPyParseError(Exception):
    pass


def _resolve_name(node: ast.AST, symtable: Dict[str, ast.AST], seen: Optional[set] = None) -> ast.AST:
    seen = seen if seen is not None else set()
    if isinstance(node, ast.Name):
        if node.id in seen:
            raise SetupPyParseError(f"Circular reference resolving '{node.id}'")
        if node.id not in symtable:
            raise SetupPyParseError(f"Could not resolve variable '{node.id}'")
        seen.add(node.id)
        return _resolve_name(symtable[node.id], symtable, seen)
    return node


def _extract_string_list(node: ast.AST, symtable: Dict[str, ast.AST]) -> List[str]:
    node = _resolve_name(node, symtable)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = []
        for element in node.elts:
            try:
                resolved = _resolve_name(element, symtable) if isinstance(element, ast.Name) else element
            except SetupPyParseError:
                continue
            if isinstance(resolved, ast.Constant) and isinstance(resolved.value, str):
                items.append(resolved.value)
        return items
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _extract_string_list(node.left, symtable) + _extract_string_list(node.right, symtable)
    raise SetupPyParseError(f"Unsupported dependency list expression: {ast.dump(node)[:80]}")


def _extract_extras_dict(node: ast.AST, symtable: Dict[str, ast.AST]) -> Dict[str, List[str]]:
    node = _resolve_name(node, symtable)
    if not isinstance(node, ast.Dict):
        raise SetupPyParseError("extras_require is not a dict literal")
    result: Dict[str, List[str]] = {}
    for key_node, value_node in zip(node.keys, node.values):
        if key_node is None or value_node is None:
            continue
        try:
            resolved_key = _resolve_name(key_node, symtable) if isinstance(key_node, ast.Name) else key_node
        except SetupPyParseError:
            continue
        if not (isinstance(resolved_key, ast.Constant) and isinstance(resolved_key.value, str)):
            continue
        try:
            result[resolved_key.value] = _extract_string_list(value_node, symtable)
        except SetupPyParseError:
            continue
    return result


def _find_setup_call(tree: ast.AST) -> Optional[ast.Call]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "setup":
                return node
            if isinstance(func, ast.Attribute) and func.attr == "setup":
                return node
    return None


def _get_keyword_value(call: ast.Call, name: str) -> Optional[ast.AST]:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _extract_baseline_version(specifier: SpecifierSet) -> Optional[str]:
    """Best-effort 'anchor' version referenced by a specifier set: prefers an
    exact pin, falls back to the highest lower/compatible bound."""
    exact, lower_bound = None, None
    for spec in specifier:
        try:
            version = Version(spec.version)
        except InvalidVersion:
            continue
        if spec.operator == "==":
            exact = version
        elif spec.operator in (">=", "~=", ">") and (lower_bound is None or version > lower_bound):
            lower_bound = version
    chosen = exact if exact is not None else lower_bound
    return str(chosen) if chosen is not None else None


def parse_setup_py_text(source: str, tap_name: str) -> List[DependencyDeclaration]:
    """Statically parses setup.py source text (never executes it)."""
    if source.startswith("\ufeff"):
        source = source[1:]
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
        raise SetupPyParseError("No setup(...) call found in setup.py")

    declarations: List[DependencyDeclaration] = []

    install_requires_node = _get_keyword_value(setup_call, "install_requires")
    if install_requires_node is not None:
        try:
            for raw in _extract_string_list(install_requires_node, symtable):
                declarations.append(_build_declaration(tap_name, "Direct-Runtime", "setup.py", raw))
        except SetupPyParseError as exc:
            LOGGER.warning("%s: could not fully parse install_requires: %s", tap_name, exc)

    extras_node = _get_keyword_value(setup_call, "extras_require")
    if extras_node is not None:
        try:
            for entries in _extract_extras_dict(extras_node, symtable).values():
                for raw in entries:
                    declarations.append(_build_declaration(tap_name, "Direct-Development", "setup.py", raw))
        except SetupPyParseError as exc:
            LOGGER.warning("%s: could not fully parse extras_require: %s", tap_name, exc)

    return declarations


def _build_declaration(tap_name: str, dep_type: str, manifest: str, raw: str) -> DependencyDeclaration:
    name, version, parse_note = raw.strip(), "", ""
    try:
        requirement = Requirement(raw)
        name = requirement.name
        version = _extract_baseline_version(requirement.specifier) or ""
    except InvalidRequirement as exc:
        parse_note = f"Could not parse requirement string {raw!r}: {exc}"
    return DependencyDeclaration(
        tap_name=tap_name, package=name, dep_type=dep_type, manifest=manifest,
        current_version=version, raw_specifier=raw, parse_note=parse_note,
    )


# ============================================================================
# DependencyAnalyzer - discovers declared dependencies from manifest files
# ============================================================================

class DependencyAnalyzer:
    def __init__(self, client: GitHubClient, org: str, local_repos_dir: Optional[Path] = None):
        self.client = client
        self.org = org
        self.local_repos_dir = local_repos_dir

    def analyze(self, tap_name: str) -> Tuple[List[DependencyDeclaration], str]:
        declarations: List[DependencyDeclaration] = []
        errors: List[str] = []
        found_any_manifest = False

        for manifest in MANIFEST_CANDIDATES:
            text, _source_note = self._read_manifest(tap_name, manifest)
            if text is None:
                continue
            found_any_manifest = True
            try:
                if manifest == "setup.py":
                    declarations.extend(parse_setup_py_text(text, tap_name))
                elif manifest == "pyproject.toml":
                    declarations.extend(self._parse_pyproject(tap_name, manifest, text))
                elif manifest == "Pipfile":
                    declarations.extend(self._parse_pipfile(tap_name, manifest, text))
                else:
                    dep_type = "Direct-Development" if "dev" in manifest else "Direct-Runtime"
                    declarations.extend(self._parse_requirements_txt(tap_name, manifest, text, dep_type))
            except Exception as exc:  # pragma: no cover - never let a bad manifest kill the tap
                errors.append(f"{manifest}: parse_error: {exc}")

        if not found_any_manifest:
            return [], "no_manifest_found: none of setup.py/requirements*.txt/pyproject.toml/Pipfile reachable"
        return declarations, "; ".join(errors)

    def _read_manifest(self, tap_name: str, manifest: str) -> Tuple[Optional[str], str]:
        if self.local_repos_dir is not None:
            local_path = self.local_repos_dir / tap_name / manifest
            if local_path.is_file():
                try:
                    return local_path.read_text(encoding="utf-8", errors="replace"), "local_clone"
                except OSError as exc:
                    LOGGER.debug("%s: could not read local %s: %s", tap_name, manifest, exc)
        text, error = self.client.get_file_contents(self.org, tap_name, manifest)
        return (None, error) if error else (text, "github_api")

    def _parse_requirements_txt(self, tap_name: str, manifest: str, text: str, dep_type: str) -> List[DependencyDeclaration]:
        declarations = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            line = line.split("#", 1)[0].strip()
            match = _REQ_LINE_RE.match(line)
            if not match:
                continue
            name, op, version = match.groups()
            declarations.append(DependencyDeclaration(
                tap_name=tap_name, package=name.strip(), dep_type=dep_type, manifest=manifest,
                current_version=version if (op == "==" and version) else "", raw_specifier=line,
            ))
        return declarations

    def _parse_pyproject(self, tap_name: str, manifest: str, text: str) -> List[DependencyDeclaration]:
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
            except ImportError:
                return []
        try:
            data = tomllib.loads(text)
        except Exception as exc:
            LOGGER.warning("%s: could not parse pyproject.toml: %s", tap_name, exc)
            return []

        declarations = []
        poetry_deps = (data.get("tool", {}).get("poetry", {}).get("dependencies", {})) or {}
        for name, spec in poetry_deps.items():
            if name.lower() == "python":
                continue
            version = spec if isinstance(spec, str) else (spec.get("version", "") if isinstance(spec, dict) else "")
            declarations.append(DependencyDeclaration(
                tap_name=tap_name, package=name, dep_type="Direct-Runtime", manifest=manifest,
                current_version=str(version).lstrip("^~=<>! "), raw_specifier=f"{name} {version}",
            ))
        dev_deps = (data.get("tool", {}).get("poetry", {}).get("group", {}).get("dev", {}).get("dependencies", {})) or {}
        for name, spec in dev_deps.items():
            version = spec if isinstance(spec, str) else (spec.get("version", "") if isinstance(spec, dict) else "")
            declarations.append(DependencyDeclaration(
                tap_name=tap_name, package=name, dep_type="Direct-Development", manifest=manifest,
                current_version=str(version).lstrip("^~=<>! "), raw_specifier=f"{name} {version}",
            ))
        for raw in data.get("project", {}).get("dependencies", []) or []:
            match = _REQ_LINE_RE.match(raw)
            if not match:
                continue
            name, op, version = match.groups()
            declarations.append(DependencyDeclaration(
                tap_name=tap_name, package=name, dep_type="Direct-Runtime", manifest=manifest,
                current_version=version if op == "==" else "", raw_specifier=raw,
            ))
        return declarations

    def _parse_pipfile(self, tap_name: str, manifest: str, text: str) -> List[DependencyDeclaration]:
        try:
            import tomllib
            data = tomllib.loads(text)
        except Exception:
            return []  # Pipfile is TOML-like but not always strictly valid TOML; best effort only.
        declarations = []
        for section, dep_type in (("packages", "Direct-Runtime"), ("dev-packages", "Direct-Development")):
            for name, spec in (data.get(section, {}) or {}).items():
                version = spec if isinstance(spec, str) else ""
                declarations.append(DependencyDeclaration(
                    tap_name=tap_name, package=name, dep_type=dep_type, manifest=manifest,
                    current_version=str(version).lstrip("=<>! "), raw_specifier=f"{name} {version}",
                ))
        return declarations


# ============================================================================
# VulnerabilityAnalyzer - combines Dependabot alert + local dependency/PyPI/
# activity context into fully analyzed Vulnerability records
# ============================================================================

class VulnerabilityAnalyzer:
    def __init__(self, pypi: PyPIClient, threat_intel: ThreatIntelClient):
        self.pypi = pypi
        self.threat_intel = threat_intel

    def analyze_alert(
        self, tap_name: str, repo_url: str, security_url: str, alert: dict,
        declarations_by_package: Dict[str, DependencyDeclaration], activity: RepoActivity,
    ) -> Vulnerability:
        advisory = alert.get("security_advisory") or {}
        sec_vuln = alert.get("security_vulnerability") or {}
        dependency = alert.get("dependency") or {}
        package_name = (sec_vuln.get("package") or {}).get("name") or (dependency.get("package") or {}).get("name", "unknown")

        ghsa = advisory.get("ghsa_id", "")
        cve = advisory.get("cve_id") or ""
        severity = SEVERITY_NORMALIZE.get((sec_vuln.get("severity") or advisory.get("severity") or "").lower(), "Informational")

        cvss_score, cvss_vector = self._extract_cvss(advisory)
        cwe = ", ".join(c.get("cwe_id", "") for c in (advisory.get("cwes") or []) if c.get("cwe_id"))
        vulnerable_range = sec_vuln.get("vulnerable_version_range", "")
        fixed_version = (sec_vuln.get("first_patched_version") or {}).get("identifier", "")

        manifest_path = dependency.get("manifest_path", "")
        scope = (dependency.get("scope") or "").lower()
        declaration = declarations_by_package.get(package_name.lower())
        dep_type = self._dependency_type(declaration, scope)
        current_version = declaration.current_version if declaration else ""

        breaking_risk = self._breaking_change_risk(current_version, fixed_version)
        code_changes = self._code_changes_required(dep_type, breaking_risk)

        existing_issue, issue_url, existing_pr, pr_url, dependabot_pr = self._find_existing_remediation(activity, package_name, ghsa)
        remediation_available = "Yes" if (existing_pr == "Yes" or dependabot_pr == "Yes") else "No"

        epss = self._extract_epss(advisory) or ""
        known_exploited = self.threat_intel.is_known_exploited(cve) if cve else "Unknown"
        exploitability = self._exploitability_text(known_exploited, epss)

        actionable = "Yes" if alert.get("state") == "open" else "No"
        priority = self._recommended_priority(severity, scope, remediation_available, actionable)
        recommended_action = self._recommended_action(package_name, current_version, fixed_version, dep_type)
        summary, description, acceptance = self._jira_content(
            tap_name, package_name, current_version, vulnerable_range, fixed_version,
            cve, ghsa, severity, cvss_score, advisory, recommended_action, dep_type, breaking_risk,
        )

        return Vulnerability(
            tap_name=tap_name, repository=f"singer-io/{tap_name}", repository_url=repo_url, security_url=security_url,
            vulnerability_id=ghsa or alert.get("html_url", f"alert-{alert.get('number', '')}"),
            cve=cve, ghsa=ghsa, ghsa_url=f"https://github.com/advisories/{ghsa}" if ghsa else "",
            cve_url=f"https://nvd.nist.gov/vuln/detail/{cve}" if cve else "",
            package=package_name, dependency_type=dep_type, current_version=current_version or "(unresolved)",
            vulnerable_version_range=vulnerable_range, fixed_version=fixed_version or "(none published)",
            severity=severity, cvss_score=cvss_score, cvss_vector=cvss_vector, cwe=cwe, epss=epss,
            exploitability=exploitability, known_exploited=known_exploited,
            description=advisory.get("description", "") or advisory.get("summary", ""),
            security_impact=advisory.get("summary", ""), technical_impact=self._technical_impact(scope, dep_type),
            recommended_action=recommended_action,
            upgrade_required="Yes" if fixed_version else "No fixed version published",
            code_changes_required=code_changes, breaking_change_risk=breaking_risk,
            dependency_source=manifest_path or (declaration.manifest if declaration else "unknown"),
            evidence=alert.get("html_url", ""), existing_issue=existing_issue, existing_issue_url=issue_url,
            existing_pr=existing_pr, existing_pr_url=pr_url, dependabot_pr=dependabot_pr,
            remediation_already_available=remediation_available, alert_state=alert.get("state", "unknown"),
            recommended_priority=priority, actionable=actionable,
            recommended_jira_summary=summary, recommended_jira_description=description,
            recommended_jira_priority=priority, suggested_acceptance_criteria=acceptance,
            status="Open" if actionable == "Yes" else f"Not actionable ({alert.get('state')})",
            notes="Remediation already available via existing PR" if remediation_available == "Yes" else "",
        )

    @staticmethod
    def _extract_cvss(advisory: dict):
        # An unrated axis comes back as score 0.0 with a null vector rather
        # than being omitted, so an entry is only trusted when it has a vector.
        severities = advisory.get("cvss_severities") or {}
        for key in ("cvss_v4", "cvss_v3"):
            entry = severities.get(key) or {}
            if entry.get("score") is not None and entry.get("vector_string"):
                return float(entry["score"]), entry.get("vector_string", "")
        legacy = advisory.get("cvss") or {}
        if legacy.get("score") is not None and legacy.get("vector_string"):
            return float(legacy["score"]), legacy.get("vector_string", "")
        return None, ""

    @staticmethod
    def _extract_epss(advisory: dict) -> str:
        epss = advisory.get("epss") or {}
        percentage = epss.get("percentage")
        if percentage is None:
            return ""
        percentile = epss.get("percentile")
        pct_text = f"{percentile * 100:.1f}th pct" if percentile is not None else "pct unknown"
        return f"{percentage:.4f} ({pct_text})"

    @staticmethod
    def _dependency_type(declaration: Optional[DependencyDeclaration], scope: str) -> str:
        if declaration is not None:
            return declaration.dep_type
        return "Transitive-Development (not directly declared)" if scope == "development" else "Transitive (not directly declared)"

    @staticmethod
    def _breaking_change_risk(current_version: str, fixed_version: str) -> str:
        if not current_version or not fixed_version:
            return "Unknown (version(s) not resolved)"
        try:
            cur, fixed = Version(current_version), Version(fixed_version)
        except InvalidVersion:
            return "Unknown (unparsable version)"
        if fixed.major > cur.major:
            return "High (major version bump)"
        if fixed.minor > cur.minor:
            return "Medium (minor version bump)"
        return "Low (patch-level bump)"

    @staticmethod
    def _code_changes_required(dep_type: str, breaking_risk: str) -> str:
        if dep_type.startswith("Transitive"):
            return "Unclear - not a direct dependency; verify via re-resolving/locking, may just need a transitive bump"
        if breaking_risk.startswith("High"):
            return "Likely - review changelog for the major version bump"
        if breaking_risk.startswith("Medium"):
            return "Possibly - review changelog for the minor version bump"
        return "Unlikely - patch-level bump"

    @staticmethod
    def _technical_impact(scope: str, dep_type: str) -> str:
        if scope == "development" or "Development" in dep_type:
            return "Limited to development/test tooling; not part of the tap's production runtime"
        return "Affects production/runtime code path used when the tap executes"

    @staticmethod
    def _find_existing_remediation(activity: RepoActivity, package_name: str, ghsa: str):
        existing_issue, issue_url = "No", ""
        existing_pr, pr_url = "No", ""
        dependabot_pr = "No"
        needle = package_name.lower()
        for issue in activity.issues:
            title = (issue.get("title") or "").lower()
            if needle in title or (ghsa and ghsa.lower() in title):
                existing_issue, issue_url = "Yes", issue.get("html_url", "")
                break
        for pr in activity.prs:
            title = (pr.get("title") or "").lower()
            author = ((pr.get("user") or {}).get("login") or "").lower()
            if needle in title or (ghsa and ghsa.lower() in title):
                existing_pr, pr_url = "Yes", pr.get("html_url", "")
                if "dependabot" in author:
                    dependabot_pr = "Yes"
                break
        return existing_issue, issue_url, existing_pr, pr_url, dependabot_pr

    @staticmethod
    def _exploitability_text(known_exploited: str, epss: str) -> str:
        if known_exploited == "Yes":
            return "Actively exploited (listed in CISA KEV catalog)"
        return f"EPSS {epss}" if epss else "No known-exploited or EPSS data available"

    @staticmethod
    def _recommended_priority(severity: str, scope: str, remediation_available: str, actionable: str) -> str:
        if actionable != "Yes":
            return "P4 - Informational"
        if remediation_available == "Yes":
            return "P3 - Low"  # just needs merging an existing fix
        if severity == "Critical":
            return "P0 - Immediate"
        if severity == "High":
            return "P1 - High" if scope != "development" else "P2 - Normal"
        if severity == "Medium":
            return "P2 - Normal"
        if severity == "Low":
            return "P3 - Low"
        return "P4 - Informational"

    @staticmethod
    def _recommended_action(package: str, current_version: str, fixed_version: str, dep_type: str) -> str:
        current = current_version or "an unresolved/unpinned version"
        if not fixed_version or fixed_version == "(none published)":
            return (f"No fixed version has been published upstream for `{package}` yet; "
                     f"monitor the advisory and re-run the audit, or evaluate removing/replacing the dependency.")
        if dep_type.startswith("Transitive"):
            return (f"`{package}` is a transitive dependency (not declared directly). Identify the direct "
                     f"dependency that pulls it in, regenerate the lock/pin, and confirm the resolved version "
                     f"is >= {fixed_version}, then run the tap's unit/integration/tap-tester test suite.")
        return (f"Upgrade `{package}` from {current} to >= {fixed_version} in its manifest, "
                f"then run the tap's unit/integration/tap-tester test suite.")

    @staticmethod
    def _jira_content(tap_name, package, current_version, vulnerable_range, fixed_version, cve, ghsa,
                       severity, cvss_score, advisory, recommended_action, dep_type, breaking_risk):
        summary = f"[{tap_name}] Upgrade vulnerable `{package}` dependency" + (
            f" to >= {fixed_version}" if fixed_version and fixed_version != "(none published)" else ""
        )
        refs = [r.get("url", "") for r in (advisory.get("references") or []) if r.get("url")]
        references = "\n".join(refs) if refs else "N/A"
        description = (
            f"h2. Problem\nA security vulnerability was reported against a dependency used by *{tap_name}*.\n\n"
            f"h2. Affected tap\n{tap_name} (singer-io/{tap_name})\n\n"
            f"h2. Vulnerable dependency\n{package} ({dep_type})\n\n"
            f"h2. Current version\n{current_version or '(unresolved - verify manually)'}\n\n"
            f"h2. Vulnerable versions\n{vulnerable_range or 'N/A'}\n\n"
            f"h2. Fixed version\n{fixed_version or '(none published yet)'}\n\n"
            f"h2. CVE / GHSA\n{cve or 'N/A'} / {ghsa or 'N/A'}\n\n"
            f"h2. Severity\n{severity}" + (f" (CVSS {cvss_score})" if cvss_score is not None else "") + "\n\n"
            f"h2. Security impact\n{advisory.get('summary', 'N/A')}\n\n"
            f"h2. Recommended remediation\n{recommended_action}\n\n"
            f"h2. Expected code changes\n"
            f"{('Dependency manifest version bump' if not dep_type.startswith('Transitive') else 'Regenerate lock file / bump the direct dependency that pulls this in')}; "
            f"breaking-change risk assessed as: {breaking_risk}.\n\n"
            f"h2. Testing required\nRun the tap's unit tests, integration tests, and tap-tester checks after the upgrade.\n\n"
            f"h2. Acceptance criteria\n* Dependency is upgraded to a non-vulnerable version\n* Full test suite passes\n"
            f"* No new Dependabot alert is raised for this package\n\nh2. References\n{references}"
        )
        acceptance = (
            f"Dependency `{package}` upgraded to a version outside {vulnerable_range or 'the vulnerable range'}; "
            f"unit/integration/tap-tester tests pass; Dependabot alert for {ghsa or cve or package} is resolved."
        )
        return summary, description, acceptance


# ============================================================================
# JiraTicketGenerator
# ============================================================================

class JiraTicketGenerator:
    @staticmethod
    def generate(vulnerabilities: List[Vulnerability]) -> List[JiraTicket]:
        tickets = []
        for vuln in vulnerabilities:
            if vuln.actionable != "Yes" or vuln.remediation_already_available == "Yes":
                continue  # not actionable, or an existing PR already covers it - avoid a duplicate ticket
            references = f"{vuln.ghsa_url}\n{vuln.cve_url}\n{vuln.security_url}".strip()
            tickets.append(JiraTicket(
                tap=vuln.tap_name, jira_summary=vuln.recommended_jira_summary, jira_priority=vuln.recommended_jira_priority,
                severity=vuln.severity, cve_ghsa=f"{vuln.cve or 'N/A'} / {vuln.ghsa or 'N/A'}", package=vuln.package,
                current_version=vuln.current_version, fixed_version=vuln.fixed_version,
                recommended_action=vuln.recommended_action, jira_description=vuln.recommended_jira_description,
                acceptance_criteria=vuln.suggested_acceptance_criteria, references=references,
            ))
        return tickets


# ============================================================================
# AuditLogger - guarantees no repository is ever silently skipped
# ============================================================================

class AuditLogger:
    def __init__(self):
        self._entries: List[AnalysisLogEntry] = []
        self._lock = threading.Lock()

    def record(self, repository: str, discovered: bool, identified_as_tap: bool, security_page_accessible: bool,
               dependency_info_accessible: bool, vulnerability_scan_successful: bool, vulnerabilities_found: int,
               analysis_status: str, error: str = "") -> None:
        import datetime
        entry = AnalysisLogEntry(
            repository=repository, discovered="Yes" if discovered else "No",
            identified_as_tap="Yes" if identified_as_tap else "No",
            security_page_accessible="Yes" if security_page_accessible else "No",
            dependency_info_accessible="Yes" if dependency_info_accessible else "No",
            vulnerability_scan_successful="Yes" if vulnerability_scan_successful else "No",
            vulnerabilities_found=vulnerabilities_found, analysis_status=analysis_status, error=error,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._entries.append(entry)

    def entries(self) -> List[AnalysisLogEntry]:
        with self._lock:
            return list(self._entries)


# ============================================================================
# ReportGenerator - single Excel workbook, 5 tabs/sheets
# ============================================================================

def _vulnerability_sort_key(v: Vulnerability):
    return (v.tap_name.lower(), SEVERITY_SORT.get(v.severity, 9), PRIORITY_SORT.get(v.recommended_priority, 9),
            -(v.cvss_score or 0.0), v.vulnerability_id)


class ReportGenerator:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write(self, tap_summaries, vulnerabilities, jira_tickets, dependency_details, analysis_log) -> Path:
        if not HAVE_OPENPYXL:
            raise RuntimeError("openpyxl is required to write the report. Install with: pip install openpyxl")

        vulnerabilities = sorted(vulnerabilities, key=_vulnerability_sort_key)
        tap_summaries = sorted(tap_summaries, key=lambda t: t.tap_name.lower())
        jira_tickets = sorted(jira_tickets, key=lambda j: (j.tap.lower(), PRIORITY_SORT.get(j.jira_priority, 9), SEVERITY_SORT.get(j.severity, 9)))
        dependency_details = sorted(dependency_details, key=lambda d: (d.tap.lower(), d.dependency.lower()))
        analysis_log = sorted(analysis_log, key=lambda a: a.repository.lower())

        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        self._sheet_tap_summary(wb, tap_summaries)
        self._sheet_vulnerabilities(wb, vulnerabilities)
        self._sheet_jira_tickets(wb, jira_tickets)
        self._sheet_dependency_details(wb, dependency_details)
        self._sheet_analysis_log(wb, analysis_log)

        path = self.output_dir / "singer_tap_security_summary.xlsx"
        wb.save(path)
        LOGGER.info("Wrote %s", path)
        return path

    @staticmethod
    def _finalize_sheet(ws, n_cols: int, n_rows: int, wrap_cols: Optional[set] = None, widths: Optional[dict] = None):
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(n_cols)}{max(n_rows + 1, 1)}"
        header_font, header_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="404040")
        for col in range(1, n_cols + 1):
            cell = ws.cell(row=1, column=col)
            cell.font, cell.fill = header_font, header_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for row in range(2, n_rows + 2):
            for col in (wrap_cols or set()):
                ws.cell(row=row, column=col).alignment = Alignment(wrap_text=True, vertical="top")
        for col, width in (widths or {}).items():
            ws.column_dimensions[get_column_letter(col)].width = width

    @staticmethod
    def _apply_value_fills(ws, col: int, n_rows: int, fill_map: dict):
        for value, color in fill_map.items():
            ws.conditional_formatting.add(
                f"{get_column_letter(col)}2:{get_column_letter(col)}{n_rows + 1}",
                CellIsRule(operator="equal", formula=[f'"{value}"'], fill=PatternFill("solid", fgColor=color)),
            )

    @staticmethod
    def _hyperlink_column(ws, col: int, n_rows: int):
        for row in range(2, n_rows + 2):
            cell = ws.cell(row=row, column=col)
            if cell.value and str(cell.value).startswith("http"):
                cell.hyperlink = str(cell.value)
                cell.font = Font(color="0563C1", underline="single")

    def _sheet_tap_summary(self, wb, tap_summaries: List[TapSummary]):
        headers = ["Tap Name", "Repository", "Repository URL", "Security URL", "Active/Archived", "Security Status",
                   "Total Vulnerabilities", "Critical", "High", "Medium", "Low", "Informational", "Actionable Issues",
                   "Highest Severity", "Highest CVSS", "Recommended Priority", "Primary Recommended Action",
                   "Jira Tickets Required", "Analysis Status", "Notes"]
        ws = wb.create_sheet("Tap Summary")
        ws.append(headers)
        for t in tap_summaries:
            ws.append([t.tap_name, t.repository, t.repository_url, t.security_url, t.active_archived, t.security_status,
                       t.total_vulnerabilities, t.critical, t.high, t.medium, t.low, t.informational, t.actionable_issues,
                       t.highest_severity, t.highest_cvss, t.recommended_priority, t.primary_recommended_action,
                       t.jira_tickets_required, t.analysis_status, t.notes])
        n = len(tap_summaries)
        self._finalize_sheet(ws, len(headers), n, wrap_cols={17, 20},
                              widths={1: 24, 2: 22, 3: 34, 4: 34, 5: 14, 6: 18, 14: 16, 17: 45, 19: 16, 20: 30})
        self._apply_value_fills(ws, 14, n, SEVERITY_FILLS)
        self._apply_value_fills(ws, 16, n, PRIORITY_FILLS)
        self._hyperlink_column(ws, 3, n)
        self._hyperlink_column(ws, 4, n)

    def _sheet_vulnerabilities(self, wb, vulnerabilities: List[Vulnerability]):
        headers = ["Tap Name", "Repository", "Repository URL", "Security URL", "Vulnerability ID", "CVE", "GHSA",
                   "GHSA URL", "CVE URL", "Package", "Dependency Type", "Current Version", "Vulnerable Version Range",
                   "Fixed Version", "Severity", "CVSS Score", "CVSS Vector", "CWE", "EPSS", "Exploitability",
                   "Known Exploited", "Description", "Security Impact", "Technical Impact", "Recommended Action",
                   "Upgrade Required", "Code Changes Required", "Breaking Change Risk", "Dependency Source", "Evidence",
                   "Existing Issue", "Existing Issue URL", "Existing PR", "Existing PR URL", "Dependabot PR",
                   "Remediation Already Available", "Alert State", "Recommended Priority", "Actionable",
                   "Recommended Jira Summary", "Recommended Jira Description", "Recommended Jira Priority",
                   "Suggested Acceptance Criteria", "Status", "Notes"]
        ws = wb.create_sheet("Vulnerabilities")
        ws.append(headers)
        for v in vulnerabilities:
            ws.append([v.tap_name, v.repository, v.repository_url, v.security_url, v.vulnerability_id, v.cve, v.ghsa,
                       v.ghsa_url, v.cve_url, v.package, v.dependency_type, v.current_version, v.vulnerable_version_range,
                       v.fixed_version, v.severity, v.cvss_score, v.cvss_vector, v.cwe, v.epss, v.exploitability,
                       v.known_exploited, v.description, v.security_impact, v.technical_impact, v.recommended_action,
                       v.upgrade_required, v.code_changes_required, v.breaking_change_risk, v.dependency_source,
                       v.evidence, v.existing_issue, v.existing_issue_url, v.existing_pr, v.existing_pr_url,
                       v.dependabot_pr, v.remediation_already_available, v.alert_state, v.recommended_priority,
                       v.actionable, v.recommended_jira_summary, v.recommended_jira_description,
                       v.recommended_jira_priority, v.suggested_acceptance_criteria, v.status, v.notes])
        n = len(vulnerabilities)
        self._finalize_sheet(ws, len(headers), n, wrap_cols={22, 24, 25, 40, 41, 43},
                              widths={1: 20, 4: 30, 22: 50, 24: 40, 25: 45, 40: 45, 41: 60, 43: 45})
        self._apply_value_fills(ws, 15, n, SEVERITY_FILLS)
        self._apply_value_fills(ws, 38, n, PRIORITY_FILLS)
        for col in (3, 4, 8, 9, 30):
            self._hyperlink_column(ws, col, n)

    def _sheet_jira_tickets(self, wb, jira_tickets: List[JiraTicket]):
        headers = ["Tap", "Jira Summary", "Jira Priority", "Severity", "CVE/GHSA", "Package", "Current Version",
                   "Fixed Version", "Recommended Action", "Jira Description", "Acceptance Criteria", "References"]
        ws = wb.create_sheet("Jira Tickets")
        ws.append(headers)
        for j in jira_tickets:
            ws.append([j.tap, j.jira_summary, j.jira_priority, j.severity, j.cve_ghsa, j.package, j.current_version,
                       j.fixed_version, j.recommended_action, j.jira_description, j.acceptance_criteria, j.references])
        n = len(jira_tickets)
        self._finalize_sheet(ws, len(headers), n, wrap_cols={2, 9, 10, 11, 12}, widths={1: 20, 2: 45, 9: 45, 10: 70, 11: 45, 12: 40})
        self._apply_value_fills(ws, 3, n, PRIORITY_FILLS)
        self._apply_value_fills(ws, 4, n, SEVERITY_FILLS)

    def _sheet_dependency_details(self, wb, dependency_details: List[DependencyDetailRow]):
        headers = ["Tap", "Dependency", "Dependency Type", "Current Version", "Latest PyPI Version",
                   "Fixed Security Version", "Vulnerable?", "Outdated?", "Direct/Transitive", "Manifest",
                   "Upgrade Required", "Notes"]
        ws = wb.create_sheet("Dependency Details")
        ws.append(headers)
        for d in dependency_details:
            ws.append([d.tap, d.dependency, d.dependency_type, d.current_version, d.latest_pypi_version,
                       d.fixed_security_version, d.vulnerable, d.outdated, d.direct_transitive, d.manifest,
                       d.upgrade_required, d.notes])
        n = len(dependency_details)
        self._finalize_sheet(ws, len(headers), n, wrap_cols={12}, widths={1: 20, 2: 20, 12: 40})
        self._apply_value_fills(ws, 7, n, {"Yes": "FFC7CE", "No": "D9EAD3"})

    def _sheet_analysis_log(self, wb, analysis_log: List[AnalysisLogEntry]):
        headers = ["Repository", "Discovered?", "Identified as tap?", "Security page accessible?",
                   "Dependency information accessible?", "Vulnerability scan successful?",
                   "Number of vulnerabilities found", "Analysis status", "Error", "Timestamp"]
        ws = wb.create_sheet("Analysis Log")
        ws.append(headers)
        for a in analysis_log:
            ws.append([a.repository, a.discovered, a.identified_as_tap, a.security_page_accessible,
                       a.dependency_info_accessible, a.vulnerability_scan_successful, a.vulnerabilities_found,
                       a.analysis_status, a.error, a.timestamp])
        n = len(analysis_log)
        self._finalize_sheet(ws, len(headers), n, wrap_cols={9}, widths={1: 26, 8: 20, 9: 45, 10: 22})
        self._apply_value_fills(ws, 8, n, {"Failed": "FFC7CE", "Manual review required": "FFF2CC", "Success": "D9EAD3"})


# ============================================================================
# Orchestration
# ============================================================================

class TapAuditResult:
    def __init__(self, repo: RepoInfo):
        self.repo = repo
        self.vulnerabilities: List[Vulnerability] = []
        self.dependency_details: List[DependencyDetailRow] = []
        self.summary: Optional[TapSummary] = None
        self.security_page_accessible = False
        self.dependency_info_accessible = False
        self.vulnerability_scan_successful = False
        self.analysis_status = "Not analyzed"
        self.error = ""


class SingerSecurityAuditor:
    def __init__(self, org: str, github: GitHubClient, pypi: PyPIClient, threat_intel: ThreatIntelClient,
                 local_repos_dir: Optional[Path], include_archived: bool):
        self.org = org
        self.github = github
        self.security_analyzer = SecurityAnalyzer(github)
        self.dependency_analyzer = DependencyAnalyzer(github, org, local_repos_dir)
        self.vulnerability_analyzer = VulnerabilityAnalyzer(pypi, threat_intel)
        self.pypi = pypi
        self.include_archived = include_archived
        self.audit_logger = AuditLogger()

    def audit_tap(self, repo: RepoInfo) -> TapAuditResult:
        result = TapAuditResult(repo)
        if (repo.archived or repo.disabled) and not self.include_archived:
            result.analysis_status = "Archived/disabled - skipped deep scan (use --include-archived to force)"
            self.audit_logger.record(repository=repo.full_name, discovered=True, identified_as_tap=True,
                                      security_page_accessible=False, dependency_info_accessible=False,
                                      vulnerability_scan_successful=False, vulnerabilities_found=0,
                                      analysis_status=result.analysis_status)
            result.summary = self._build_summary(repo, [], result.analysis_status, [])
            return result

        alerts, alerts_error = self.security_analyzer.get_dependabot_alerts(self.org, repo.name)
        result.security_page_accessible = not alerts_error
        activity = self.security_analyzer.get_repo_activity(self.org, repo.name)

        declarations, dep_error = self.dependency_analyzer.analyze(repo.name)
        result.dependency_info_accessible = not (dep_error and dep_error.startswith("no_manifest_found"))
        declarations_by_package = {d.package.lower(): d for d in declarations}

        vulnerabilities: List[Vulnerability] = []
        if not alerts_error:
            for alert in alerts:
                try:
                    vulnerabilities.append(self.vulnerability_analyzer.analyze_alert(
                        repo.name, repo.html_url, repo.security_url, alert, declarations_by_package, activity))
                except Exception as exc:  # pragma: no cover - never let one bad alert kill the tap
                    LOGGER.exception("%s: failed to analyze one alert: %s", repo.name, exc)
            result.vulnerability_scan_successful = True
        result.vulnerabilities = vulnerabilities

        vulnerable_packages = {v.package.lower() for v in vulnerabilities if v.actionable == "Yes"}
        for decl in declarations:
            pypi_info = self.pypi.get_latest_version(decl.package)
            latest = pypi_info.latest_version or ""
            outdated = "Unknown"
            if decl.current_version and latest:
                outdated = "Yes" if decl.current_version != latest else "No"
            fixed_versions = sorted({v.fixed_version for v in vulnerabilities
                                      if v.package.lower() == decl.package.lower() and v.fixed_version and v.fixed_version != "(none published)"})
            result.dependency_details.append(DependencyDetailRow(
                tap=repo.name, dependency=decl.package, dependency_type=decl.dep_type,
                current_version=decl.current_version or "(unresolved)",
                latest_pypi_version=latest or (pypi_info.error or "unknown"),
                fixed_security_version=", ".join(fixed_versions) or "N/A",
                vulnerable="Yes" if decl.package.lower() in vulnerable_packages else "No", outdated=outdated,
                direct_transitive="Direct" if decl.dep_type.startswith("Direct") else "Transitive",
                manifest=decl.manifest, upgrade_required="Yes" if decl.package.lower() in vulnerable_packages else "No",
                notes=decl.parse_note,
            ))

        if alerts_error == "forbidden":
            status = "Insufficient permissions to read security data - manual review required"
        elif alerts_error == "not_found":
            status = "Dependabot alerts not available for this repo - manual review required"
        elif alerts_error and alerts_error.startswith("authentication_error"):
            status = "Authentication error - manual review required"
        elif alerts_error:
            status = f"Security scan failed ({alerts_error}) - manual review required"
        elif vulnerabilities:
            status = "Vulnerabilities found"
        else:
            status = "No known vulnerabilities"
        result.analysis_status = status
        result.error = alerts_error or dep_error or ""

        self.audit_logger.record(repository=repo.full_name, discovered=True, identified_as_tap=True,
                                  security_page_accessible=result.security_page_accessible,
                                  dependency_info_accessible=result.dependency_info_accessible,
                                  vulnerability_scan_successful=result.vulnerability_scan_successful,
                                  vulnerabilities_found=len(vulnerabilities), analysis_status=status, error=result.error)
        result.summary = self._build_summary(repo, vulnerabilities, status, declarations)
        return result

    @staticmethod
    def _build_summary(repo: RepoInfo, vulnerabilities: List[Vulnerability], status: str, declarations) -> TapSummary:
        counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Informational": 0}
        actionable = 0
        for v in vulnerabilities:
            counts[v.severity] = counts.get(v.severity, 0) + 1
            if v.actionable == "Yes":
                actionable += 1
        highest_severity = "None"
        for sev in ("Critical", "High", "Medium", "Low", "Informational"):
            if counts[sev] > 0:
                highest_severity = sev
                break
        cvss_values = [v.cvss_score for v in vulnerabilities if v.cvss_score is not None]
        highest_cvss = max(cvss_values) if cvss_values else None
        priorities = [v.recommended_priority for v in vulnerabilities if v.actionable == "Yes"]
        recommended_priority = sorted(priorities)[0] if priorities else "P4 - Informational"
        actionable_vulns = [v for v in vulnerabilities if v.actionable == "Yes" and v.remediation_already_available == "No"]
        primary_action = actionable_vulns[0].recommended_action if actionable_vulns else (
            "No action required" if status == "No known vulnerabilities" else "See Analysis Log for details")
        active_archived = "Archived" if repo.archived else ("Disabled" if repo.disabled else "Active")
        return TapSummary(
            tap_name=repo.name, repository=repo.full_name, repository_url=repo.html_url, security_url=repo.security_url,
            active_archived=active_archived, security_status=status, total_vulnerabilities=len(vulnerabilities),
            critical=counts["Critical"], high=counts["High"], medium=counts["Medium"], low=counts["Low"],
            informational=counts["Informational"], actionable_issues=actionable, highest_severity=highest_severity,
            highest_cvss=highest_cvss, recommended_priority=recommended_priority, primary_recommended_action=primary_action,
            jira_tickets_required=len(actionable_vulns), analysis_status=status,
            notes="" if declarations else "No dependency manifest could be parsed",
        )


# ============================================================================
# CLI entry point
# ============================================================================

def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(handler)


def load_token(cli_token: Optional[str], config_path: Path) -> Optional[str]:
    if cli_token:
        return cli_token
    env_token = os.environ.get("GITHUB_TOKEN")
    if env_token:
        return env_token
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            token = data.get("api_key") or data.get("token")
            if token:
                LOGGER.info("Loaded GitHub token from %s", config_path)
                return token
        except (OSError, ValueError) as exc:
            LOGGER.warning("Could not read %s: %s", config_path, exc)
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit every singer-io tap for security vulnerabilities.")
    parser.add_argument("--org", default="singer-io")
    parser.add_argument("--output", default="./security-audit")
    parser.add_argument("--github-token", default=None)
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retry-count", type=int, default=5)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--tap", nargs="+", default=None, help="Audit only these tap(s) instead of the whole org (skips discovery).")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    if not HAVE_OPENPYXL:
        LOGGER.error("openpyxl is required to write the Excel report. Install with: pip install openpyxl")
        return 1

    token = load_token(args.github_token, DEFAULT_CONFIG_PATH)
    if not token:
        LOGGER.warning("No GitHub token found (GITHUB_TOKEN / --github-token / tmp/configs/config.json). "
                        "Proceeding unauthenticated at a much lower rate limit; Dependabot alerts require a token for most repos.")

    github = GitHubClient(token=token, timeout=args.timeout, retry_count=args.retry_count)
    pypi = PyPIClient(timeout=args.timeout)
    threat_intel = ThreatIntelClient(timeout=args.timeout)
    local_repos_dir = Path("singer_tap_repos")
    if not local_repos_dir.is_dir():
        local_repos_dir = None

    auditor = SingerSecurityAuditor(args.org, github, pypi, threat_intel, local_repos_dir, args.include_archived)

    discovery_error = ""
    if args.tap:
        LOGGER.info("Auditing %d explicitly specified tap(s), skipping org-wide discovery.", len(args.tap))
        all_repos = []
        for name in args.tap:
            data, error = github.get_repo(args.org, name)
            if error or not data:
                LOGGER.error("Could not fetch %s/%s: %s", args.org, name, error or "unknown error")
                continue
            all_repos.append(RepoInfo(
                name=data["name"], full_name=data["full_name"], html_url=data["html_url"],
                security_url=f"https://github.com/{data['full_name']}/security",
                archived=bool(data.get("archived")), is_fork=bool(data.get("fork")),
                disabled=bool(data.get("disabled")), default_branch=data.get("default_branch", "master"),
                description=data.get("description") or "", topics=data.get("topics") or [],
            ))
    else:
        LOGGER.info("Discovering repositories in org '%s'...", args.org)
        all_repos, discovery_error = RepositoryDiscovery(github, args.org).discover()
        if discovery_error:
            LOGGER.error("Repository discovery encountered an error: %s", discovery_error)

    for repo in all_repos:
        TapDetector.classify(repo)
    taps = [r for r in all_repos if r.is_tap]
    non_taps = [r for r in all_repos if not r.is_tap]

    for repo in non_taps:
        auditor.audit_logger.record(repository=repo.full_name, discovered=True, identified_as_tap=False,
                                     security_page_accessible=False, dependency_info_accessible=False,
                                     vulnerability_scan_successful=False, vulnerabilities_found=0,
                                     analysis_status=f"Skipped (not a tap: {repo.tap_detection_reason})")

    LOGGER.info("Discovered %d repositories total; %d identified as taps; %d excluded (non-taps).",
                len(all_repos), len(taps), len(non_taps))

    results: List[TapAuditResult] = []
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as pool:
        futures = {pool.submit(auditor.audit_tap, repo): repo for repo in taps}
        for i, future in enumerate(as_completed(futures), 1):
            repo = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - a tap must never vanish silently
                LOGGER.exception("%s: unexpected failure during audit: %s", repo.name, exc)
                auditor.audit_logger.record(repository=repo.full_name, discovered=True, identified_as_tap=True,
                                             security_page_accessible=False, dependency_info_accessible=False,
                                             vulnerability_scan_successful=False, vulnerabilities_found=0,
                                             analysis_status="Failed (unexpected exception)", error=str(exc))
                result = TapAuditResult(repo)
                result.analysis_status = "Failed (unexpected exception)"
                result.summary = SingerSecurityAuditor._build_summary(repo, [], "Failed (unexpected exception)", [])
            results.append(result)
            LOGGER.info("[%d/%d] %s: %s", i, len(taps), repo.name, result.analysis_status)

    all_vulnerabilities: List[Vulnerability] = []
    all_dependency_details: List[DependencyDetailRow] = []
    all_summaries: List[TapSummary] = []
    for r in results:
        all_vulnerabilities.extend(r.vulnerabilities)
        all_dependency_details.extend(r.dependency_details)
        if r.summary:
            all_summaries.append(r.summary)

    jira_tickets = JiraTicketGenerator.generate(all_vulnerabilities)

    output_dir = Path(args.output)
    report_path = ReportGenerator(output_dir).write(all_summaries, all_vulnerabilities, jira_tickets,
                                                      all_dependency_details, auditor.audit_logger.entries())

    archived_skipped = [r for r in results if (r.repo.archived or r.repo.disabled) and not r.vulnerability_scan_successful]
    analyzed = [r for r in results if r.vulnerability_scan_successful]
    with_vulns = [r for r in analyzed if r.vulnerabilities]
    without_vulns = [r for r in analyzed if not r.vulnerabilities]
    manual_review = [r for r in results if not r.vulnerability_scan_successful and r not in archived_skipped]
    skipped = [r for r in results if r.analysis_status == "Not analyzed"]

    sev_counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Informational": 0}
    for v in all_vulnerabilities:
        sev_counts[v.severity] = sev_counts.get(v.severity, 0) + 1

    print("=" * 50)
    print("SINGER.IO SECURITY AUDIT")
    print("=" * 50)
    print()
    print(f"Repositories discovered:       {len(all_repos)}")
    print(f"Singer taps identified:        {len(taps)}")
    print(f"Successfully analyzed:         {len(analyzed)}")
    print(f"Manual review required:        {len(manual_review)}")
    print()
    print("Vulnerabilities:")
    print(f"  Critical:                    {sev_counts['Critical']}")
    print(f"  High:                        {sev_counts['High']}")
    print(f"  Medium:                      {sev_counts['Medium']}")
    print(f"  Low:                         {sev_counts['Low']}")
    print(f"  Informational:               {sev_counts['Informational']}")
    print()
    print(f"Jira tickets recommended:      {len(jira_tickets)}")
    print()
    print("Output:")
    print(f"  {report_path}")
    print()
    print("=" * 50)
    print()
    print("Reconciliation:")
    print(f"  Singer repositories discovered:  {len(all_repos)}")
    print(f"  Singer taps identified:          {len(taps)}")
    print(f"  Taps successfully analyzed:      {len(analyzed)}")
    print(f"  Taps with vulnerabilities:       {len(with_vulns)}")
    print(f"  Taps without vulnerabilities:    {len(without_vulns)}")
    print(f"  Taps requiring manual review:    {len(manual_review)}")
    print(f"  Taps archived/inactive (skipped by design): {len(archived_skipped)}")
    print(f"  Taps skipped:                    {len(skipped)}")

    if skipped or len(analyzed) + len(manual_review) + len(archived_skipped) != len(taps) or discovery_error:
        LOGGER.error("Audit completed with unresolved gaps (skipped taps, discovery errors, or reconciliation mismatch). See Analysis Log.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
