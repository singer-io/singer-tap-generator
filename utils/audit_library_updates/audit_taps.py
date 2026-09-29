#!/usr/bin/env python3
"""
Singer Tap Dependency Auditor
=============================

Audits every ``tap-*`` repository under the ``singer-io`` GitHub organization
for outdated dependencies declared in ``setup.py``, produces an Excel/CSV
report, and (optionally) opens pull requests that bump outdated pins.

STEP-BY-STEP SETUP
-------------------
1. Install Python 3.8+ and Git, and make sure ``git`` is on your ``PATH``
   (``git --version`` should work in a terminal).
2. That's it. The script automatically creates and manages its own virtual
   environment (``.venv`` next to this file) the first time it runs, installs
   every required package into it, and re-executes itself inside that venv -
   you do not need to run ``pip install`` yourself. If your network/sandbox
   doesn't allow creating a venv or installing packages, it falls back to
   running in whatever interpreter invoked it and prints a clear error naming
   any package you still need to install by hand::

       pip install requests packaging openpyxl

   ``openpyxl`` is optional. If it is not installed, the script automatically
   falls back to writing two CSV files (``<output>.csv`` + ``<output>.summary.csv``)
   instead of a single ``.xlsx`` workbook.
3. (Recommended) Create a GitHub personal access token and export it, so API
   requests aren't limited to 60/hour and so ``--create-pr`` can push/open
   PRs:

       # PowerShell
       $env:GITHUB_TOKEN = "ghp_xxx..."
       # bash/zsh
       export GITHUB_TOKEN="ghp_xxx..."

   A classic token with the ``public_repo`` scope (or fine-grained equivalent)
   is enough. You can also pass it explicitly with ``--token``.

   As an alternative to the environment variable, you may instead create::

       tmp/configs/config.json
       {
           "username": "your-github-username",
           "api_key": "ghp_xxx..."
       }

   The script uses ``--token``, then ``GITHUB_TOKEN``, then this file's
   ``api_key`` (in that order) as the GitHub token for ``--create-pr``. The
   token value is never printed or written to the log file. Add ``tmp/`` to
   ``.gitignore`` so this file is never committed to source control, and
   treat it like any other secret (avoid pasting real tokens into chats,
   issues, or commit messages, and revoke/rotate a token immediately if it is
   ever exposed).

HOW TO RUN
----------
Audit every ``tap-*`` repository in the ``singer-io`` org and write an Excel
report (this clones/updates every tap under ``./singer_tap_repos`` and can
take a while the first time it runs)::

    python audit_taps.py

Audit into a specific output file::

    python audit_taps.py --output taps_dependency_report.xlsx

Audit only one or more specific taps (skips org-wide discovery entirely)::

    python audit_taps.py --tap tap-shopify
    python audit_taps.py --tap tap-shopify tap-gitlab tap-mailjet

Update outdated dependency pins and open pull requests for specific taps::

    python audit_taps.py --create-pr tap-shopify tap-gitlab tap-mailjet

Preview exactly what would change (no git/GitHub side effects at all)::

    python audit_taps.py --create-pr tap-shopify --dry-run

Other useful flags: ``--org`` (default ``singer-io``), ``--workdir`` (default
``./singer_tap_repos``, reused across runs), ``--token`` (overrides
``GITHUB_TOKEN``/``--config``), ``--config`` (path to a token JSON file,
default ``tmp/configs/config.json``), ``--log-file`` (default
``logs/audit_taps.log``; every run's full log is appended there - one
consolidated file across the whole execution - in addition to the console),
``--verbose`` (debug logging).

OPTIONAL QTC STATUS ENRICHMENT
------------------------------
If ``qtc_status.xlsx`` is present in the same directory as this script, it is
automatically loaded and a ``QTC Status`` column is added to every generated
report sheet (``Dependency Audit`` and ``Summary``). The value is matched by
the tap name in ``TAP_NAME`` and taken from ``release_ff``.

The file is optional: if it is not present, the report is generated exactly as
before. The first worksheet must contain these columns (column order does not
matter)::

    TAP_NAME                 release_ff
    tap-shopify              QCDI_STITCH_INTEGRATION
    tap-gitlab               QTCP_STITCH_CONN_BATCH_14
    tap-mailjet              QTCP_STITCH_CONN_BATCH_17

``TAP_NAME`` must contain the exact Singer tap repository name used by the
report. Duplicate ``TAP_NAME`` values are rejected to avoid ambiguous status
mapping. Taps that are not present in the QTC file receive a blank ``QTC
Status`` value. Extra columns in the QTC workbook are ignored.

VIRTUAL ENVIRONMENT
---------------------
On every invocation, the script checks for a ``.venv`` folder next to this
file. If it doesn't exist yet, it is created automatically (via the stdlib
``venv`` module) and the required packages are installed into it once
(tracked with a ``.venv/.setup_complete`` marker so this only happens the
first time or after deleting ``.venv``). The script then re-executes itself
using that venv's Python interpreter, so a plain ``python audit_taps.py``
always runs fully isolated from your system Python. This step is skipped
automatically when already running inside that venv (or when the module is
imported rather than executed directly).

WHAT HAPPENS ON RE-RUNS
------------------------
If a tap has already been cloned into the working directory from a previous
run, the script does NOT reuse it as-is: it fetches the repository's default
branch, force-checks it out (discarding any local branch left over from a
previous ``--create-pr`` run), and hard-resets it to match the latest code on
GitHub *before* parsing ``setup.py`` and analyzing dependencies. This means
re-running the script always audits the latest upstream code.

HOW ``--create-pr`` OPENS PULL REQUESTS
-----------------------------------------
Most contributors don't have push access to ``singer-io`` repositories
directly, so ``--create-pr`` uses the standard fork workflow: it looks up the
authenticated user for the supplied token, creates (or reuses) their fork of
the target repository, pushes the dependency-update branch to that fork, and
opens a pull request from ``<your-user>:chore/update-dependencies`` against
the upstream repository's default branch. A GitHub token is required for this
(``GITHUB_TOKEN`` or ``--token``); without one, ``--create-pr`` will still
compute and log the dependency updates but will skip pushing/opening a PR.

Environment variables
----------------------
    GITHUB_TOKEN   Optional GitHub personal access token. Raises the
                   unauthenticated API rate limit and is required to fork,
                   push branches, and open pull requests in ``--create-pr``
                   mode.

Corporate networks / TLS-intercepting proxies
----------------------------------------------
If GitHub API calls fail with a certificate verification error even though
``git clone`` works fine, your network likely intercepts TLS with an internal
CA that's trusted by the OS (and therefore by ``git``, which uses the OS
certificate store) but not by Python's bundled ``certifi`` CA list. Installing
``pip install truststore`` fixes this: the script automatically detects it and
switches ``requests`` to validate certificates against the OS trust store
instead, exactly like ``git`` already does. This is optional and unused if not
installed.
"""

import os
import sys
from pathlib import Path

_VENV_MARKER_ENV = "SINGER_AUDIT_VENV_ACTIVE"
_VENV_DIR_NAME = ".venv"
_VENV_REQUIRED_PACKAGES = ["requests", "packaging", "openpyxl", "truststore"]


def _venv_paths():
    root = Path(__file__).resolve().parent
    venv_dir = root / _VENV_DIR_NAME
    venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return venv_dir, venv_python


def _running_inside_managed_venv(venv_python: Path) -> bool:
    if os.environ.get(_VENV_MARKER_ENV) == "1":
        return True
    try:
        return Path(sys.executable).resolve() == venv_python.resolve()
    except OSError:
        return False


def _ensure_venv_and_reexec() -> None:
    """Creates ./.venv (if missing), installs required packages into it once,
    and re-executes this script inside it so a plain `python audit_taps.py`
    always runs fully isolated from the system Python. Falls back to running
    in the current interpreter (best effort) if venv setup fails for any
    reason (e.g. no network access, restricted sandbox, or a transient
    Windows file lock from antivirus/indexing right after venv creation).
    """
    import subprocess
    import time
    import venv as venv_module

    venv_dir, venv_python = _venv_paths()
    if _running_inside_managed_venv(venv_python):
        return

    def _pip_install(args, attempts=3):
        last_exc = None
        for attempt in range(1, attempts + 1):
            try:
                subprocess.run(
                    [str(venv_python), "-m", "pip", "install", "--quiet"] + args, check=True,
                )
                return
            except subprocess.CalledProcessError as exc:
                last_exc = exc
                if attempt < attempts:
                    time.sleep(2 * attempt)
        raise last_exc

    try:
        if not venv_python.exists():
            sys.stderr.write(f"Creating virtual environment at {venv_dir} ...\n")
            venv_module.create(venv_dir, with_pip=True)

        marker = venv_dir / ".setup_complete"
        if not marker.exists():
            sys.stderr.write("Installing required packages into the virtual environment ...\n")
            _pip_install(_VENV_REQUIRED_PACKAGES)
            marker.write_text("ok", encoding="utf-8")

        env = os.environ.copy()
        env[_VENV_MARKER_ENV] = "1"
        completed = subprocess.run(
            [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:], env=env
        )
        sys.exit(completed.returncode)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - never block a run just because auto-venv setup failed
        sys.stderr.write(
            f"WARNING: Could not set up/use the virtual environment ({exc}); "
            "continuing with the current Python interpreter.\n"
        )


if __name__ == "__main__" and os.environ.get(_VENV_MARKER_ENV) != "1":
    _ensure_venv_and_reexec()

import importlib

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:  # pragma: no cover - optional, only helps on some networks
    pass


def _check_third_party_dependencies():
    missing = []
    for module_name in ("requests", "packaging"):
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(module_name)
    if missing:
        sys.stderr.write(
            "ERROR: Missing required Python package(s): {}\n"
            "Install them with:\n\n    pip install {}\n\n"
            "(openpyxl is optional; without it the report is written as CSV)\n".format(
                ", ".join(missing), " ".join(missing)
            )
        )
        sys.exit(1)


_check_third_party_dependencies()

import argparse
import ast
import csv
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - very old urllib3
    from requests.packages.urllib3.util.retry import Retry

from packaging.requirements import Requirement, InvalidRequirement
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.version import Version, InvalidVersion

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    HAVE_OPENPYXL = True
except ImportError:
    HAVE_OPENPYXL = False


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
PYPI_API = "https://pypi.org/pypi/{name}/json"
DEFAULT_ORG = "singer-io"
DEFAULT_BRANCH_NAME = "chore/update-dependencies"
DEFAULT_PR_TITLE = "chore: update dependencies"
BOT_NAME = "singer-tap-audit-bot"
BOT_EMAIL = "singer-tap-audit-bot@users.noreply.github.com"
DEFAULT_CONFIG_PATH = Path("tmp/configs/config.json")
LOGS_DIR_NAME = "logs"
DEFAULT_LOG_FILE = str(Path(LOGS_DIR_NAME) / "audit_taps.log")

MAIN_DEP_TYPE = "Main/Runtime"
DEV_DEP_TYPE = "Development/Test"

STATUS_UPDATE_REQUIRED = "UPDATE_REQUIRED"
STATUS_UP_TO_DATE = "UP_TO_DATE"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_UNABLE_TO_CHECK = "UNABLE_TO_CHECK"

REPORT_COLUMNS = [
    "Tap Name",
    "Repository",
    "Dependency Type",
    "Library Name",
    "Installed/Specified Version",
    "Latest PyPI Version",
    "Needs Update",
    "Update Available",
    "Version Change Type",
    "Status",
    "Error/Notes",
]

SUMMARY_COLUMNS = [
    "Tap Name",
    "Total Dependencies",
    "Main Dependencies",
    "Dev Dependencies",
    "Requiring Updates",
    "Up To Date",
    "Overall Version Change Type",
    "Libraries Requiring Updates",
    "Overall Status",
]

# Optional QTC status enrichment. When qtc_status.xlsx exists next to this
# script, its TAP_NAME -> release_ff mapping is added to every generated
# report sheet.
DEFAULT_QTC_STATUS_FILE = "qtc_status.xlsx"
QTC_TAP_NAME_COLUMN = "TAP_NAME"
QTC_STATUS_SOURCE_COLUMN = "release_ff"
QTC_STATUS_REPORT_COLUMN = "QTC Status"

# Fallback stdlib module list used on Python < 3.10 where
# sys.stdlib_module_names does not exist.
_FALLBACK_STDLIB = {
    "abc", "argparse", "array", "asyncio", "base64", "bisect", "builtins",
    "calendar", "collections", "configparser", "contextlib", "copy", "csv",
    "ctypes", "dataclasses", "datetime", "decimal", "difflib", "dis", "email",
    "enum", "errno", "fnmatch", "fractions", "ftplib", "functools", "gc",
    "getpass", "glob", "gzip", "hashlib", "heapq", "hmac", "html", "http",
    "importlib", "inspect", "io", "ipaddress", "itertools", "json", "keyword",
    "logging", "lzma", "math", "mimetypes", "multiprocessing", "operator",
    "os", "pathlib", "pickle", "pkgutil", "platform", "pprint", "queue",
    "random", "re", "sched", "secrets", "shelve", "shutil", "signal",
    "site", "smtplib", "socket", "socketserver", "sqlite3", "ssl", "stat",
    "statistics", "string", "struct", "subprocess", "sys", "tarfile",
    "tempfile", "textwrap", "threading", "time", "timeit", "tkinter",
    "token", "tokenize", "traceback", "types", "typing", "unittest",
    "urllib", "uuid", "venv", "warnings", "weakref", "xml", "zipfile",
    "zlib", "zoneinfo",
}

try:
    STDLIB_MODULES = set(sys.stdlib_module_names)  # type: ignore[attr-defined]
except AttributeError:
    STDLIB_MODULES = set(_FALLBACK_STDLIB)


LOGGER = logging.getLogger("audit_taps")


def setup_logging(verbose: bool = False, log_file: Optional[str] = None) -> None:
    """Configures console + (optional) file logging. Safe to call more than
    once (e.g. from tests) since it replaces any previously installed handlers.
    All runs append to the same consolidated log file by default (logs/audit_taps.log).
    """
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(console_handler)

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root.addHandler(file_handler)
        LOGGER.info("Logging to file: %s", log_path)


def mask_secret(text: str, secret: Optional[str]) -> str:
    """Redact a secret (e.g. a GitHub token) from a string before logging it."""
    if not secret:
        return text
    return text.replace(secret, "***")


def load_token_from_config(config_path: Path) -> Optional[str]:
    """Reads a GitHub token from a JSON file shaped like
    ``{"username": "...", "api_key": "..."}``. Never logs the token value.
    """
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOGGER.warning("Could not read GitHub config file %s: %s", config_path, exc)
        return None
    token = data.get("api_key") or data.get("token")
    if not token:
        LOGGER.warning("%s does not contain an 'api_key' field; ignoring", config_path)
        return None
    LOGGER.info(
        "Loaded GitHub token from %s (user: %s)", config_path, data.get("username", "unknown")
    )
    return token


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class ParsedDependency:
    tap_name: str
    repo_url: str
    dep_type: str
    extra_name: Optional[str]
    raw_requirement: str
    name: str
    requirement: Optional[Requirement]
    parse_error: Optional[str]
    source_span: Optional[Tuple[int, int, int, int]]  # start_line, start_col, end_line, end_col


@dataclass
class ReportRow:
    tap_name: str
    repository: str
    dep_type: str
    library_name: str
    installed_version: str
    latest_version: str
    needs_update: str
    update_available: str
    status: str
    notes: str
    version_change_type: str = ""  # "Major", "Minor", "Patch", or "" when not applicable


@dataclass
class DependencyChange:
    name: str
    dep_type: str
    old_version: str
    new_version: str
    change_type: str = ""  # "Major", "Minor", or "Patch"


@dataclass
class PRResult:
    tap_name: str
    status: str  # CREATED, DRY_RUN, SKIPPED_NO_UPDATES, SKIPPED_EXISTING_PR, SKIPPED_NO_TOKEN, SKIPPED, FAILED
    pr_url: str = ""
    old_tap_version: str = ""
    new_tap_version: str = ""
    dependencies_updated: str = ""
    notes: str = ""


@dataclass
class PyPILookupResult:
    status: str  # "ok", "not_found", "error"
    latest_version: Optional[str]
    error: Optional[str] = None


# --------------------------------------------------------------------------
# PyPI client
# --------------------------------------------------------------------------

class PyPIClient:
    """Queries PyPI for the latest release of a package, with retries and caching."""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._cache: Dict[str, PyPILookupResult] = {}
        self.session = requests.Session()
        retries = Retry(
            total=4,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.session.mount("http://", HTTPAdapter(max_retries=retries))
        self.session.headers.update({"User-Agent": "singer-tap-dependency-auditor"})

    def get_latest_version(self, package_name: str) -> PyPILookupResult:
        cache_key = package_name.lower()
        if cache_key in self._cache:
            return self._cache[cache_key]

        result = self._fetch(package_name)
        self._cache[cache_key] = result
        return result

    def _fetch(self, package_name: str) -> PyPILookupResult:
        url = PYPI_API.format(name=package_name)
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            LOGGER.warning("PyPI lookup failed for %s: %s", package_name, exc)
            return PyPILookupResult("error", None, str(exc))

        if resp.status_code == 404:
            return PyPILookupResult("not_found", None, "Package not found on PyPI")

        if resp.status_code != 200:
            return PyPILookupResult(
                "error", None, f"PyPI returned HTTP {resp.status_code}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            return PyPILookupResult("error", None, f"Invalid JSON from PyPI: {exc}")

        latest = self._select_latest_version(data)
        if latest is None:
            return PyPILookupResult("error", None, "No usable releases found on PyPI")
        return PyPILookupResult("ok", latest)

    @staticmethod
    def _select_latest_version(data: dict) -> Optional[str]:
        releases = data.get("releases") or {}
        candidates = []
        for version_str, files in releases.items():
            if not files:
                continue
            if all(f.get("yanked") for f in files):
                continue
            try:
                candidates.append(Version(version_str))
            except InvalidVersion:
                continue

        stable = [v for v in candidates if not v.is_prerelease and not v.is_devrelease]
        pool = stable if stable else candidates
        if pool:
            return str(max(pool))

        info_version = (data.get("info") or {}).get("version")
        return info_version or None


# --------------------------------------------------------------------------
# GitHub API client
# --------------------------------------------------------------------------

class GitHubAPIError(Exception):
    pass


class GitHubClient:
    def __init__(self, token: Optional[str] = None, timeout: int = 30):
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=1.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "singer-tap-dependency-auditor",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.session.headers.update(headers)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise GitHubAPIError(f"Network error calling GitHub API ({method} {path}): {exc}") from exc
        if resp.status_code == 403 and self._is_rate_limited(resp):
            wait_seconds = self._rate_limit_wait(resp)
            LOGGER.warning(
                "GitHub API rate limit hit, waiting %.0fs before retrying", wait_seconds
            )
            time.sleep(wait_seconds)
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                raise GitHubAPIError(f"Network error calling GitHub API ({method} {path}): {exc}") from exc
        return resp

    @staticmethod
    def _is_rate_limited(resp: requests.Response) -> bool:
        return resp.headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in resp.text.lower()

    @staticmethod
    def _rate_limit_wait(resp: requests.Response) -> float:
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return max(1.0, float(reset) - time.time() + 1)
            except ValueError:
                pass
        return 30.0

    def list_org_tap_repos(self, org: str) -> List[str]:
        names = []
        page = 1
        while True:
            resp = self._request(
                "GET",
                f"/orgs/{org}/repos",
                params={"per_page": 100, "page": page, "type": "public"},
            )
            if resp.status_code != 200:
                raise GitHubAPIError(
                    f"Failed to list repos for org '{org}': HTTP {resp.status_code} {resp.text[:200]}"
                )
            batch = resp.json()
            if not batch:
                break
            for repo in batch:
                name = repo.get("name", "")
                if name.startswith("tap-"):
                    names.append(name)
            page += 1
        return sorted(set(names))

    def get_repo(self, org: str, repo: str) -> Optional[dict]:
        resp = self._request("GET", f"/repos/{org}/{repo}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise GitHubAPIError(f"Failed to fetch repo {org}/{repo}: HTTP {resp.status_code}")
        return resp.json()

    def find_existing_pr(
        self, org: str, repo: str, branch: str, head_owner: Optional[str] = None
    ) -> Optional[dict]:
        owner = head_owner or org
        resp = self._request(
            "GET",
            f"/repos/{org}/{repo}/pulls",
            params={"state": "open", "head": f"{owner}:{branch}"},
        )
        if resp.status_code != 200:
            raise GitHubAPIError(
                f"Failed to search existing PRs for {org}/{repo}: HTTP {resp.status_code}"
            )
        results = resp.json()
        return results[0] if results else None

    def get_authenticated_user(self) -> str:
        resp = self._request("GET", "/user")
        if resp.status_code != 200:
            raise GitHubAPIError(
                f"Failed to determine the authenticated GitHub user (is the token valid?): "
                f"HTTP {resp.status_code}"
            )
        return resp.json()["login"]

    def ensure_fork(self, org: str, repo: str, fork_owner: str, max_wait: float = 60.0) -> dict:
        """Returns the fork_owner/repo repository, creating the fork first if needed."""
        existing = self.get_repo(fork_owner, repo)
        if existing is not None:
            return existing
        resp = self._request("POST", f"/repos/{org}/{repo}/forks")
        if resp.status_code not in (200, 202):
            raise GitHubAPIError(
                f"Failed to fork {org}/{repo} into {fork_owner}: HTTP {resp.status_code} {resp.text[:200]}"
            )
        deadline = time.time() + max_wait
        while time.time() < deadline:
            forked = self.get_repo(fork_owner, repo)
            if forked is not None:
                return forked
            time.sleep(3)
        raise GitHubAPIError(f"Timed out waiting for fork {fork_owner}/{repo} to become available")

    def create_pull_request(
        self, org: str, repo: str, title: str, body: str, head: str, base: str
    ) -> dict:
        resp = self._request(
            "POST",
            f"/repos/{org}/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        if resp.status_code not in (200, 201):
            raise GitHubAPIError(
                f"Failed to create PR for {org}/{repo}: HTTP {resp.status_code} {resp.text[:300]}"
            )
        return resp.json()


# --------------------------------------------------------------------------
# Git operations (subprocess based)
# --------------------------------------------------------------------------

class GitError(Exception):
    pass


class GitOps:
    @staticmethod
    def _run(args: List[str], cwd: Path, log_cmd: Optional[str] = None) -> str:
        LOGGER.debug("git %s (cwd=%s)", log_cmd or " ".join(args), cwd)
        result = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitError(
                f"git {log_cmd or ' '.join(args)} failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return result.stdout

    @classmethod
    def get_remote_default_branch(cls, url: str) -> str:
        output = cls._run(["ls-remote", "--symref", url, "HEAD"], cwd=Path.cwd())
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("ref:"):
                # e.g. "ref: refs/heads/master\tHEAD"
                ref = line.split()[1]
                return ref.rsplit("/", 1)[-1]
        raise GitError(f"Could not determine the default branch for {url}")

    @classmethod
    def sync_repo(cls, url: str, dest: Path, default_branch: str, shallow: bool = True) -> None:
        """Clones the repo if it isn't present locally yet. If it is already
        present (from a previous run), force-checks it out to the default
        branch and hard-resets it to match origin, discarding any local
        changes or leftover branches from a previous --create-pr run, so
        dependency analysis always runs against the latest upstream code.
        """
        if dest.exists():
            LOGGER.info(
                "%s already cloned locally; fetching and resetting to latest '%s'",
                dest.name, default_branch,
            )
            fetch_args = ["fetch", "origin", default_branch]
            if shallow:
                fetch_args += ["--depth", "1"]
            cls._run(fetch_args, cwd=dest)
            cls._run(["checkout", "-f", default_branch], cwd=dest)
            cls._run(["reset", "--hard", f"origin/{default_branch}"], cwd=dest)
            cls._run(["clean", "-fd"], cwd=dest)
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--branch", default_branch]
        if shallow:
            args += ["--depth", "1"]
        args += [url, str(dest)]
        cls._run(args, cwd=dest.parent)

    @classmethod
    def checkout_branch(cls, repo_dir: Path, branch: str, base: str) -> None:
        cls._run(["checkout", base], cwd=repo_dir)
        try:
            cls._run(["branch", "-D", branch], cwd=repo_dir)
        except GitError:
            pass  # branch did not exist locally yet
        cls._run(["checkout", "-b", branch], cwd=repo_dir)

    @classmethod
    def commit_all(cls, repo_dir: Path, files: List[str], message: str) -> bool:
        cls._run(["add"] + files, cwd=repo_dir)
        status = cls._run(["status", "--porcelain"], cwd=repo_dir)
        if not status.strip():
            return False
        cls._run(
            [
                "-c", f"user.email={BOT_EMAIL}",
                "-c", f"user.name={BOT_NAME}",
                "commit", "-m", message,
            ],
            cwd=repo_dir,
        )
        return True

    @classmethod
    def push_branch(cls, repo_dir: Path, branch: str, org: str, repo: str, token: Optional[str]) -> None:
        if token:
            remote_url = f"https://{token}@github.com/{org}/{repo}.git"
        else:
            remote_url = f"https://github.com/{org}/{repo}.git"
        cls._run(
            ["push", "-f", remote_url, f"HEAD:refs/heads/{branch}"],
            cwd=repo_dir,
            log_cmd=f"push -f https://***@github.com/{org}/{repo}.git HEAD:refs/heads/{branch}",
        )


# --------------------------------------------------------------------------
# setup.py static parsing
# --------------------------------------------------------------------------

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


def _extract_string_list(node: ast.AST, symtable: Dict[str, ast.AST]) -> List[Tuple[str, Optional[ast.AST]]]:
    node = _resolve_name(node, symtable)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items: List[Tuple[str, Optional[ast.AST]]] = []
        for element in node.elts:
            try:
                resolved = _resolve_name(element, symtable) if isinstance(element, ast.Name) else element
            except SetupPyParseError:
                continue
            if isinstance(resolved, ast.Constant) and isinstance(resolved.value, str):
                items.append((resolved.value, resolved))
            # Non-literal entries (f-strings, function calls, etc.) can't be
            # statically resolved; they are silently skipped rather than
            # aborting the whole tap.
        return items
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _extract_string_list(node.left, symtable) + _extract_string_list(node.right, symtable)
    raise SetupPyParseError(f"Unsupported dependency list expression: {ast.dump(node)[:80]}")


def _extract_extras_dict(
    node: ast.AST, symtable: Dict[str, ast.AST]
) -> Dict[str, List[Tuple[str, Optional[ast.AST]]]]:
    node = _resolve_name(node, symtable)
    if not isinstance(node, ast.Dict):
        raise SetupPyParseError("extras_require is not a dict literal")
    result: Dict[str, List[Tuple[str, Optional[ast.AST]]]] = {}
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
        except SetupPyParseError as exc:
            LOGGER.debug("Skipping extras_require key %r: %s", resolved_key.value, exc)
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


def parse_setup_py(
    setup_py_path: Path, tap_name: str, repo_url: str
) -> Tuple[List[ParsedDependency], str]:
    """Statically parses setup.py and returns (dependencies, source_text).

    Static AST parsing (rather than executing setup.py) is used deliberately:
    it avoids running arbitrary/untrusted code from cloned repositories while
    still covering the vast majority of real-world Singer tap setup.py files.
    """
    source = setup_py_path.read_text(encoding="utf-8", errors="replace")
    if source.startswith("\ufeff"):
        source = source[1:]  # strip a UTF-8 BOM if present
    try:
        tree = ast.parse(source, filename=str(setup_py_path))
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

    deps: List[ParsedDependency] = []

    install_requires_node = _get_keyword_value(setup_call, "install_requires")
    if install_requires_node is not None:
        try:
            for raw, node in _extract_string_list(install_requires_node, symtable):
                deps.append(_build_dependency(tap_name, repo_url, MAIN_DEP_TYPE, None, raw, node))
        except SetupPyParseError as exc:
            LOGGER.warning("%s: could not fully parse install_requires: %s", tap_name, exc)

    extras_node = _get_keyword_value(setup_call, "extras_require")
    if extras_node is not None:
        try:
            extras = _extract_extras_dict(extras_node, symtable)
            for extra_name, entries in extras.items():
                for raw, node in entries:
                    deps.append(
                        _build_dependency(tap_name, repo_url, DEV_DEP_TYPE, extra_name, raw, node)
                    )
        except SetupPyParseError as exc:
            LOGGER.warning("%s: could not fully parse extras_require: %s", tap_name, exc)

    return deps, source


def _build_dependency(
    tap_name: str,
    repo_url: str,
    dep_type: str,
    extra_name: Optional[str],
    raw: str,
    node: Optional[ast.AST],
) -> ParsedDependency:
    requirement: Optional[Requirement] = None
    parse_error: Optional[str] = None
    name = raw.strip()
    try:
        requirement = Requirement(raw)
        name = requirement.name
    except InvalidRequirement as exc:
        parse_error = f"Could not parse requirement string {raw!r}: {exc}"

    span = None
    if node is not None and hasattr(node, "lineno"):
        span = (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)

    return ParsedDependency(
        tap_name=tap_name,
        repo_url=repo_url,
        dep_type=dep_type,
        extra_name=extra_name,
        raw_requirement=raw,
        name=name,
        requirement=requirement,
        parse_error=parse_error,
        source_span=span,
    )


def is_stdlib_dependency(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return normalized in STDLIB_MODULES


# --------------------------------------------------------------------------
# Version comparison
# --------------------------------------------------------------------------

def _extract_baseline_version(specifier: SpecifierSet) -> Optional[Version]:
    """Best-effort 'anchor' version referenced by a specifier set.

    Prefers an exact pin, falls back to a lower/compatible bound. Returns
    None when the specifier set has no version anchor to compare against.
    """
    exact = None
    lower_bound = None
    for spec in specifier:
        try:
            version = Version(spec.version)
        except InvalidVersion:
            continue
        if spec.operator == "==":
            exact = version
        elif spec.operator in (">=", "~=", ">") and (lower_bound is None or version > lower_bound):
            lower_bound = version
    if exact is not None:
        return exact
    return lower_bound


_CHANGE_TYPE_RANK = {"Major": 3, "Minor": 2, "Patch": 1}


def _normalize_release(version: Version, length: int = 3) -> Tuple[int, ...]:
    parts = list(version.release[:length])
    while len(parts) < length:
        parts.append(0)
    return tuple(parts)


def classify_version_change(old_version: Version, new_version: Version) -> str:
    """Classifies a version bump as "Major", "Minor", or "Patch" using
    semantic-versioning rules on the release segment (major.minor.micro).
    Returns "" when the two versions have the same release numbers (e.g. only
    a pre-release/local segment differs).
    """
    old_release = _normalize_release(old_version)
    new_release = _normalize_release(new_version)
    if old_release == new_release:
        return ""
    if old_release[0] != new_release[0]:
        return "Major"
    if old_release[1] != new_release[1]:
        return "Minor"
    return "Patch"


def build_report_row(dep: ParsedDependency, lookup: Optional[PyPILookupResult]) -> ReportRow:
    installed_display = "(unspecified)"
    needs_update = "No"
    update_available = "No"
    status = STATUS_UNABLE_TO_CHECK
    notes = ""
    latest_display = ""

    if dep.parse_error:
        return ReportRow(
            tap_name=dep.tap_name,
            repository=dep.repo_url,
            dep_type=dep.dep_type,
            library_name=dep.name or dep.raw_requirement,
            installed_version=dep.raw_requirement,
            latest_version="",
            needs_update="No",
            update_available="No",
            status=STATUS_UNABLE_TO_CHECK,
            notes=dep.parse_error,
        )

    requirement = dep.requirement
    assert requirement is not None
    if requirement.specifier:
        installed_display = str(requirement.specifier)
        if installed_display.startswith("=="):
            # Show a bare version number for exact pins (the common case);
            # keep the operator for ranges (>=, ~=, etc.) where it's meaningful.
            installed_display = installed_display[2:]

    if lookup is None:
        notes = "PyPI lookup was not performed"
        return ReportRow(
            dep.tap_name, dep.repo_url, dep.dep_type, dep.name, installed_display,
            "", "No", "No", STATUS_UNABLE_TO_CHECK, notes,
        )

    if lookup.status == "not_found":
        return ReportRow(
            dep.tap_name, dep.repo_url, dep.dep_type, dep.name, installed_display,
            "", "No", "No", STATUS_NOT_FOUND, lookup.error or "Package not found on PyPI",
        )

    if lookup.status == "error" or not lookup.latest_version:
        return ReportRow(
            dep.tap_name, dep.repo_url, dep.dep_type, dep.name, installed_display,
            "", "No", "No", STATUS_UNABLE_TO_CHECK, lookup.error or "Unable to check PyPI",
        )

    latest_display = lookup.latest_version
    try:
        latest_version = Version(lookup.latest_version)
    except InvalidVersion:
        return ReportRow(
            dep.tap_name, dep.repo_url, dep.dep_type, dep.name, installed_display,
            latest_display, "No", "No", STATUS_UNABLE_TO_CHECK,
            f"Invalid latest version string from PyPI: {lookup.latest_version!r}",
        )

    if not requirement.specifier:
        return ReportRow(
            dep.tap_name, dep.repo_url, dep.dep_type, dep.name, installed_display,
            latest_display, "No", "No", STATUS_UP_TO_DATE, "No version pin specified",
        )

    baseline = _extract_baseline_version(requirement.specifier)
    update_available = "Yes" if (baseline is not None and latest_version > baseline) else "No"
    version_change_type = classify_version_change(baseline, latest_version) if baseline is not None else ""

    try:
        satisfied = requirement.specifier.contains(latest_version, prereleases=False)
    except InvalidSpecifier:
        return ReportRow(
            dep.tap_name, dep.repo_url, dep.dep_type, dep.name, installed_display,
            latest_display, "No", update_available, STATUS_UNABLE_TO_CHECK,
            "Invalid version specifier", version_change_type,
        )

    if satisfied:
        needs_update = "No"
        status = STATUS_UP_TO_DATE
        notes = ""
    else:
        needs_update = "Yes"
        status = STATUS_UPDATE_REQUIRED
        notes = "Current constraint does not allow the latest PyPI release"

    return ReportRow(
        dep.tap_name, dep.repo_url, dep.dep_type, dep.name, installed_display,
        latest_display, needs_update, update_available, status, notes, version_change_type,
    )


# --------------------------------------------------------------------------
# Report generation
# --------------------------------------------------------------------------

def sort_report_rows(rows: List[ReportRow]) -> List[ReportRow]:
    taps_needing_update = {
        row.tap_name for row in rows if row.needs_update == "Yes"
    }

    def tap_sort_key(tap_name: str) -> Tuple[int, str]:
        return (0 if tap_name in taps_needing_update else 1, tap_name.lower())

    tap_order = sorted({row.tap_name for row in rows}, key=tap_sort_key)
    tap_index = {name: i for i, name in enumerate(tap_order)}

    def row_sort_key(row: ReportRow):
        return (
            tap_index[row.tap_name],
            0 if row.needs_update == "Yes" else 1,
            row.dep_type,
            row.library_name.lower(),
        )

    return sorted(rows, key=row_sort_key)


@dataclass
class TapSummary:
    tap_name: str
    total: int
    main_count: int
    dev_count: int
    needing_update: int
    up_to_date: int
    overall_status: str
    overall_change_type: str = ""
    libraries_requiring_updates: str = ""


def build_summaries(rows: List[ReportRow]) -> List[TapSummary]:
    by_tap: Dict[str, List[ReportRow]] = {}
    for row in rows:
        by_tap.setdefault(row.tap_name, []).append(row)

    summaries = []
    for tap_name, tap_rows in by_tap.items():
        total = len(tap_rows)
        main_count = sum(1 for r in tap_rows if r.dep_type == MAIN_DEP_TYPE)
        dev_count = sum(1 for r in tap_rows if r.dep_type == DEV_DEP_TYPE)
        needing_update = sum(1 for r in tap_rows if r.needs_update == "Yes")
        up_to_date = sum(1 for r in tap_rows if r.status == STATUS_UP_TO_DATE)
        if needing_update > 0:
            overall = STATUS_UPDATE_REQUIRED
        elif any(r.status in (STATUS_NOT_FOUND, STATUS_UNABLE_TO_CHECK) for r in tap_rows):
            overall = "NEEDS_REVIEW"
        else:
            overall = STATUS_UP_TO_DATE

        needing_update_rows = [r for r in tap_rows if r.needs_update == "Yes"]
        overall_change_type = ""
        best_rank = 0
        for r in needing_update_rows:
            rank = _CHANGE_TYPE_RANK.get(r.version_change_type, 0)
            if rank > best_rank:
                best_rank = rank
                overall_change_type = r.version_change_type
        libraries_requiring_updates = "; ".join(
            f"{r.library_name}: {r.installed_version} -> {r.latest_version}" for r in needing_update_rows
        )

        summaries.append(
            TapSummary(
                tap_name, total, main_count, dev_count, needing_update, up_to_date, overall,
                overall_change_type, libraries_requiring_updates,
            )
        )

    summaries.sort(key=lambda s: (0 if s.needing_update > 0 else 1, s.tap_name.lower()))
    return summaries


def _load_qtc_status(qtc_status_path: Optional[Path]) -> Optional[Dict[str, str]]:
    """Load optional TAP_NAME -> release_ff QTC status mapping."""
    if qtc_status_path is None or not qtc_status_path.exists():
        return None
    if not HAVE_OPENPYXL:
        raise RuntimeError(f"{qtc_status_path} was found, but openpyxl is required to read it.")

    try:
        wb = openpyxl.load_workbook(qtc_status_path, read_only=True, data_only=True)
    except Exception as exc:
        raise RuntimeError(f"Could not read QTC status file {qtc_status_path}: {exc}") from exc

    try:
        if not wb.sheetnames:
            raise ValueError("workbook contains no worksheets")
        ws = wb[wb.sheetnames[0]]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration as exc:
            raise ValueError("workbook is empty") from exc

        headers = {str(value).strip().lower(): index for index, value in enumerate(header_row) if value is not None}
        tap_col = headers.get(QTC_TAP_NAME_COLUMN.lower())
        status_col = headers.get(QTC_STATUS_SOURCE_COLUMN.lower())
        if tap_col is None or status_col is None:
            raise ValueError(
                f"expected columns '{QTC_TAP_NAME_COLUMN}' and '{QTC_STATUS_SOURCE_COLUMN}' in the first worksheet"
            )

        mapping: Dict[str, str] = {}
        duplicate_taps = set()
        for row in rows_iter:
            if tap_col >= len(row) or row[tap_col] is None or not str(row[tap_col]).strip():
                continue
            tap_name = str(row[tap_col]).strip()
            status = str(row[status_col]).strip() if status_col < len(row) and row[status_col] is not None else ""
            if tap_name in mapping:
                duplicate_taps.add(tap_name)
            mapping[tap_name] = status

        if duplicate_taps:
            sample = ", ".join(sorted(duplicate_taps)[:10])
            suffix = " ..." if len(duplicate_taps) > 10 else ""
            raise ValueError(f"duplicate TAP_NAME values found: {sample}{suffix}")

        LOGGER.info("Loaded QTC status mapping from %s: %d tap(s)", qtc_status_path, len(mapping))
        return mapping
    finally:
        wb.close()


def write_report(
    rows: List[ReportRow],
    output_path: str,
    qtc_status_path: Optional[Path] = None,
) -> None:
    sorted_rows = sort_report_rows(rows)
    summaries = build_summaries(rows)

    if HAVE_OPENPYXL and output_path.lower().endswith(".xlsx"):
        _write_xlsx(sorted_rows, summaries, output_path, qtc_status_path=qtc_status_path)
    else:
        if output_path.lower().endswith(".xlsx") and not HAVE_OPENPYXL:
            LOGGER.warning("openpyxl is not installed; writing CSV files instead of .xlsx")
        _write_csv(sorted_rows, summaries, output_path, qtc_status_path=qtc_status_path)


# Leading characters that Excel/other spreadsheet apps may misinterpret as the
# start of a formula (e.g. a version string like "==3.3.6" reads as "=(=3.3.6)"
# once opened as CSV, which is what actually made that column look blank).
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _spreadsheet_safe(value):
    if isinstance(value, str) and value.startswith(_FORMULA_TRIGGER_CHARS):
        return "'" + value
    return value


def _write_csv(
    rows: List[ReportRow],
    summaries: List[TapSummary],
    output_path: str,
    qtc_status_path: Optional[Path] = None,
) -> None:
    qtc_status = _load_qtc_status(qtc_status_path)
    report_columns = REPORT_COLUMNS + ([QTC_STATUS_REPORT_COLUMN] if qtc_status is not None else [])
    summary_columns = SUMMARY_COLUMNS + ([QTC_STATUS_REPORT_COLUMN] if qtc_status is not None else [])
    base = output_path
    if base.lower().endswith(".xlsx"):
        base = base[: -len(".xlsx")] + ".csv"
    elif not base.lower().endswith(".csv"):
        base = base + ".csv"

    with open(base, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(report_columns)
        for row in rows:
            writer.writerow(
                [
                    _spreadsheet_safe(v) for v in (
                        row.tap_name, row.repository, row.dep_type, row.library_name,
                        row.installed_version, row.latest_version, row.needs_update,
                        row.update_available, row.version_change_type, row.status, row.notes,
                    )
                    + ((qtc_status.get(row.tap_name, ""),) if qtc_status is not None else ())
                ]
            )
    LOGGER.info("Wrote detail report to %s", base)

    summary_path = base[: -len(".csv")] + ".summary.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(summary_columns)
        for s in summaries:
            writer.writerow(
                [
                    _spreadsheet_safe(v) for v in (
                        s.tap_name, s.total, s.main_count, s.dev_count, s.needing_update,
                        s.up_to_date, s.overall_change_type, s.libraries_requiring_updates, s.overall_status,
                    )
                    + ((qtc_status.get(s.tap_name, ""),) if qtc_status is not None else ())
                ]
            )
    LOGGER.info("Wrote summary report to %s", summary_path)


_STATUS_FILL_COLORS = {
    STATUS_UPDATE_REQUIRED: "FFF4CCCC",
    STATUS_UP_TO_DATE: "FFD9EAD3",
    STATUS_NOT_FOUND: "FFEFEFEF",
    STATUS_UNABLE_TO_CHECK: "FFFFF2CC",
    "NEEDS_REVIEW": "FFFFF2CC",
}


def _style_header(ws, num_columns: int) -> None:
    header_font = Font(bold=True, color="FFFFFFFF")
    header_fill = PatternFill(start_color="FF4472C4", end_color="FF4472C4", fill_type="solid")
    for col in range(1, num_columns + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def _save_workbook_atomically(wb, output_path: str, attempts: int = 5) -> None:
    """Saves to a temp file then renames into place, retrying briefly on
    Windows file locks caused by antivirus scans, search indexing, or the
    file being open for preview in an editor.
    """
    tmp_path = f"{output_path}.tmp-{os.getpid()}"
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            wb.save(tmp_path)
            os.replace(tmp_path, output_path)
            return
        except OSError as exc:
            last_exc = exc
            Path(tmp_path).unlink(missing_ok=True)
            if attempt < attempts:
                LOGGER.warning(
                    "Could not save %s (attempt %d/%d, %s); retrying...",
                    output_path, attempt, attempts, exc,
                )
                time.sleep(2 * attempt)
    raise last_exc


def _write_xlsx(
    rows: List[ReportRow],
    summaries: List[TapSummary],
    output_path: str,
    qtc_status_path: Optional[Path] = None,
) -> None:
    qtc_status = _load_qtc_status(qtc_status_path)
    report_columns = REPORT_COLUMNS + ([QTC_STATUS_REPORT_COLUMN] if qtc_status is not None else [])
    summary_columns = SUMMARY_COLUMNS + ([QTC_STATUS_REPORT_COLUMN] if qtc_status is not None else [])
    wb = openpyxl.Workbook()

    version_cols = {
        REPORT_COLUMNS.index("Installed/Specified Version") + 1,
        REPORT_COLUMNS.index("Latest PyPI Version") + 1,
    }

    detail_ws = wb.active
    detail_ws.title = "Dependency Audit"
    detail_ws.append(report_columns)
    for row in rows:
        detail_ws.append(
            [
                _spreadsheet_safe(v) for v in (
                    row.tap_name, row.repository, row.dep_type, row.library_name,
                    row.installed_version, row.latest_version, row.needs_update,
                    row.update_available, row.version_change_type, row.status, row.notes,
                )
                + ((qtc_status.get(row.tap_name, ""),) if qtc_status is not None else ())
            ]
        )
        status_col = REPORT_COLUMNS.index("Status") + 1
        fill_color = _STATUS_FILL_COLORS.get(row.status)
        cell = detail_ws.cell(row=detail_ws.max_row, column=status_col)
        if fill_color:
            cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        for col in range(1, len(report_columns) + 1):
            data_cell = detail_ws.cell(row=detail_ws.max_row, column=col)
            data_cell.alignment = Alignment(vertical="top", wrap_text=True)
            if col in version_cols:
                # Force Text format so Excel never reinterprets e.g. "==3.3.6"
                # as a formula/number and silently blanks or reformats it.
                data_cell.number_format = "@"

    column_widths = [22, 40, 20, 28, 26, 20, 13, 16, 18, 18, 45]
    if qtc_status is not None:
        column_widths.append(30)
    for i, width in enumerate(column_widths, start=1):
        detail_ws.column_dimensions[get_column_letter(i)].width = width
    _style_header(detail_ws, len(report_columns))

    summary_ws = wb.create_sheet("Summary")
    summary_ws.append(summary_columns)
    for s in summaries:
        summary_ws.append(
            [
                _spreadsheet_safe(v) for v in (
                    s.tap_name, s.total, s.main_count, s.dev_count, s.needing_update,
                    s.up_to_date, s.overall_change_type, s.libraries_requiring_updates, s.overall_status,
                )
                + ((qtc_status.get(s.tap_name, ""),) if qtc_status is not None else ())
            ]
        )
        status_col = SUMMARY_COLUMNS.index("Overall Status") + 1
        fill_color = _STATUS_FILL_COLORS.get(s.overall_status)
        cell = summary_ws.cell(row=summary_ws.max_row, column=status_col)
        if fill_color:
            cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        for col in range(1, len(summary_columns) + 1):
            summary_ws.cell(row=summary_ws.max_row, column=col).alignment = Alignment(
                vertical="top", wrap_text=True
            )

    summary_widths = [26, 18, 18, 16, 18, 14, 20, 55, 18]
    if qtc_status is not None:
        summary_widths.append(30)
    for i, width in enumerate(summary_widths, start=1):
        summary_ws.column_dimensions[get_column_letter(i)].width = width
    _style_header(summary_ws, len(summary_columns))

    _save_workbook_atomically(wb, output_path)
    LOGGER.info("Wrote Excel report to %s", output_path)


# --------------------------------------------------------------------------
# Auditing (discovery + clone + parse + PyPI check)
# --------------------------------------------------------------------------

def audit_tap(
    tap_name: str,
    org: str,
    workdir: Path,
    pypi_client: PyPIClient,
    shallow: bool = True,
) -> List[ReportRow]:
    repo_url = f"https://github.com/{org}/{tap_name}.git"
    web_url = f"https://github.com/{org}/{tap_name}"
    dest = workdir / tap_name

    try:
        default_branch = GitOps.get_remote_default_branch(repo_url)
        GitOps.sync_repo(repo_url, dest, default_branch, shallow=shallow)
    except GitError as exc:
        LOGGER.error("Failed to clone/update %s: %s", tap_name, exc)
        return [
            ReportRow(
                tap_name, web_url, "N/A", "N/A", "", "", "No", "No",
                STATUS_UNABLE_TO_CHECK, f"Failed to clone repository: {exc}",
            )
        ]

    setup_py_path = dest / "setup.py"
    if not setup_py_path.exists():
        LOGGER.warning("%s has no setup.py", tap_name)
        return [
            ReportRow(
                tap_name, web_url, "N/A", "N/A", "", "", "No", "No",
                STATUS_UNABLE_TO_CHECK, "No setup.py found in repository",
            )
        ]

    try:
        deps, _source = parse_setup_py(setup_py_path, tap_name, web_url)
    except SetupPyParseError as exc:
        LOGGER.error("%s: failed to parse setup.py: %s", tap_name, exc)
        return [
            ReportRow(
                tap_name, web_url, "N/A", "N/A", "", "", "No", "No",
                STATUS_UNABLE_TO_CHECK, f"Failed to parse setup.py: {exc}",
            )
        ]

    deps = [d for d in deps if not (d.requirement and is_stdlib_dependency(d.name))]

    if not deps:
        return [
            ReportRow(
                tap_name, web_url, "N/A", "N/A", "", "", "No", "No",
                STATUS_UP_TO_DATE, "No third-party dependencies found",
            )
        ]

    rows = []
    for dep in deps:
        if dep.parse_error:
            rows.append(build_report_row(dep, None))
            continue
        lookup = pypi_client.get_latest_version(dep.name)
        rows.append(build_report_row(dep, lookup))
    return rows


def run_audit(
    tap_names: List[str],
    org: str,
    workdir: Path,
    pypi_client: PyPIClient,
    shallow: bool = True,
) -> List[ReportRow]:
    all_rows: List[ReportRow] = []
    for i, tap_name in enumerate(tap_names, start=1):
        LOGGER.info("[%d/%d] Auditing %s", i, len(tap_names), tap_name)
        try:
            all_rows.extend(audit_tap(tap_name, org, workdir, pypi_client, shallow=shallow))
        except Exception as exc:  # noqa: BLE001 - keep the audit going no matter what
            LOGGER.exception("Unexpected error auditing %s: %s", tap_name, exc)
            all_rows.append(
                ReportRow(
                    tap_name, f"https://github.com/{org}/{tap_name}", "N/A", "N/A",
                    "", "", "No", "No", STATUS_UNABLE_TO_CHECK, f"Unexpected error: {exc}",
                )
            )
    return all_rows


# --------------------------------------------------------------------------
# Dependency updating (for --create-pr mode)
# --------------------------------------------------------------------------

def _offset_of(source: str, lineno: int, col: int) -> int:
    lines = source.splitlines(keepends=True)
    return sum(len(l) for l in lines[: lineno - 1]) + col


def _apply_edits(source: str, edits: List[Tuple[int, int, str]]) -> str:
    # Apply from the end of the file backwards so earlier offsets stay valid.
    result = source
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def compute_dependency_updates(
    tap_name: str,
    setup_py_path: Path,
    pypi_client: PyPIClient,
) -> Tuple[Optional[str], List[DependencyChange], Optional[str], Optional[str]]:
    """Computes an updated setup.py source (or None if no changes) plus a
    list of the individual dependency changes made, and the tap's own
    ``version=`` before/after a patch bump (None/None if there were no
    changes, or if the version literal couldn't be statically determined).

    Only exact pins (``package==X.Y.Z``) are auto-updated, and only when a
    strictly newer stable release exists on PyPI. Range/compatible-release
    specifiers are intentionally left untouched: if the latest release
    already satisfies them nothing needs to change, and if it doesn't the
    constraint was chosen deliberately and should be reviewed by a human
    rather than silently loosened or rewritten.
    """
    repo_url = f"https://github.com/singer-io/{tap_name}"
    deps, source = parse_setup_py(setup_py_path, tap_name, repo_url)

    edits: List[Tuple[int, int, str]] = []
    changes: List[DependencyChange] = []

    for dep in deps:
        if dep.parse_error or dep.requirement is None or dep.source_span is None:
            continue
        if is_stdlib_dependency(dep.name):
            continue

        specifier = dep.requirement.specifier
        exact_pins = [s for s in specifier if s.operator == "=="]
        if len(exact_pins) != 1 or len(list(specifier)) != 1:
            continue  # only touch simple single "==" pins

        old_version_str = exact_pins[0].version
        try:
            old_version = Version(old_version_str)
        except InvalidVersion:
            continue

        lookup = pypi_client.get_latest_version(dep.name)
        if lookup.status != "ok" or not lookup.latest_version:
            continue
        try:
            latest_version = Version(lookup.latest_version)
        except InvalidVersion:
            continue

        if latest_version <= old_version:
            continue

        start_line, start_col, end_line, end_col = dep.source_span
        start_offset = _offset_of(source, start_line, start_col)
        end_offset = _offset_of(source, end_line, end_col)
        segment = source[start_offset:end_offset]
        if len(segment) < 2 or segment[0] not in ("'", '"'):
            continue  # unexpected literal shape; skip rather than risk corrupting the file
        quote = segment[0]
        new_raw = dep.raw_requirement.replace(old_version_str, str(latest_version), 1)
        new_segment = f"{quote}{new_raw}{quote}"
        edits.append((start_offset, end_offset, new_segment))
        change_type = classify_version_change(old_version, latest_version) or "Patch"
        changes.append(DependencyChange(dep.name, dep.dep_type, old_version_str, str(latest_version), change_type))

    if not changes:
        return None, [], None, None

    overall_change_type = _overall_change_type_for_changes(changes)
    old_tap_version, version_span = _extract_tap_version_from_source(source)
    new_tap_version = None
    if old_tap_version is not None and version_span is not None:
        bumped = bump_version_for_change_type(old_tap_version, overall_change_type)
        if bumped and bumped != old_tap_version:
            start_line, start_col, end_line, end_col = version_span
            start_offset = _offset_of(source, start_line, start_col)
            end_offset = _offset_of(source, end_line, end_col)
            segment = source[start_offset:end_offset]
            if len(segment) >= 2 and segment[0] in ("'", '"'):
                quote = segment[0]
                edits.append((start_offset, end_offset, f"{quote}{bumped}{quote}"))
                new_tap_version = bumped
            else:
                LOGGER.debug(
                    "%s: version literal has an unexpected shape; leaving tap version unchanged", tap_name
                )

    updated_source = _apply_edits(source, edits)
    return updated_source, changes, old_tap_version, new_tap_version


def _overall_change_type_for_changes(changes: List[DependencyChange]) -> str:
    """Highest-severity change type across all changes (Major > Minor > Patch),
    used to decide how much to bump the tap's own version.
    """
    best = "Patch"
    best_rank = 0
    for change in changes:
        rank = _CHANGE_TYPE_RANK.get(change.change_type, 1)
        if rank > best_rank:
            best_rank = rank
            best = change.change_type or "Patch"
    return best


def bump_version_for_change_type(version_str: str, change_type: str) -> Optional[str]:
    """Increments the version component matching the given change severity
    ("Major" -> 1st component, "Minor" -> 2nd, "Patch"/other -> last),
    zeroing any less-significant components, e.g.
    bump_version_for_change_type("2.2.1", "Minor") -> "2.3.0". Returns None if
    the string isn't in plain dotted-numeric form (left alone rather than
    guessed).
    """
    parts = version_str.strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if change_type == "Major":
        idx = 0
    elif change_type == "Minor":
        idx = 1 if len(nums) > 1 else 0
    else:
        idx = len(nums) - 1
    idx = min(idx, len(nums) - 1)
    nums[idx] += 1
    for i in range(idx + 1, len(nums)):
        nums[i] = 0
    return ".".join(str(n) for n in nums)


def _extract_tap_version_from_source(
    source: str,
) -> Tuple[Optional[str], Optional[Tuple[int, int, int, int]]]:
    """Statically extracts the setup() call's version="..." literal and its
    source span from an already-parsed setup.py source string.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None, None
    symtable: Dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symtable[target.id] = node.value
    setup_call = _find_setup_call(tree)
    if setup_call is None:
        return None, None
    version_node = _get_keyword_value(setup_call, "version")
    if version_node is None:
        return None, None
    try:
        resolved = _resolve_name(version_node, symtable) if isinstance(version_node, ast.Name) else version_node
    except SetupPyParseError:
        return None, None
    if isinstance(resolved, ast.Constant) and isinstance(resolved.value, str):
        span = (resolved.lineno, resolved.col_offset, resolved.end_lineno, resolved.end_col_offset)
        return resolved.value, span
    return None, None


# --------------------------------------------------------------------------
# CHANGELOG.md updating
# --------------------------------------------------------------------------

def build_changelog_entry(changes: List[DependencyChange], bullet: str, heading: str) -> str:
    lines = [heading, ""]
    for change in changes:
        change_note = f" ({change.change_type})" if change.change_type else ""
        lines.append(
            f"{bullet} Bump `{change.name}` from `{change.old_version}` to "
            f"`{change.new_version}` ({change.dep_type}{change_note})"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


_CHANGELOG_HEADING_RE = re.compile(r"^(#{2,4})\s*\[?v?(\d+\.\d+(?:\.\d+)?)\]?", re.MULTILINE)


def _detect_changelog_heading_style(content: str) -> Tuple[str, bool]:
    """Infers (hash_prefix, use_brackets) from the first existing version
    heading, e.g. ('##', True) for '## [1.2.3]' or ('###', False) for
    '### 1.2.3'. Defaults to ('##', True) when no heading is found.
    """
    match = _CHANGELOG_HEADING_RE.search(content)
    if match:
        return match.group(1), "[" in match.group(0)
    return "##", True


def update_changelog(
    repo_dir: Path, changes: List[DependencyChange], new_tap_version: Optional[str] = None
) -> Tuple[bool, str]:
    """Returns (changed, new_full_content). Never rewrites existing entries.
    The new entry's heading uses ``new_tap_version`` (matching the existing
    changelog's heading style) when available, falling back to "Unreleased".
    """
    changelog_path = repo_dir / "CHANGELOG.md"
    bullet = "*"
    content = "# Changelog\n\n"
    if changelog_path.exists():
        content = changelog_path.read_text(encoding="utf-8", errors="replace")
        bullet_match = re.search(r"^\s*([*-])\s+", content, re.MULTILINE)
        if bullet_match:
            bullet = bullet_match.group(1)

    hashes, use_brackets = _detect_changelog_heading_style(content)
    if new_tap_version:
        heading = f"{hashes} [{new_tap_version}]" if use_brackets else f"{hashes} {new_tap_version}"
    else:
        heading = f"{hashes} Unreleased"

    entry_block = build_changelog_entry(changes, bullet, heading)

    lines = content.splitlines(keepends=True)
    insert_idx = 0
    if lines and lines[0].lstrip().startswith("#") and not lines[0].lstrip().startswith("##"):
        insert_idx = 1
        while insert_idx < len(lines) and lines[insert_idx].strip() == "":
            insert_idx += 1

    new_content = "".join(lines[:insert_idx])
    if insert_idx > 0:
        new_content += "\n"
    new_content += entry_block + "\n" + "".join(lines[insert_idx:])
    return True, new_content


# --------------------------------------------------------------------------
# PR body generation
# --------------------------------------------------------------------------

def build_pr_body(
    tap_name: str,
    changes: List[DependencyChange],
    old_tap_version: Optional[str] = None,
    new_tap_version: Optional[str] = None,
) -> str:
    main_changes = [c for c in changes if c.dep_type == MAIN_DEP_TYPE]
    dev_changes = [c for c in changes if c.dep_type == DEV_DEP_TYPE]

    lines = [
        f"This PR updates outdated dependency pins in `{tap_name}` based on an",
        "automated dependency audit against the latest PyPI releases.",
        "",
    ]
    if new_tap_version:
        lines += [f"Tap version bumped: `{old_tap_version}` -> `{new_tap_version}`.", ""]
    lines += [
        "## Dependencies updated",
        "",
        "| Package | Old Version | New Version | Change Type | Type |",
        "|---|---|---|---|---|",
    ]
    for c in changes:
        lines.append(f"| `{c.name}` | `{c.old_version}` | `{c.new_version}` | {c.change_type} | {c.dep_type} |")

    lines += [
        "",
        f"- Runtime dependency changes: {len(main_changes)}",
        f"- Development/test dependency changes: {len(dev_changes)}",
        "",
        "## Changelog",
        "",
        "`CHANGELOG.md` has been updated with an entry describing these changes.",
        "",
        "_This PR was created automatically by the Singer Tap Dependency Auditor._",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# PR mode orchestration
# --------------------------------------------------------------------------

def process_tap_for_pr(
    tap_name: str,
    org: str,
    workdir: Path,
    gh_client: GitHubClient,
    pypi_client: PyPIClient,
    token: Optional[str],
    dry_run: bool,
    fork_owner: Optional[str] = None,
    branch_name: str = DEFAULT_BRANCH_NAME,
) -> PRResult:
    LOGGER.info("Processing %s for PR creation%s", tap_name, " (dry-run)" if dry_run else "")

    repo_info = gh_client.get_repo(org, tap_name)
    if repo_info is None:
        LOGGER.error("Repository %s/%s does not exist; skipping", org, tap_name)
        return PRResult(tap_name, "SKIPPED", notes="Repository does not exist")
    default_branch = repo_info.get("default_branch", "master")

    repo_url = f"https://github.com/{org}/{tap_name}.git"
    dest = workdir / tap_name
    try:
        GitOps.sync_repo(repo_url, dest, default_branch, shallow=False)
    except GitError as exc:
        LOGGER.error("Failed to clone/update %s: %s", tap_name, exc)
        return PRResult(tap_name, "FAILED", notes=f"Failed to clone/update: {exc}")

    setup_py_path = dest / "setup.py"
    if not setup_py_path.exists():
        LOGGER.error("%s has no setup.py; skipping", tap_name)
        return PRResult(tap_name, "SKIPPED", notes="No setup.py found")

    try:
        updated_source, changes, old_tap_version, new_tap_version = compute_dependency_updates(
            tap_name, setup_py_path, pypi_client
        )
    except SetupPyParseError as exc:
        LOGGER.error("%s: could not parse setup.py: %s", tap_name, exc)
        return PRResult(tap_name, "FAILED", notes=f"Could not parse setup.py: {exc}")
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("%s: unexpected error computing updates: %s", tap_name, exc)
        return PRResult(tap_name, "FAILED", notes=f"Unexpected error: {exc}")

    if not changes:
        LOGGER.info("%s: no dependency updates needed, skipping PR", tap_name)
        return PRResult(tap_name, "SKIPPED_NO_UPDATES", notes="No dependency updates needed")

    LOGGER.info("%s: %d dependency update(s) found:", tap_name, len(changes))
    for c in changes:
        LOGGER.info("    %s: %s -> %s (%s)", c.name, c.old_version, c.new_version, c.dep_type)
    if new_tap_version:
        LOGGER.info("%s: tap version bumped %s -> %s", tap_name, old_tap_version, new_tap_version)

    deps_summary = "; ".join(f"{c.name} {c.old_version}->{c.new_version}" for c in changes)
    common_fields = dict(
        old_tap_version=old_tap_version or "",
        new_tap_version=new_tap_version or "",
        dependencies_updated=deps_summary,
    )

    _, changelog_content = update_changelog(dest, changes, new_tap_version)

    if dry_run:
        LOGGER.info("[dry-run] Would update setup.py and CHANGELOG.md for %s", tap_name)
        LOGGER.info("[dry-run] Would create branch '%s' from '%s'", branch_name, default_branch)
        target_owner = fork_owner or "<authenticated-user's fork>"
        LOGGER.info(
            "[dry-run] Would push to %s/%s and open a PR '%s' into %s:%s",
            target_owner, tap_name, DEFAULT_PR_TITLE, org, default_branch,
        )
        return PRResult(tap_name, "DRY_RUN", notes="Dry run - no changes were made", **common_fields)

    if not token:
        LOGGER.error(
            "%s: a GitHub token is required to fork/push/open a PR (set GITHUB_TOKEN or --token); skipping",
            tap_name,
        )
        return PRResult(tap_name, "SKIPPED_NO_TOKEN", notes="No GitHub token supplied", **common_fields)
    if not fork_owner:
        LOGGER.error("%s: could not determine the authenticated GitHub user; skipping", tap_name)
        return PRResult(
            tap_name, "FAILED", notes="Could not determine authenticated GitHub user", **common_fields
        )

    try:
        existing_pr = gh_client.find_existing_pr(org, tap_name, branch_name, head_owner=fork_owner)
    except GitHubAPIError as exc:
        LOGGER.error("%s: failed to check for existing PRs: %s", tap_name, exc)
        return PRResult(tap_name, "FAILED", notes=f"Failed to check existing PRs: {exc}", **common_fields)
    if existing_pr is not None:
        LOGGER.info(
            "%s: an equivalent dependency-update PR already exists: %s",
            tap_name, existing_pr.get("html_url"),
        )
        return PRResult(
            tap_name, "SKIPPED_EXISTING_PR", pr_url=existing_pr.get("html_url", ""),
            notes="An equivalent PR already exists", **common_fields,
        )

    try:
        gh_client.ensure_fork(org, tap_name, fork_owner)
    except GitHubAPIError as exc:
        LOGGER.error("%s: failed to fork repository into %s: %s", tap_name, fork_owner, exc)
        return PRResult(tap_name, "FAILED", notes=f"Failed to fork: {exc}", **common_fields)

    try:
        GitOps.checkout_branch(dest, branch_name, default_branch)
        setup_py_path.write_text(updated_source, encoding="utf-8")
        (dest / "CHANGELOG.md").write_text(changelog_content, encoding="utf-8")

        committed = GitOps.commit_all(
            dest, ["setup.py", "CHANGELOG.md"], DEFAULT_PR_TITLE
        )
        if not committed:
            LOGGER.info("%s: nothing to commit after formatting; skipping PR", tap_name)
            return PRResult(tap_name, "SKIPPED_NO_UPDATES", notes="Nothing to commit", **common_fields)

        GitOps.push_branch(dest, branch_name, fork_owner, tap_name, token)
    except GitError as exc:
        LOGGER.error("%s: git operation failed: %s", tap_name, exc)
        return PRResult(tap_name, "FAILED", notes=f"Git operation failed: {exc}", **common_fields)

    try:
        pr = gh_client.create_pull_request(
            org, tap_name, DEFAULT_PR_TITLE,
            build_pr_body(tap_name, changes, old_tap_version, new_tap_version),
            head=f"{fork_owner}:{branch_name}", base=default_branch,
        )
    except GitHubAPIError as exc:
        LOGGER.error("%s: failed to create pull request: %s", tap_name, exc)
        return PRResult(tap_name, "FAILED", notes=f"Failed to create pull request: {exc}", **common_fields)

    pr_url = pr.get("html_url", "")
    LOGGER.info("%s: pull request created: %s", tap_name, pr_url)
    return PRResult(tap_name, "CREATED", pr_url=pr_url, **common_fields)


PR_REPORT_COLUMNS = [
    "Tap Name", "Status", "PR Link", "Old Tap Version", "New Tap Version",
    "Dependencies Updated", "Notes",
]


def save_pr_results(results: List[PRResult], output_path: str) -> None:
    """Merges PR creation results into the same report file used for the
    dependency audit (a "PR Creation" sheet in the .xlsx, alongside
    "Dependency Audit"/"Summary" if present) instead of a separate file.
    """
    if not results:
        return
    if HAVE_OPENPYXL and output_path.lower().endswith(".xlsx"):
        _merge_pr_sheet_into_xlsx(results, output_path)
    else:
        _write_pr_csv_fallback(results, output_path)


def _merge_pr_sheet_into_xlsx(results: List[PRResult], output_path: str) -> None:
    path = Path(output_path)
    wb = None
    if path.exists():
        try:
            wb = openpyxl.load_workbook(output_path)
        except Exception as exc:  # noqa: BLE001 - corrupt/locked file shouldn't lose PR results
            LOGGER.warning(
                "Could not open existing %s to merge PR results (%s); creating a new workbook",
                output_path, exc,
            )
    if wb is None:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)  # drop the default blank sheet

    if "PR Creation" in wb.sheetnames:
        del wb["PR Creation"]
    ws = wb.create_sheet("PR Creation")
    ws.append(PR_REPORT_COLUMNS)
    for r in results:
        ws.append(
            [
                _spreadsheet_safe(v) for v in (
                    r.tap_name, r.status, r.pr_url, r.old_tap_version,
                    r.new_tap_version, r.dependencies_updated, r.notes,
                )
            ]
        )
        for col in range(1, len(PR_REPORT_COLUMNS) + 1):
            ws.cell(row=ws.max_row, column=col).alignment = Alignment(vertical="top", wrap_text=True)

    column_widths = [22, 20, 45, 16, 16, 50, 35]
    for i, width in enumerate(column_widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    _style_header(ws, len(PR_REPORT_COLUMNS))

    _save_workbook_atomically(wb, output_path)
    created = sum(1 for r in results if r.status == "CREATED")
    LOGGER.info(
        "Merged PR creation results into %s ('PR Creation' sheet, %d PR(s) created)", output_path, created
    )


def _write_pr_csv_fallback(results: List[PRResult], output_path: str) -> None:
    base = output_path
    if base.lower().endswith(".xlsx"):
        base = base[: -len(".xlsx")]
    elif base.lower().endswith(".csv"):
        base = base[: -len(".csv")]
    pr_csv_path = base + ".pr_creation.csv"

    with open(pr_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(PR_REPORT_COLUMNS)
        for r in results:
            writer.writerow(
                [
                    _spreadsheet_safe(v) for v in (
                        r.tap_name, r.status, r.pr_url, r.old_tap_version,
                        r.new_tap_version, r.dependencies_updated, r.notes,
                    )
                ]
            )
    created = sum(1 for r in results if r.status == "CREATED")
    LOGGER.info("Wrote PR creation results to %s (%d PR(s) created)", pr_csv_path, created)


def run_pr_mode(
    tap_names: List[str],
    org: str,
    workdir: Path,
    gh_client: GitHubClient,
    pypi_client: PyPIClient,
    token: Optional[str],
    dry_run: bool,
    output_path: Optional[str] = None,
) -> List[PRResult]:
    fork_owner = None
    if token:
        try:
            fork_owner = gh_client.get_authenticated_user()
            LOGGER.info(
                "Authenticated as GitHub user '%s'; PR branches will be pushed to their fork", fork_owner
            )
        except GitHubAPIError as exc:
            LOGGER.warning("Could not determine the authenticated GitHub user: %s", exc)
    elif not dry_run:
        LOGGER.warning("No GitHub token supplied; forking/pushing/PR creation will be skipped for each tap")

    results: List[PRResult] = []
    for tap_name in tap_names:
        try:
            result = process_tap_for_pr(
                tap_name, org, workdir, gh_client, pypi_client, token, dry_run, fork_owner=fork_owner
            )
        except Exception as exc:  # noqa: BLE001 - one tap failing must not stop the rest
            LOGGER.exception("Unexpected error while processing %s for PR: %s", tap_name, exc)
            result = PRResult(tap_name, "FAILED", notes=f"Unexpected error: {exc}")
        results.append(result)

    if output_path:
        save_pr_results(results, output_path)
    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit and optionally patch dependency versions across singer-io tap-* repositories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python audit_taps.py\n"
            "  python audit_taps.py --output taps_dependency_report.xlsx\n"
            "  python audit_taps.py --tap tap-shopify\n"
            "  python audit_taps.py --tap tap-shopify tap-gitlab\n"
            "  python audit_taps.py --extra-taps tap-some-private-fork\n"
            "  python audit_taps.py --create-pr tap-shopify tap-gitlab\n"
            "  python audit_taps.py --create-pr tap-shopify --dry-run\n"
            "  python audit_taps.py --create-pr-all\n"
            "  python audit_taps.py --create-pr-all --dry-run\n"
        ),
    )
    parser.add_argument(
        "--output", default="taps_dependency_report.xlsx",
        help="Path to the report file to generate (default: %(default)s)",
    )
    parser.add_argument(
        "--tap", nargs="+", default=None, metavar="TAP",
        help="Audit only the specified tap(s) instead of the whole organization",
    )
    parser.add_argument(
        "--extra-taps", nargs="+", default=None, metavar="TAP",
        help=(
            "Additional tap name(s) to include manually alongside whatever "
            "--tap/discovery already selected (e.g. repos discovery might miss)"
        ),
    )
    parser.add_argument(
        "--create-pr", nargs="+", metavar="TAP", default=None,
        help="Update dependencies and open pull requests for the given tap names",
    )
    parser.add_argument(
        "--create-pr-all", action="store_true",
        help=(
            "Audit --tap/discovered/--extra-taps repositories and automatically "
            "open pull requests for every tap that needs a dependency update"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="With --create-pr/--create-pr-all, show what would change without committing/pushing/opening a PR",
    )
    parser.add_argument(
        "--org", default=DEFAULT_ORG,
        help="GitHub organization to audit (default: %(default)s)",
    )
    parser.add_argument(
        "--token", default=None,
        help="GitHub token (overrides the GITHUB_TOKEN environment variable and --config)",
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH),
        help=(
            "Path to a JSON file with {'username':..., 'api_key':...} used as a "
            "fallback GitHub token source for --create-pr when --token/GITHUB_TOKEN "
            "aren't set (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--workdir", default="./singer_tap_repos",
        help="Local directory to clone repositories into (default: %(default)s)",
    )
    parser.add_argument(
        "--log-file", default=DEFAULT_LOG_FILE,
        help="Path to a log file capturing the full run (default: %(default)s). Use '' to disable.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable debug logging",
    )
    parsed = parser.parse_args(argv)
    if parsed.create_pr and parsed.create_pr_all:
        parser.error("--create-pr and --create-pr-all are mutually exclusive")
    return parsed


def _merge_extra_taps(names: List[str], extra_taps: Optional[List[str]]) -> List[str]:
    if not extra_taps:
        return names
    merged = list(names)
    for extra in extra_taps:
        if extra not in merged:
            merged.append(extra)
    return merged


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose, log_file=args.log_file or None)

    token = args.token or os.environ.get("GITHUB_TOKEN") or load_token_from_config(Path(args.config))
    if not token:
        LOGGER.warning(
            "No GitHub token supplied; unauthenticated API requests have a low "
            "rate limit and --create-pr push/PR steps will fail."
        )

    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    # Optional QTC enrichment file. Resolve relative to the script so the
    # behavior is independent of the directory from which the script is run.
    qtc_status_path = Path(__file__).resolve().parent / DEFAULT_QTC_STATUS_FILE
    if qtc_status_path.exists():
        LOGGER.info("QTC status enrichment enabled: %s", qtc_status_path)
    else:
        qtc_status_path = None

    gh_client = GitHubClient(token)
    pypi_client = PyPIClient()

    def discover_tap_names() -> Optional[List[str]]:
        if args.tap:
            names = list(args.tap)
        else:
            LOGGER.info("Discovering tap-* repositories under '%s'...", args.org)
            try:
                names = gh_client.list_org_tap_repos(args.org)
            except GitHubAPIError as exc:
                LOGGER.error("Failed to discover repositories: %s", exc)
                return None
            LOGGER.info("Found %d tap-* repositories", len(names))
        names = _merge_extra_taps(names, args.extra_taps)
        return names

    if args.create_pr:
        tap_names = _merge_extra_taps(args.create_pr, args.extra_taps)
        run_pr_mode(tap_names, args.org, workdir, gh_client, pypi_client, token, args.dry_run, args.output)
        return 0

    if args.create_pr_all:
        tap_names = discover_tap_names()
        if tap_names is None:
            return 1
        rows = run_audit(tap_names, args.org, workdir, pypi_client, shallow=True)
        write_report(rows, args.output, qtc_status_path=qtc_status_path)
        taps_needing_update = sorted({r.tap_name for r in rows if r.needs_update == "Yes"})
        if not taps_needing_update:
            LOGGER.info("No taps require dependency updates; nothing to do for --create-pr-all")
            return 0
        LOGGER.info(
            "%d tap(s) need updates and will be processed for PR creation: %s",
            len(taps_needing_update), ", ".join(taps_needing_update),
        )
        run_pr_mode(taps_needing_update, args.org, workdir, gh_client, pypi_client, token, args.dry_run, args.output)
        return 0

    tap_names = discover_tap_names()
    if tap_names is None:
        return 1

    rows = run_audit(tap_names, args.org, workdir, pypi_client, shallow=True)
    write_report(rows, args.output, qtc_status_path=qtc_status_path)

    needing_update = len({r.tap_name for r in rows if r.needs_update == "Yes"})
    LOGGER.info(
        "Audit complete: %d tap(s) processed, %d tap(s) have at least one dependency needing an update",
        len(tap_names), needing_update,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
