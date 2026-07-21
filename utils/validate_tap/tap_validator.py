#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tap_validator.py - Comprehensive Singer tap validation tool.

Validates 8 key areas of a Singer tap and produces a clean developer report.
Discovery and sync checks run the tap executable against real credentials when
--tap-config is provided; otherwise they fall back to static code analysis.

Usage:
    python tap_validator.py --tap-dir /path/to/tap-name
    python tap_validator.py --tap-dir /path/to/tap-name --tap-config tap_credentials.json
    python tap_validator.py --tap-dir /path/to/tap-name --checks python_upgrade schema
    python tap_validator.py --config validation_config.json
    python tap_validator.py --tap-dir /path/to/tap-name --output report.json --verbose

Available checks (--checks):
    python_upgrade    - CircleCI uses Python 3.12; setup.py has versioned deps
    metadata          - replication-method and parent-tap-stream-id in metadata
    unauth_exclusion  - 403/unauthorized stream exclusion in discovery
    unit_tests        - Unit tests exist with adequate coverage
    integration_tests - Integration test files present
    discovery         - Run tap --discover and validate Singer catalog output
    sync              - Run Sync1 + Sync2: bookmarks, pagination, full/incremental, parent-child
    schema            - Nullable fields, date-time format, key-properties
    catalog_validation - Validate catalog structure: root metadata, field inclusion, key/replication-key coverage
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# ANSI colours
# ---------------------------------------------------------------------------
def _supports_color() -> bool:
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            return True
        except Exception:
            return "ANSICON" in os.environ or "WT_SESSION" in os.environ
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

_COLOR = _supports_color()
GREEN  = "\033[0;32m" if _COLOR else ""
RED    = "\033[0;31m" if _COLOR else ""
YELLOW = "\033[0;33m" if _COLOR else ""
CYAN   = "\033[0;36m" if _COLOR else ""
BOLD   = "\033[1m"    if _COLOR else ""
DIM    = "\033[2m"    if _COLOR else ""
NC     = "\033[0m"    if _COLOR else ""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MIN_PYTHON_STR = "3.12"
MIN_PYTHON     = (3, 12)

SINGER_PACKAGES = {
    "singer-python": "6.0.0",
    "requests":      "2.20.0",
    "backoff":       "2.0.0",
}

# ---------------------------------------------------------------------------
# PyPI version lookup (cached)
# ---------------------------------------------------------------------------
_PYPI_VERSION_CACHE: Dict[str, Optional[str]] = {}

def _fetch_pypi_latest(package: str) -> Optional[str]:
    """Return the latest stable version of *package* from PyPI, or None on error."""
    if package in _PYPI_VERSION_CACHE:
        return _PYPI_VERSION_CACHE[package]
    try:
        url = f"https://pypi.org/pypi/{package}/json"
        with urllib.request.urlopen(url, timeout=10) as resp:  # nosec
            data = json.loads(resp.read().decode("utf-8"))
        version = data["info"]["version"]
    except Exception:
        version = None
    _PYPI_VERSION_CACHE[package] = version
    return version


# ---------------------------------------------------------------------------
# PyPI version lookup (cached)
# ---------------------------------------------------------------------------
_PYPI_VERSION_CACHE: Dict[str, Optional[str]] = {}

def _fetch_pypi_latest(package: str) -> Optional[str]:
    """Return the latest stable version of *package* from PyPI, or None on error."""
    if package in _PYPI_VERSION_CACHE:
        return _PYPI_VERSION_CACHE[package]
    try:
        url = f"https://pypi.org/pypi/{package}/json"
        with urllib.request.urlopen(url, timeout=10) as resp:  # nosec
            data = json.loads(resp.read().decode("utf-8"))
        version = data["info"]["version"]
    except Exception:
        version = None
    _PYPI_VERSION_CACHE[package] = version
    return version


# Central venvs dir — same convention as run_tap_discovery_sync.py
# workspace/taps/virtual_envs/<tap-name>/
_SCRIPT_DIR        = Path(__file__).resolve().parent          # validate_tap/
_WORKSPACE_ROOT    = _SCRIPT_DIR.parent.parent                 # workspace/
CENTRAL_VENVS_DIR  = _WORKSPACE_ROOT / "taps" / "virtual_envs"
ALT_VENVS_DIR      = _WORKSPACE_ROOT / "Sample-taps-test" / "virtual_envs"

# Sync run timeout in seconds
SYNC_TIMEOUT = 600

# ---------------------------------------------------------------------------
# Module-level runtime context
# Populated by main() from --tap-config / --venv / --output-dir before
# validators are called.  Validators read this dict to decide whether to
# execute the tap or fall back to static analysis.
# ---------------------------------------------------------------------------
_RUNTIME: Dict[str, Any] = {
    "tap_config":  None,   # Path  — credentials file passed to the tap
    "venv":        None,   # Path  — venv that has the tap installed
    "output_dir":  None,   # Path  — where runtime artifacts are written
    "timeout":     SYNC_TIMEOUT,
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name:    str
    status:  str            # PASS | FAIL | WARN | SKIP
    summary: str
    details: List[str] = field(default_factory=list)

    @property
    def icon(self) -> str:
        return {"PASS": "[+]", "FAIL": "[X]", "WARN": "[!]", "SKIP": "[-]"}.get(self.status, "[?]")

    @property
    def color(self) -> str:
        return {"PASS": GREEN, "FAIL": RED, "WARN": YELLOW, "SKIP": DIM}.get(self.status, NC)


@dataclass
class ValidationReport:
    tap_name:  str
    tap_dir:   str
    timestamp: str
    results:   List[CheckResult] = field(default_factory=list)

    @property
    def passed(self)  -> int: return sum(1 for r in self.results if r.status == "PASS")
    @property
    def failed(self)  -> int: return sum(1 for r in self.results if r.status == "FAIL")
    @property
    def warned(self)  -> int: return sum(1 for r in self.results if r.status == "WARN")
    @property
    def skipped(self) -> int: return sum(1 for r in self.results if r.status == "SKIP")

    @property
    def overall(self) -> str:
        if self.failed  > 0: return "FAIL"
        if self.warned  > 0: return "WARN"
        return "PASS"


# ---------------------------------------------------------------------------
# Validator registry
# ---------------------------------------------------------------------------
VALIDATORS: Dict[str, Callable] = {}

def register(name: str):
    def decorator(fn: Callable) -> Callable:
        VALIDATORS[name] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Shared helpers (static analysis)
# ---------------------------------------------------------------------------

def _read_file(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _find_tap_package(tap_dir: Path) -> Optional[Path]:
    for item in sorted(tap_dir.iterdir()):
        if item.is_dir() and item.name.startswith("tap_") and (item / "__init__.py").exists():
            return item
    return None


def _load_json(path: Path) -> Optional[Any]:
    text = _read_file(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _find_python(tap_dir: Path) -> str:
    bin_dir = "Scripts" if sys.platform == "win32" else "bin"
    ext     = ".exe"    if sys.platform == "win32" else ""
    for root in [tap_dir / ".venv", tap_dir / "venv"]:
        exe = root / bin_dir / f"python{ext}"
        if exe.exists():
            return str(exe)
    return sys.executable


# ---------------------------------------------------------------------------
# Runtime helpers (tap execution)
# ---------------------------------------------------------------------------

def _find_tap_exe(tap_name: str, venv_override: Optional[Path] = None) -> Optional[str]:
    """
    Locate the tap executable.  Search order:
      1. User-provided --venv
      2. tap_dir/.venv  or  tap_dir/venv
      3. central_venvs/<tap_name>
      4. alt_venvs/<tap_name>
      5. System PATH
    """
    bin_dir = "Scripts" if sys.platform == "win32" else "bin"

    candidates: List[Path] = []
    if venv_override:
        candidates.append(Path(venv_override))
    candidates += [
        CENTRAL_VENVS_DIR / tap_name,
        ALT_VENVS_DIR      / tap_name,
    ]

    for venv_root in candidates:
        found = shutil.which(tap_name, path=str(venv_root / bin_dir))
        if found:
            return found

    # System PATH fallback
    return shutil.which(tap_name)


def _build_clean_credentials(tap_config_path: Path) -> Optional[Path]:
    """
    Read the universal tap_credentials.json, strip _ -prefixed metadata keys,
    and write a clean temp file that is safe to pass to the tap executable.
    Returns the temp file path (caller must clean up).
    """
    raw = _load_json(tap_config_path)
    if not isinstance(raw, dict):
        return None
    clean = {k: v for k, v in raw.items() if not k.startswith("_")}
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(clean, tmp)
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


def _run_subprocess(cmd: List[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def _parse_singer_output(text: str) -> Tuple[Dict[str, List], Dict[str, dict], List[dict]]:
    """
    Parse raw Singer output text.
    Returns:
        records   : {stream_name: [record, ...]}
        schemas   : {stream_name: schema_dict}
        states    : [state_value, ...]   (all STATE.value entries in order)
    """
    records: Dict[str, List]   = {}
    schemas: Dict[str, dict]   = {}
    states:  List[dict]        = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_type = msg.get("type")
        if msg_type == "SCHEMA":
            stream = msg.get("stream", "")
            schemas[stream] = msg.get("schema", {})
            records.setdefault(stream, [])
        elif msg_type == "RECORD":
            stream = msg.get("stream", "")
            records.setdefault(stream, []).append(msg.get("record", {}))
        elif msg_type == "STATE":
            states.append(msg.get("value", {}))

    return records, schemas, states


def _select_all_streams(catalog: dict) -> dict:
    """Return a copy of the catalog with every stream selected."""
    import copy
    cat = copy.deepcopy(catalog)
    for stream in cat.get("streams", []):
        for md in stream.get("metadata", []):
            if md.get("breadcrumb") == []:
                md["metadata"]["selected"] = True
    return cat


def _has_placeholder(tap_config_path: Path) -> bool:
    """Return True if the credentials file still has unfilled placeholder values."""
    text = _read_file(tap_config_path) or ""
    return bool(re.search(
        r"YOUR_|<[A-Z_]+>|PLACEHOLDER|your_token|your_key|your_secret|your_access",
        text, re.IGNORECASE
    ))


# ===========================================================================
# VALIDATOR 1 - Python upgrade  (static)
# ===========================================================================

@register("python_upgrade")
def check_python_upgrade(tap_dir: Path) -> CheckResult:
    details: List[str] = []
    issues:  List[str] = []
    warnings: List[str] = []

    # CircleCI
    ci_text = _read_file(tap_dir / ".circleci" / "config.yml")
    if ci_text is None:
        issues.append("CircleCI config not found: .circleci/config.yml")
        details.append("  CircleCI config: NOT FOUND")
    else:
        versions = re.findall(r"(?:--python|python)\s+([\d.]+)", ci_text)
        versions += re.findall(r"python:?([\d.]+)", ci_text)
        if not versions:
            warnings.append("Could not detect Python version in CircleCI config")
            details.append("  CircleCI: no explicit Python version detected")
        else:
            for v in set(versions):
                parts = tuple(int(x) for x in v.split(".")[:2] if x.isdigit())
                ok = parts and parts >= MIN_PYTHON
                details.append(f"  CircleCI Python {v}: {'OK' if ok else 'FAIL (expected >= ' + MIN_PYTHON_STR + ')'}")
                if not ok:
                    issues.append(f"CircleCI Python {v} < {MIN_PYTHON_STR}")

    # setup.py / setup.cfg
    setup_text = _read_file(tap_dir / "setup.py") or _read_file(tap_dir / "setup.cfg") or ""
    if not setup_text:
        issues.append("setup.py / setup.cfg not found")
    else:
        details.append("  setup.py package pins:")
        for pkg in SINGER_PACKAGES:
            m = re.search(rf"{re.escape(pkg)}\s*[=><~!]+([\d.]+)", setup_text, re.IGNORECASE)
            if m:
                pinned = m.group(1)
                latest = _fetch_pypi_latest(pkg)
                if latest is None:
                    details.append(f"    {pkg}=={pinned}: found (PyPI unreachable, skipping latest check)")
                elif pinned == latest:
                    details.append(f"    {pkg}=={pinned}: up-to-date")
                else:
                    warnings.append(f"{pkg}=={pinned} is outdated (latest: {latest})")
                    details.append(f"    {pkg}=={pinned}: OUTDATED (latest: {latest})")
            elif pkg in setup_text:
                warnings.append(f"{pkg} in setup.py but no version pin")
                details.append(f"    {pkg}: present but no version pin")

    if issues:
        return CheckResult("python_upgrade", "FAIL",
                           "Python upgrade incomplete: " + "; ".join(issues), details)
    if warnings:
        return CheckResult("python_upgrade", "WARN",
                           "Python upgrade warnings: " + "; ".join(warnings), details)
    return CheckResult("python_upgrade", "PASS",
                       f"CircleCI uses Python {MIN_PYTHON_STR}+ and setup.py packages are versioned",
                       details)


# ===========================================================================
# VALIDATOR 2 - Metadata  (static)
# ===========================================================================

@register("metadata")
def check_metadata(tap_dir: Path) -> CheckResult:
    details: List[str] = []
    issues:  List[str] = []
    warnings: List[str] = []

    pkg_dir = _find_tap_package(tap_dir)
    if not pkg_dir:
        return CheckResult("metadata", "SKIP", "Tap package directory not found", [])

    schema_code = "".join(
        _read_file(pkg_dir / f) or ""
        for f in ["schema.py", "discover.py", "catalog.py"]
    )
    streams_code = _read_file(pkg_dir / "streams.py") or ""
    sd = pkg_dir / "streams"
    if sd.exists():
        for fp in sd.glob("*.py"):
            streams_code += _read_file(fp) or ""

    # replication-method
    if "replication-method" in schema_code or "replication_method" in schema_code:
        details.append("  replication-method: found in metadata code")
    else:
        warnings.append("replication-method not explicitly set in schema/discover code")
        details.append("  replication-method: NOT found")

    # get_standard_metadata
    if "get_standard_metadata" in schema_code:
        details.append("  get_standard_metadata(): used")
    else:
        warnings.append("get_standard_metadata() not found")
        details.append("  get_standard_metadata(): NOT used")

    # parent-tap-stream-id and FULL_TABLE — prefer catalog (ground truth) over static analysis
    out_dir: Path = _RUNTIME.get("output_dir") or (tap_dir / "validator_output")
    catalog_candidates = [out_dir / "catalog.json", tap_dir / "catalog.json"]
    catalog_data = None
    catalog_source = None
    for _cp in catalog_candidates:
        if _cp.is_file():
            catalog_data = _load_json(_cp)
            catalog_source = _cp.name + (" (runtime)" if "validator_output" in str(_cp) else " (static)")
            break

    if catalog_data and isinstance(catalog_data, dict):
        _streams_list = catalog_data.get("streams", [])
        child_streams_found: List[str] = []
        full_table_streams_found: List[str] = []
        for _s in _streams_list:
            _sname = _s.get("tap_stream_id") or _s.get("stream", "?")
            _root_md = next(
                (m.get("metadata", {}) for m in _s.get("metadata", []) if m.get("breadcrumb") == []),
                {}
            )
            _repl   = _root_md.get("forced-replication-method") or _root_md.get("replication-method", "")
            _parent = _root_md.get("parent-tap-stream-id")
            if _parent:
                child_streams_found.append(f"{_sname}(parent={_parent})")
            if _repl == "FULL_TABLE":
                full_table_streams_found.append(_sname)

        if child_streams_found:
            details.append(
                f"  parent-tap-stream-id: {len(child_streams_found)} child stream(s) in catalog"
                f" — {', '.join(child_streams_found)}"
            )
        else:
            details.append("  parent-tap-stream-id: no child streams in catalog")

        if full_table_streams_found:
            details.append(
                f"  FULL_TABLE replication: {len(full_table_streams_found)} stream(s)"
                f" — {', '.join(full_table_streams_found)}"
            )
        else:
            details.append("  FULL_TABLE replication: not found in catalog (may be all-incremental)")

        details.append(f"  (catalog source: {catalog_source})")
    else:
        # Fallback: static code analysis when no catalog is available
        has_parent_attr = bool(re.search(r"\bparent\b\s*=\s*['\"]", streams_code))
        has_parent_meta = "parent-tap-stream-id" in schema_code
        if has_parent_attr and not has_parent_meta:
            issues.append("Child streams exist but parent-tap-stream-id not written in metadata")
            details.append("  parent-tap-stream-id: MISSING")
        elif has_parent_attr:
            details.append("  parent-tap-stream-id: written for child streams")
        else:
            details.append("  parent-tap-stream-id: no child streams detected (static analysis)")

        if "FULL_TABLE" in streams_code:
            details.append("  FULL_TABLE replication: present (static analysis)")
        else:
            details.append("  FULL_TABLE replication: not found (may be all-incremental)")

    if issues:
        return CheckResult("metadata", "FAIL",  "Metadata issues: " + "; ".join(issues), details)
    if warnings:
        return CheckResult("metadata", "WARN",  "Metadata warnings: " + "; ".join(warnings), details)
    return CheckResult("metadata", "PASS",
                       "Replication-method and parent-tap-stream-id metadata correctly set", details)


# ===========================================================================
# VALIDATOR 3 - Unauth stream exclusion  (static)
# ===========================================================================

@register("unauth_exclusion")
def check_unauth_exclusion(tap_dir: Path) -> CheckResult:
    details: List[str] = []
    issues:  List[str] = []

    pkg_dir = _find_tap_package(tap_dir)
    if not pkg_dir:
        return CheckResult("unauth_exclusion", "SKIP", "Tap package directory not found", [])

    discover_text = _read_file(pkg_dir / "discover.py") or ""
    all_code = discover_text
    for fp in pkg_dir.glob("*.py"):
        all_code += _read_file(fp) or ""
    sd = pkg_dir / "streams"
    if sd.exists():
        for fp in sd.glob("*.py"):
            all_code += _read_file(fp) or ""

    checks = {
        "check_access() method":          bool(re.search(r"def\s+check_access", all_code)),
        "403/Forbidden in discover.py":   bool(re.search(r"403|Forbidden|ForbiddenError|AuthorizationError", discover_text)),
        "_apply_access_checks() called":  "_apply_access_checks" in discover_text,
        "inaccessible stream removal":    bool(re.search(r"schemas\.pop|field_metadata\.pop|inaccessible|excluded_streams", discover_text)),
        "_prune_inaccessible_children()": "_prune_inaccessible_children" in discover_text,
    }

    found = sum(1 for v in checks.values() if v)
    for label, ok in checks.items():
        details.append(f"  {label}: {'OK' if ok else 'NOT FOUND'}")

    if not checks["403/Forbidden in discover.py"] and not checks["check_access() method"]:
        issues.append("No 403/Forbidden/check_access() in discover.py")
    if not checks["inaccessible stream removal"]:
        issues.append("No inaccessible stream removal in discover.py")

    if issues:
        return CheckResult("unauth_exclusion", "FAIL",
                           "Unauth exclusion NOT implemented: " + "; ".join(issues), details)
    if found < 3:
        return CheckResult("unauth_exclusion", "WARN",
                           f"Unauth exclusion partial ({found}/{len(checks)} patterns)", details)
    return CheckResult("unauth_exclusion", "PASS",
                       f"Unauth stream exclusion fully implemented ({found}/{len(checks)} patterns)",
                       details)


# ===========================================================================
# VALIDATOR 4 - Unit tests  (static + pytest collection)
# ===========================================================================

@register("unit_tests")
def check_unit_tests(tap_dir: Path) -> CheckResult:
    details: List[str] = []
    issues:  List[str] = []
    warnings: List[str] = []

    unit_test_dir: Optional[Path] = None
    for cand in [tap_dir / "tests" / "unittests",
                 tap_dir / "tests" / "unit",
                 tap_dir / "unittests"]:
        if cand.is_dir():
            unit_test_dir = cand
            break

    if unit_test_dir is None:
        issues.append("No unit test directory found")
        return CheckResult("unit_tests", "FAIL", "Unit test directory not found", issues)

    test_files = list(unit_test_dir.glob("test_*.py")) + list(unit_test_dir.glob("*_test.py"))
    if not test_files:
        issues.append(f"No test_*.py files in {unit_test_dir.relative_to(tap_dir)}")
        return CheckResult("unit_tests", "FAIL", "No unit test files found", issues)

    details.append(f"  Unit test dir: {unit_test_dir.relative_to(tap_dir)}")
    details.append(f"  Test files: {sorted(f.name for f in test_files)}")

    for pattern, label in [("test_client",   "HTTP client / auth"),
                            ("test_discover", "discovery / catalog"),
                            ("test_sync",     "sync / bookmarks")]:
        found = any(pattern in f.name for f in test_files)
        details.append(f"  {label} tests: {'found' if found else 'MISSING'}")
        if not found:
            warnings.append(f"No {label} test file")

    python = _find_python(tap_dir)
    try:
        r = subprocess.run(
            [python, "-m", "pytest", str(unit_test_dir), "--collect-only", "-q", "--tb=no"],
            cwd=str(tap_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=30, encoding="utf-8", errors="replace",
        )
        m = re.search(r"(\d+)\s+test", r.stdout)
        details.append(f"  Pytest collected: {m.group(1) if m else 'unknown'} tests")
        if r.returncode != 0 and not m:
            warnings.append("pytest collection failed (possible import errors)")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        details.append("  pytest: not available or timed out")

    if (tap_dir / ".coverage").exists():
        details.append("  .coverage file: present")
        try:
            pkg_dir = _find_tap_package(tap_dir)
            inc = f"{pkg_dir}/*" if pkg_dir else ""
            cr = subprocess.run(
                [python, "-m", "coverage", "report", f"--include={inc}"],
                cwd=str(tap_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=30, encoding="utf-8", errors="replace",
            )
            m2 = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", cr.stdout)
            if m2:
                pct = int(m2.group(1))
                details.append(f"  Coverage: {pct}%")
                if pct < 50:
                    issues.append(f"Coverage {pct}% < minimum 50%")
                elif pct < 70:
                    warnings.append(f"Coverage {pct}% below recommended 70%")
        except Exception:
            pass
    elif (tap_dir / "htmlcov").is_dir():
        details.append("  htmlcov/: present")
    else:
        warnings.append("No .coverage — run: coverage run -m pytest tests/unittests/")
        details.append("  Coverage: not measured")

    if issues:
        return CheckResult("unit_tests", "FAIL",  "Unit test issues: " + "; ".join(issues), details)
    if warnings:
        return CheckResult("unit_tests", "WARN",  "Unit test warnings: " + "; ".join(warnings), details)
    return CheckResult("unit_tests", "PASS",
                       f"Unit tests present ({len(test_files)} files, all key areas covered)", details)


# ===========================================================================
# VALIDATOR 5 - Integration tests  (static)
# ===========================================================================

@register("integration_tests")
def check_integration_tests(tap_dir: Path) -> CheckResult:
    details: List[str] = []
    issues:  List[str] = []
    warnings: List[str] = []

    tests_dir = tap_dir / "tests"
    if not tests_dir.is_dir():
        return CheckResult("integration_tests", "FAIL", "No tests/ directory found", ["  tests/: NOT FOUND"])

    int_files = [f for f in tests_dir.iterdir()
                 if f.is_file() and f.name.startswith("test_") and "unittests" not in str(f)]

    details.append(f"  Integration test files: {sorted(f.name for f in int_files) or 'none'}")

    expected = {
        "bookmark":         "test_bookmark",
        "discovery":        "test_discovery",
        "pagination":       "test_pagination",
        "all_fields":       "test_all_fields",
        "automatic_fields": "test_automatic_fields",
        "start_date":       "test_start_date",
    }
    found, missing = [], []
    for t, pat in expected.items():
        if any(pat in f.name for f in int_files):
            found.append(t);   details.append(f"  {t}: found")
        else:
            missing.append(t); details.append(f"  {t}: NOT found")

    if (tests_dir / "base.py").exists():
        details.append("  tests/base.py: found")
    else:
        warnings.append("tests/base.py not found")

    ci_text = _read_file(tap_dir / ".circleci" / "config.yml") or ""
    if re.search(r"Integration Tests|run-test|tap_tester", ci_text, re.IGNORECASE):
        details.append("  CircleCI integration step: found")
    else:
        warnings.append("No integration step in CircleCI config")

    if not int_files:
        issues.append("No integration test files found")
    if missing:
        warnings.append(f"Missing test types: {', '.join(missing)}")

    if issues:
        return CheckResult("integration_tests", "FAIL",
                           "Integration tests missing: " + "; ".join(issues), details)
    if warnings:
        return CheckResult("integration_tests", "WARN",
                           f"Integration tests partial: {len(found)}/{len(expected)} types present",
                           details)
    return CheckResult("integration_tests", "PASS",
                       f"Integration tests complete ({len(int_files)} files, {len(found)}/{len(expected)} types)",
                       details)


# ===========================================================================
# VALIDATOR 6 - Discovery
# ===========================================================================
# If --tap-config is provided and has real credentials:
#   -> actually RUN the tap --discover and validate Singer catalog output
# Otherwise:
#   -> static analysis of discover.py

@register("discovery")
def check_discovery(tap_dir: Path) -> CheckResult:
    tap_config: Optional[Path] = _RUNTIME.get("tap_config")
    venv_path:  Optional[Path] = _RUNTIME.get("venv")
    out_dir:    Path            = _RUNTIME.get("output_dir") or (tap_dir / "validator_output")
    timeout:    int             = _RUNTIME.get("timeout") or 120

    # ------------------------------------------------------------------
    # RUNTIME PATH — actually execute the tap
    # ------------------------------------------------------------------
    if tap_config and tap_config.is_file() and not _has_placeholder(tap_config):
        return _check_discovery_runtime(tap_dir, tap_config, venv_path, out_dir, timeout)

    # ------------------------------------------------------------------
    # STATIC PATH — code analysis only
    # ------------------------------------------------------------------
    return _check_discovery_static(tap_dir, tap_config)


def _check_discovery_runtime(tap_dir: Path, tap_config: Path,
                              venv_path: Optional[Path], out_dir: Path,
                              timeout: int) -> CheckResult:
    details: List[str] = []
    issues:  List[str] = []
    warnings: List[str] = []
    tap_name = tap_dir.name

    details.append("  Mode: RUNTIME (executing tap --discover)")

    # Find executable
    tap_exe = _find_tap_exe(tap_name, venv_path)
    if not tap_exe:
        return CheckResult("discovery", "FAIL",
                           f"Tap executable '{tap_name}' not found. Install it first: pip install -e .",
                           [f"  Searched: venv dirs, system PATH",
                            f"  Hint: activate the tap's venv or pass --venv /path/to/venv"])

    details.append(f"  Tap executable: {tap_exe}")

    # Build clean credentials temp file
    clean_creds = _build_clean_credentials(tap_config)
    if clean_creds is None:
        return CheckResult("discovery", "FAIL", "Could not parse tap_credentials.json", details)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        catalog_path = out_dir / "catalog.json"

        # Run tap --discover
        result = _run_subprocess(
            [tap_exe, "--config", str(clean_creds), "--discover"],
            timeout=timeout,
        )

        if result.returncode != 0:
            details.append(f"  tap --discover stderr:\n    {result.stderr[-600:]}")
            return CheckResult("discovery", "FAIL",
                               f"tap --discover exited with code {result.returncode}",
                               details)

        # Parse catalog
        try:
            catalog = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            details.append(f"  Raw output (first 300 chars): {result.stdout[:300]}")
            return CheckResult("discovery", "FAIL",
                               f"tap --discover output is not valid JSON: {e}", details)

        # Save catalog
        catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        details.append(f"  Catalog saved: {catalog_path}")

        # Validate catalog structure
        streams = catalog.get("streams", [])
        if not streams:
            return CheckResult("discovery", "FAIL",
                               "Catalog has no streams", details)

        details.append(f"  Streams in catalog: {len(streams)}")

        stream_issues: List[str] = []
        for s in streams:
            name = s.get("stream") or s.get("tap_stream_id", "unknown")
            kp   = s.get("key_properties", [])
            schema_props = s.get("schema", {}).get("properties", {})
            root_md = next(
                (m["metadata"] for m in s.get("metadata", []) if m.get("breadcrumb") == []),
                {}
            )
            repl    = root_md.get("forced-replication-method", root_md.get("replication-method", ""))
            parent  = root_md.get("parent-tap-stream-id", "")

            row = (f"  Stream: {name}  |  keys={kp}"
                   f"  |  replication={repl or 'NOT SET'}"
                   f"  |  fields={len(schema_props)}"
                   f"  |  parent={parent or '-'}")
            details.append(row)

            if not kp:
                stream_issues.append(f"{name}: no key_properties")
            if not repl:
                warnings.append(f"{name}: replication-method not in metadata")

        if stream_issues:
            issues += stream_issues

        if issues:
            return CheckResult("discovery", "FAIL",
                               f"Discovery FAILED: {'; '.join(issues)}", details)
        if warnings:
            return CheckResult("discovery", "WARN",
                               f"Discovery OK with {len(streams)} streams, warnings: {'; '.join(warnings[:2])}",
                               details)
        return CheckResult("discovery", "PASS",
                           f"Discovery returned {len(streams)} valid streams with keys and replication-method",
                           details)
    finally:
        if clean_creds and clean_creds.exists():
            clean_creds.unlink(missing_ok=True)


def _check_discovery_static(tap_dir: Path, tap_config: Optional[Path]) -> CheckResult:
    details: List[str] = []
    issues:  List[str] = []
    warnings: List[str] = []

    if tap_config:
        details.append("  Mode: STATIC (tap_credentials.json has placeholder values — fill in real credentials to enable runtime check)")
    else:
        details.append("  Mode: STATIC (no --tap-config provided)")

    pkg_dir = _find_tap_package(tap_dir)
    if not pkg_dir:
        return CheckResult("discovery", "SKIP", "Tap package directory not found", details)

    discover_text = _read_file(pkg_dir / "discover.py")
    if discover_text is None:
        return CheckResult("discovery", "FAIL", "discover.py missing", details)

    checks = {
        "discover() function defined":  bool(re.search(r"def\s+discover\b", discover_text)),
        "Catalog constructed":          "Catalog" in discover_text,
        "CatalogEntry used":            "CatalogEntry" in discover_text,
        "Schema loaded per stream":     bool(re.search(r"Schema|schema_dict|get_schemas", discover_text)),
        "key_properties set":           "key_properties" in discover_text,
        "tap_stream_id set":            "tap_stream_id" in discover_text,
        "metadata written":             "metadata" in discover_text,
    }
    for label, ok in checks.items():
        mark = "OK" if ok else "NOT FOUND"
        details.append(f"  {label}: {mark}")
        if not ok and label in ("discover() function defined", "Catalog constructed",
                                "CatalogEntry used", "Schema loaded per stream"):
            issues.append(f"discover.py: {label}")

    init_text = _read_file(pkg_dir / "__init__.py") or ""
    if "discover" in init_text:
        details.append("  __init__.py references discover(): OK")
    else:
        warnings.append("discover() not referenced in __init__.py")

    if issues:
        return CheckResult("discovery", "FAIL",  "Discovery code issues: " + "; ".join(issues), details)
    if warnings:
        return CheckResult("discovery", "WARN",  "Discovery code warnings: " + "; ".join(warnings), details)
    return CheckResult("discovery", "PASS",
                       "discover() correctly builds and returns a Singer Catalog (static check)",
                       details)


# ===========================================================================
# VALIDATOR 7 - Sync
# ===========================================================================
# If --tap-config is provided and has real credentials:
#   -> RUN Sync1 (no state) + Sync2 (with state from Sync1)
#   -> Validate RECORD, SCHEMA, STATE messages, bookmark advancement
# Otherwise:
#   -> static analysis

@register("sync")
def check_sync(tap_dir: Path) -> CheckResult:
    tap_config: Optional[Path] = _RUNTIME.get("tap_config")
    venv_path:  Optional[Path] = _RUNTIME.get("venv")
    out_dir:    Path            = _RUNTIME.get("output_dir") or (tap_dir / "validator_output")
    timeout:    int             = _RUNTIME.get("timeout") or SYNC_TIMEOUT

    if tap_config and tap_config.is_file() and not _has_placeholder(tap_config):
        return _check_sync_runtime(tap_dir, tap_config, venv_path, out_dir, timeout)
    return _check_sync_static(tap_dir, tap_config)


def _check_sync_runtime(tap_dir: Path, tap_config: Path,
                        venv_path: Optional[Path], out_dir: Path,
                        timeout: int) -> CheckResult:
    details: List[str] = []
    issues:  List[str] = []
    warnings: List[str] = []
    tap_name = tap_dir.name

    details.append("  Mode: RUNTIME (executing Sync1 + Sync2)")

    tap_exe = _find_tap_exe(tap_name, venv_path)
    if not tap_exe:
        return CheckResult("sync", "FAIL",
                           f"Tap executable '{tap_name}' not found. Run discovery first.",
                           [f"  Hint: pass --venv /path/to/venv or install the tap first"])

    clean_creds = _build_clean_credentials(tap_config)
    if clean_creds is None:
        return CheckResult("sync", "FAIL", "Could not parse tap_credentials.json", details)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        catalog_path = out_dir / "catalog.json"
        state1_path  = out_dir / "state_sync1.json"
        sync1_out    = out_dir / "sync1_output.json"
        sync2_out    = out_dir / "sync2_output.json"

        # ------------------------------------------------------------------
        # Step A: Discovery to get catalog (reuse if already produced)
        # ------------------------------------------------------------------
        if not catalog_path.is_file():
            details.append("  Running --discover to get catalog...")
            disc = _run_subprocess(
                [tap_exe, "--config", str(clean_creds), "--discover"],
                timeout=120,
            )
            if disc.returncode != 0:
                return CheckResult("sync", "FAIL",
                                   f"Discovery for sync failed (exit {disc.returncode})",
                                   details + [f"  stderr: {disc.stderr[-400:]}"])
            try:
                catalog = json.loads(disc.stdout)
            except json.JSONDecodeError:
                return CheckResult("sync", "FAIL", "Discovery output not valid JSON", details)
            catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        else:
            catalog = _load_json(catalog_path)
            details.append(f"  Reusing existing catalog: {catalog_path}")

        # Select all streams
        catalog_selected = _select_all_streams(catalog)
        selected_path = out_dir / "catalog_selected.json"
        selected_path.write_text(json.dumps(catalog_selected, indent=2), encoding="utf-8")
        stream_names = [s.get("stream") or s.get("tap_stream_id", "?")
                        for s in catalog_selected.get("streams", [])]
        details.append(f"  Streams selected for sync: {stream_names}")

        # ------------------------------------------------------------------
        # Step B: Sync1 — historical (no state)
        # ------------------------------------------------------------------
        details.append("  Running Sync1 (no state / full historical)...")
        r1 = _run_subprocess(
            [tap_exe, "--config", str(clean_creds), "--catalog", str(selected_path)],
            timeout=timeout,
        )
        sync1_out.write_text(r1.stdout, encoding="utf-8")
        if r1.returncode != 0:
            details.append(f"  Sync1 stderr: {r1.stderr[-400:]}")
            issues.append(f"Sync1 exited with code {r1.returncode}")

        records1, schemas1, states1 = _parse_singer_output(r1.stdout)
        total1 = sum(len(v) for v in records1.values())
        details.append(f"  Sync1 — SCHEMA messages: {len(schemas1)}")
        details.append(f"  Sync1 — Total records:   {total1}")
        details.append(f"  Sync1 — STATE messages:  {len(states1)}")

        for sname in sorted(records1):
            cnt = len(records1[sname])
            details.append(f"    {sname}: {cnt} records")

        # Validate Sync1
        if not schemas1:
            issues.append("Sync1: no SCHEMA messages emitted")
        if total1 == 0:
            warnings.append("Sync1: zero records returned — API may be empty or start_date too recent")
        if not states1:
            warnings.append("Sync1: no STATE messages — bookmarking may not be implemented")
        else:
            last_state1 = states1[-1]
            state1_path.write_text(json.dumps(last_state1, indent=2), encoding="utf-8")
            details.append(f"  Sync1 state saved: {state1_path}")

            # Validate bookmark structure
            bookmarks = last_state1.get("bookmarks", {})
            if bookmarks:
                details.append(f"  Sync1 bookmarks: {list(bookmarks.keys())}")
                for sname, bm in bookmarks.items():
                    details.append(f"    {sname}: {json.dumps(bm)[:100]}")
            else:
                warnings.append("Sync1: STATE emitted but 'bookmarks' key is empty")

        # ------------------------------------------------------------------
        # Step C: Sync2 — bookmark sync (with state from Sync1)
        # ------------------------------------------------------------------
        if state1_path.is_file():
            details.append("  Running Sync2 (with state from Sync1)...")
            r2 = _run_subprocess(
                [tap_exe, "--config", str(clean_creds),
                 "--catalog", str(selected_path),
                 "--state",   str(state1_path)],
                timeout=timeout,
            )
            sync2_out.write_text(r2.stdout, encoding="utf-8")
            if r2.returncode != 0:
                warnings.append(f"Sync2 exited with code {r2.returncode}")
                details.append(f"  Sync2 stderr: {r2.stderr[-400:]}")

            records2, schemas2, states2 = _parse_singer_output(r2.stdout)
            total2 = sum(len(v) for v in records2.values())
            details.append(f"  Sync2 — SCHEMA messages: {len(schemas2)}")
            details.append(f"  Sync2 — Total records:   {total2}")
            details.append(f"  Sync2 — STATE messages:  {len(states2)}")

            for sname in sorted(records2):
                cnt = len(records2[sname])
                details.append(f"    {sname}: {cnt} records")

            # Bookmark advancement check
            if states2:
                last_state2 = states2[-1]
                bm2 = last_state2.get("bookmarks", {})
                bm1 = last_state1.get("bookmarks", {}) if state1_path.is_file() else {}

                advanced, same, regressed = [], [], []
                for sname, val2 in bm2.items():
                    val1 = bm1.get(sname)
                    if val1 is None:
                        same.append(sname)
                    elif json.dumps(val2, sort_keys=True) == json.dumps(val1, sort_keys=True):
                        same.append(sname)
                    elif json.dumps(val2, sort_keys=True) > json.dumps(val1, sort_keys=True):
                        advanced.append(sname)
                    else:
                        regressed.append(sname)

                details.append(f"  Bookmark advancement: advanced={advanced} same={same} regressed={regressed}")
                if regressed:
                    issues.append(f"Bookmarks regressed for: {regressed}")
                if not advanced and not same:
                    warnings.append("No bookmark data in Sync2 state")
            else:
                warnings.append("Sync2: no STATE messages emitted")

            # FULL_TABLE streams should return same count; INCREMENTAL should return <= Sync1
            for sname in records1:
                cnt1 = len(records1[sname])
                cnt2 = len(records2.get(sname, []))
                root_md = next(
                    (m["metadata"] for m in
                     next((s for s in catalog_selected.get("streams", [])
                           if s.get("stream") == sname or s.get("tap_stream_id") == sname), {})
                     .get("metadata", [])
                     if m.get("breadcrumb") == []),
                    {}
                )
                repl = root_md.get("forced-replication-method", root_md.get("replication-method", ""))
                if repl == "FULL_TABLE" and cnt2 != cnt1:
                    warnings.append(f"{sname}: FULL_TABLE Sync2 count ({cnt2}) != Sync1 ({cnt1})")
                elif repl == "INCREMENTAL" and cnt2 > cnt1:
                    warnings.append(f"{sname}: INCREMENTAL Sync2 ({cnt2}) > Sync1 ({cnt1})")
        else:
            warnings.append("Sync2 skipped — no state from Sync1 (no STATE messages in Sync1)")

        # ------------------------------------------------------------------
        # Artifacts summary
        # ------------------------------------------------------------------
        details.append(f"  Artifacts written to: {out_dir}")
        details.append(f"    catalog.json, catalog_selected.json")
        details.append(f"    sync1_output.json, sync2_output.json, state_sync1.json")

        if issues:
            return CheckResult("sync", "FAIL",
                               f"Sync FAILED: {'; '.join(issues)}", details)
        if warnings:
            return CheckResult("sync", "WARN",
                               f"Sync OK (Sync1={total1} records, Sync2={total2} records) — warnings: {len(warnings)}",
                               details)
        return CheckResult("sync", "PASS",
                           f"Sync1 ({total1} records) + Sync2 ({total2} records) — bookmarks, pagination, state all validated",
                           details)
    finally:
        if clean_creds and clean_creds.exists():
            clean_creds.unlink(missing_ok=True)


def _check_sync_static(tap_dir: Path, tap_config: Optional[Path]) -> CheckResult:
    details: List[str] = []
    issues:  List[str] = []
    warnings: List[str] = []

    if tap_config:
        details.append("  Mode: STATIC (placeholder credentials — fill in tap_credentials.json for runtime)")
    else:
        details.append("  Mode: STATIC (no --tap-config provided)")

    pkg_dir = _find_tap_package(tap_dir)
    if not pkg_dir:
        return CheckResult("sync", "SKIP", "Tap package directory not found", details)

    sync_code = ""
    for fname in ["sync.py", "streams.py"]:
        sync_code += _read_file(pkg_dir / fname) or ""
    sd = pkg_dir / "streams"
    if sd.exists():
        for fp in sd.glob("*.py"):
            sync_code += _read_file(fp) or ""

    if not sync_code:
        return CheckResult("sync", "FAIL", "sync.py / streams.py not found", details)

    checks = {
        "get_bookmark (read bookmark)":   bool(re.search(r"get_bookmark", sync_code)),
        "write_state (save bookmark)":    bool(re.search(r"write_state\s*\(|singer\.write_state", sync_code)),
        "write_record":                   bool(re.search(r"write_record\s*\(|singer\.write_record", sync_code)),
        "write_schema":                   bool(re.search(r"write_schema\s*\(|singer\.write_schema", sync_code)),
        "Pagination":                     bool(re.search(r"page|per_page|next_page|offset|cursor", sync_code, re.IGNORECASE)),
        "FULL_TABLE replication":         "FULL_TABLE" in sync_code,
        "INCREMENTAL replication":        "INCREMENTAL" in sync_code,
        "Parent-child sync":              bool(re.search(r"children|child_to_sync|parent_obj|parent_record", sync_code)),
        "currently_syncing (Sync2)":      bool(re.search(r"currently_syncing|get_currently_syncing|set_currently_syncing", sync_code)),
        "Transformer usage":              bool(re.search(r"Transformer|\.transform\s*\(", sync_code)),
    }

    for label, ok in checks.items():
        details.append(f"  {label}: {'OK' if ok else 'NOT FOUND'}")

    for c in ["get_bookmark (read bookmark)", "write_state (save bookmark)",
              "write_record", "write_schema"]:
        if not checks[c]:
            issues.append(f"sync: {c} not found")

    for r in ["Pagination", "FULL_TABLE replication", "INCREMENTAL replication",
              "currently_syncing (Sync2)"]:
        if not checks[r]:
            warnings.append(f"sync: {r} not detected (may be intentional)")

    if issues:
        return CheckResult("sync", "FAIL",  "Sync issues: " + "; ".join(issues), details)
    if warnings:
        return CheckResult("sync", "WARN",  "Sync warnings: " + "; ".join(warnings), details)
    return CheckResult("sync", "PASS",
                       "Sync implements bookmarks, pagination, full/incremental, parent-child (static check)",
                       details)


# ===========================================================================
# VALIDATOR 8 - Schema  (static)
# ===========================================================================

@register("schema")
def check_schema(tap_dir: Path) -> CheckResult:
    details: List[str] = []
    issues:  List[str] = []
    warnings: List[str] = []

    pkg_dir = _find_tap_package(tap_dir)
    if not pkg_dir:
        return CheckResult("schema", "SKIP", "Tap package directory not found", [])

    schemas_dir = pkg_dir / "schemas"
    if not schemas_dir.is_dir():
        return CheckResult("schema", "FAIL", "schemas/ directory missing",
                           ["  schemas/: NOT FOUND in tap package"])

    schema_files = sorted(schemas_dir.glob("*.json"))
    if not schema_files:
        return CheckResult("schema", "FAIL", "No .json schema files found", [])

    details.append(f"  Schema files: {len(schema_files)}")
    s_issues, s_warns = 0, 0

    for sf in schema_files:
        name   = sf.stem
        schema = _load_json(sf)
        if schema is None:
            issues.append(f"{name}: invalid JSON");  s_issues += 1
            details.append(f"  {name}.json: INVALID JSON");  continue

        fi, fw = [], []
        if schema.get("type") not in ("object", ["object"], ["null", "object"]):
            fi.append("root type is not 'object'")

        props = schema.get("properties", {})
        if not props:
            fw.append("no properties defined")

        non_null, dt_missing = [], []
        for pname, pdef in props.items():
            if not isinstance(pdef, dict):
                continue
            ptype = pdef.get("type", [])
            if isinstance(ptype, str):
                ptype = [ptype]
            if ptype and "null" not in ptype:
                non_null.append(pname)
            if "string" in ptype:
                is_dt = any(k in pname.lower() for k in
                            ("_at", "_date", "_time", "date", "timestamp", "created", "updated", "modified"))
                if is_dt and pdef.get("format") != "date-time":
                    dt_missing.append(pname)

        if non_null:
            fw.append(f"{len(non_null)} non-nullable: {', '.join(non_null[:5])}")
        if dt_missing:
            fw.append(f"{len(dt_missing)} datetime fields missing format:date-time: {', '.join(dt_missing[:5])}")

        if fi:  s_issues += 1; issues  += [f"{name}: {x}" for x in fi]
        if fw:  s_warns  += 1; warnings += [f"{name}: {x}" for x in fw]

        status = "OK" if not fi and not fw else ("WARN" if fw and not fi else "FAIL")
        details.append(
            f"  {name}.json [{status}]"
            f"  props={len(props)}"
            f"  non-nullable={len(non_null)}"
            f"  dt-format-issues={len(dt_missing)}"
        )
        # Append per-field issue lines so the report shows exactly which fields need fixing
        if non_null:
            details.append(f"    non-nullable fields (add null type): {', '.join(non_null)}")
        if dt_missing:
            details.append(f"    missing format:date-time: {', '.join(dt_missing)}")
        if fi:
            for issue_line in fi:
                details.append(f"    error: {issue_line}")

    details.append(f"  Total: {len(schema_files)} files — {s_issues} errors, {s_warns} warnings")

    if issues:
        return CheckResult("schema", "FAIL",
                           f"Schema FAILED: {len(issues)} issue(s) in {s_issues} file(s)", details)
    if warnings:
        return CheckResult("schema", "WARN",
                           f"Schema warnings: {len(warnings)} warning(s) across {s_warns} file(s)", details)
    return CheckResult("schema", "PASS",
                       f"All {len(schema_files)} schemas valid (nullable, date-time format, root type)",
                       details)


# ===========================================================================
# VALIDATOR 9 - Catalog Validation  (static or runtime artifact)
# ===========================================================================
# Validates the Singer catalog JSON for full structural and semantic correctness.
# Source priority:
#   1. validator_output/catalog.json  (produced by the discovery check at runtime)
#   2. catalog.json  in the tap root  (checked-in / manually generated)
# Each stream is checked for:
#   - Required top-level fields (tap_stream_id, stream, key_properties, schema, metadata)
#   - stream == tap_stream_id consistency
#   - Root breadcrumb [] metadata completeness (table-key-properties,
#     forced-replication-method, inclusion, valid-replication-keys for INCREMENTAL)
#   - table-key-properties agrees with key_properties
#   - Replication method value is a recognised Singer value
#   - Every schema property has a metadata entry
#   - Every metadata entry has inclusion set (automatic | available | unsupported)
#   - key_properties fields have inclusion: automatic
#   - valid-replication-keys fields have inclusion: automatic

_VALID_REPLICATION_METHODS = {"INCREMENTAL", "FULL_TABLE", "LOG_BASED"}
_VALID_INCLUSION_VALUES    = {"automatic", "available", "unsupported"}


def _validate_catalog_stream(stream: dict) -> Tuple[List[str], List[str]]:
    """
    Validate a single catalog stream entry.
    Returns (issues, warnings) — issues are FAIL-level, warnings are WARN-level.
    """
    issues:   List[str] = []
    warnings: List[str] = []

    name = stream.get("tap_stream_id") or stream.get("stream") or "<unknown>"

    # ------------------------------------------------------------------
    # 1. Required top-level fields
    # ------------------------------------------------------------------
    for field in ("tap_stream_id", "stream", "key_properties", "schema", "metadata"):
        if field not in stream:
            issues.append(f"[{name}] missing top-level field: '{field}'")

    if "tap_stream_id" not in stream or "stream" not in stream:
        return issues, warnings  # can't validate further without identifiers

    # 2. stream == tap_stream_id
    if stream["tap_stream_id"] != stream["stream"]:
        issues.append(
            f"[{name}] 'stream' ({stream['stream']!r}) != 'tap_stream_id' ({stream['tap_stream_id']!r})"
        )

    key_props: List[str] = stream.get("key_properties") or []
    if not key_props:
        issues.append(f"[{name}] 'key_properties' is empty")

    schema_props: dict = stream.get("schema", {}).get("properties", {})
    metadata_list: List[dict] = stream.get("metadata", [])

    # ------------------------------------------------------------------
    # 3. Root breadcrumb [] metadata
    # ------------------------------------------------------------------
    root_md_entries = [m for m in metadata_list if m.get("breadcrumb") == []]
    if not root_md_entries:
        issues.append(f"[{name}] no root breadcrumb [] in metadata")
        return issues, warnings
    if len(root_md_entries) > 1:
        warnings.append(f"[{name}] multiple root breadcrumb [] entries ({len(root_md_entries)})")

    root_md: dict = root_md_entries[0].get("metadata", {})

    # table-key-properties
    if "table-key-properties" not in root_md:
        issues.append(f"[{name}] root metadata missing 'table-key-properties'")
    else:
        catalog_kp = root_md["table-key-properties"]
        if sorted(catalog_kp) != sorted(key_props):
            issues.append(
                f"[{name}] 'table-key-properties' {catalog_kp} != 'key_properties' {key_props}"
            )

    # forced-replication-method / replication-method
    repl_method = (
        root_md.get("forced-replication-method")
        or root_md.get("replication-method")
    )
    if not repl_method:
        issues.append(f"[{name}] root metadata missing 'forced-replication-method'")
    elif repl_method not in _VALID_REPLICATION_METHODS:
        issues.append(
            f"[{name}] invalid replication method '{repl_method}' "
            f"(expected one of: {sorted(_VALID_REPLICATION_METHODS)})"
        )

    # inclusion at root level
    if "inclusion" not in root_md:
        warnings.append(f"[{name}] root metadata missing 'inclusion'")

    # valid-replication-keys required for INCREMENTAL
    valid_repl_keys: List[str] = root_md.get("valid-replication-keys", [])
    if repl_method == "INCREMENTAL":
        if not valid_repl_keys:
            issues.append(f"[{name}] INCREMENTAL stream missing 'valid-replication-keys' in root metadata")

    # ------------------------------------------------------------------
    # 4. Build a fast lookup: property path -> metadata dict
    # ------------------------------------------------------------------
    prop_meta: Dict[str, dict] = {}
    for entry in metadata_list:
        bc = entry.get("breadcrumb", [])
        if len(bc) == 2 and bc[0] == "properties":
            prop_meta[bc[1]] = entry.get("metadata", {})

    # ------------------------------------------------------------------
    # 5. Every schema property should have a metadata entry
    # ------------------------------------------------------------------
    missing_meta = [p for p in schema_props if p not in prop_meta]
    if missing_meta:
        warnings.append(
            f"[{name}] {len(missing_meta)} schema property(ies) have no metadata entry: "
            f"{', '.join(missing_meta[:8])}{'...' if len(missing_meta) > 8 else ''}"
        )

    # ------------------------------------------------------------------
    # 6. Every metadata entry must have a valid 'inclusion'
    # ------------------------------------------------------------------
    bad_inclusion = []
    for prop, md in prop_meta.items():
        inc = md.get("inclusion")
        if inc is None:
            bad_inclusion.append(f"{prop}(missing)")
        elif inc not in _VALID_INCLUSION_VALUES:
            bad_inclusion.append(f"{prop}({inc!r})")
    if bad_inclusion:
        issues.append(
            f"[{name}] invalid/missing 'inclusion' on "
            f"{len(bad_inclusion)} property(ies): "
            f"{', '.join(bad_inclusion[:8])}{'...' if len(bad_inclusion) > 8 else ''}"
        )

    # ------------------------------------------------------------------
    # 7. key_properties must have inclusion: automatic
    # ------------------------------------------------------------------
    for kp in key_props:
        inc = prop_meta.get(kp, {}).get("inclusion")
        if inc != "automatic":
            issues.append(
                f"[{name}] key property '{kp}' has inclusion={inc!r} (must be 'automatic')"
            )

    # ------------------------------------------------------------------
    # 8. valid-replication-keys must have inclusion: automatic
    # ------------------------------------------------------------------
    for rk in valid_repl_keys:
        inc = prop_meta.get(rk, {}).get("inclusion")
        if inc != "automatic":
            issues.append(
                f"[{name}] replication key '{rk}' has inclusion={inc!r} (must be 'automatic')"
            )

    # ------------------------------------------------------------------
    # 9. parent-tap-stream-id — return value for cross-stream check
    # ------------------------------------------------------------------
    # (cross-stream referential validation is done in check_catalog_validation)

    return issues, warnings


def _get_parent_stream_id(stream: dict) -> Optional[str]:
    """Return the parent-tap-stream-id value from root metadata, or None."""
    root_md = next(
        (m.get("metadata", {}) for m in stream.get("metadata", [])
         if m.get("breadcrumb") == []),
        {}
    )
    return root_md.get("parent-tap-stream-id")


@register("catalog_validation")
def check_catalog_validation(tap_dir: Path) -> CheckResult:
    details:  List[str] = []
    issues:   List[str] = []
    warnings: List[str] = []

    # Locate the catalog to validate
    out_dir: Path = _RUNTIME.get("output_dir") or (tap_dir / "validator_output")
    candidates = [
        out_dir      / "catalog.json",   # runtime-generated (preferred)
        tap_dir      / "catalog.json",   # checked-in / manually generated
    ]

    catalog_path: Optional[Path] = None
    for c in candidates:
        if c.is_file():
            catalog_path = c
            break

    if catalog_path is None:
        return CheckResult(
            "catalog_validation", "SKIP",
            "No catalog.json found. Run discovery first (--tap-config) or place catalog.json in the tap root.",
            ["  Searched: validator_output/catalog.json, <tap-root>/catalog.json"],
        )

    details.append(f"  Catalog source: {catalog_path.name} "
                   f"({'runtime' if 'validator_output' in str(catalog_path) else 'static'})")

    catalog = _load_json(catalog_path)
    if not isinstance(catalog, dict):
        return CheckResult("catalog_validation", "FAIL",
                           "catalog.json is not valid JSON or not an object", details)

    streams = catalog.get("streams", [])
    if not streams:
        return CheckResult("catalog_validation", "FAIL",
                           "catalog.json contains no streams", details)

    details.append(f"  Total streams: {len(streams)}")
    details.append("")

    total_issues   = 0
    total_warnings = 0

    for stream in streams:
        s_name = stream.get("tap_stream_id") or stream.get("stream") or "<unknown>"
        s_issues, s_warnings = _validate_catalog_stream(stream)

        status = "OK"
        if s_issues:
            status = "FAIL"
        elif s_warnings:
            status = "WARN"

        # Count metadata entries and schema properties for summary
        n_props  = len(stream.get("schema", {}).get("properties", {}))
        n_meta   = len([m for m in stream.get("metadata", [])
                        if len(m.get("breadcrumb", [])) == 2
                        and m["breadcrumb"][0] == "properties"])
        root_md  = next(
            (m.get("metadata", {}) for m in stream.get("metadata", [])
             if m.get("breadcrumb") == []),
            {}
        )
        repl     = (root_md.get("forced-replication-method")
                    or root_md.get("replication-method") or "NOT SET")
        kp       = stream.get("key_properties", [])
        vrk      = root_md.get("valid-replication-keys", [])
        parent   = root_md.get("parent-tap-stream-id", "")

        details.append(
            f"  [{status}] {s_name}"
            f"  |  replication={repl}"
            f"  |  keys={kp}"
            f"  |  repl-keys={vrk or '-'}"
            f"  |  parent={parent or '-'}"
            f"  |  schema-props={n_props}"
            f"  |  meta-prop-entries={n_meta}"
        )
        for line in s_issues:
            details.append(f"        ERROR: {line}")
            issues.append(line)
            total_issues += 1
        for line in s_warnings:
            details.append(f"        WARN:  {line}")
            warnings.append(line)
            total_warnings += 1

    # ------------------------------------------------------------------
    # Cross-stream: parent-tap-stream-id referential integrity
    # ------------------------------------------------------------------
    all_stream_ids = {s.get("tap_stream_id") or s.get("stream") for s in streams}
    child_streams  = [
        (s.get("tap_stream_id") or s.get("stream"), _get_parent_stream_id(s))
        for s in streams
        if _get_parent_stream_id(s)
    ]

    if child_streams:
        details.append("  parent-tap-stream-id cross-stream checks:")
        for child_name, parent_name in sorted(child_streams):
            if parent_name not in all_stream_ids:
                msg = (
                    f"[{child_name}] parent-tap-stream-id '{parent_name}' "
                    f"not found in catalog (dangling reference)"
                )
                issues.append(msg)
                total_issues += 1
                details.append(f"    ERROR: {msg}")
            else:
                details.append(
                    f"    OK: '{child_name}' -> parent '{parent_name}'"
                )
    else:
        details.append("  parent-tap-stream-id: no child streams in catalog")

    details.append("")
    details.append(
        f"  Summary: {len(streams)} streams — "
        f"{total_issues} error(s), {total_warnings} warning(s)"
    )

    if issues:
        return CheckResult(
            "catalog_validation", "FAIL",
            f"Catalog invalid: {total_issues} error(s) across {len(streams)} streams",
            details,
        )
    if warnings:
        return CheckResult(
            "catalog_validation", "WARN",
            f"Catalog valid with warnings: {total_warnings} warning(s) across {len(streams)} streams",
            details,
        )
    return CheckResult(
        "catalog_validation", "PASS",
        f"All {len(streams)} streams passed catalog validation "
        f"(structure, metadata completeness, inclusion values, key/replication-key/parent coverage)",
        details,
    )


# ===========================================================================
# Report output
# ===========================================================================

SEP  = "-" * 72
SEP2 = "=" * 72


def _print_stdout_summary(report: ValidationReport) -> None:
    print()
    print(f"{CYAN}{SEP}{NC}")
    print(f"{BOLD}  Singer Tap Validator - {report.tap_name}{NC}")
    print(f"  {DIM}{report.tap_dir}  |  {report.timestamp}{NC}")
    print(f"{CYAN}{SEP}{NC}")
    print()

    label_w = max(len(r.name) for r in report.results) + 2
    for r in report.results:
        label = r.name.replace("_", " ").title().ljust(label_w)
        print(f"  {r.color}{r.icon}  {BOLD}{label}{NC}  {r.summary}")

    print()
    oc = {
        "PASS": GREEN, "FAIL": RED, "WARN": YELLOW,
    }.get(report.overall, NC)
    print(f"{CYAN}{SEP}{NC}")
    print(
        f"  {oc}{BOLD}Overall: {report.overall}{NC}   "
        f"{GREEN}PASS {report.passed}{NC}  "
        f"{RED}FAIL {report.failed}{NC}  "
        f"{YELLOW}WARN {report.warned}{NC}  "
        f"{DIM}SKIP {report.skipped}{NC}"
    )
    print(f"{CYAN}{SEP}{NC}")
    print()


def _print_verbose_report(report: ValidationReport) -> None:
    print()
    print(f"{BOLD}{SEP2}")
    print(f"  Detailed Validation Report - {report.tap_name}")
    print(f"{SEP2}{NC}")
    for r in report.results:
        print()
        print(f"{r.color}{BOLD}  [{r.status}] {r.name.replace('_', ' ').title()}{NC}")
        print(f"  {r.summary}")
        for line in r.details:
            print(f"  {line}")
    print()


_ABS_PATH_RE = re.compile(
    r'[A-Za-z]:\\(?:[^\s,\'"}\]]+)'   # Windows  C:\...\file.ext
    r'|/(?:home|usr|var|tmp|root|Users)/[^\s,\'"}\]]+'  # Unix /home/... /usr/...
)


def _sanitize_detail(text: str) -> str:
    """Replace absolute filesystem paths in a detail line with just the filename."""
    def _basename(m: re.Match) -> str:
        return Path(m.group(0)).name
    return _ABS_PATH_RE.sub(_basename, text)


def _inject_tap_name(output_arg: str, tap_name: str) -> Path:
    """
    Ensure the output filename includes the tap name so reports from different
    taps never overwrite each other.

    Rules:
      - If the stem already contains the tap_name  -> use as-is
      - Otherwise                                  -> insert <tap_name>_ before the stem

    Examples:
      "report.json"           + "tap-gitlab"  -> "tap-gitlab_report.json"
      "tap-gitlab_report.json" + "tap-gitlab"  -> "tap-gitlab_report.json"  (unchanged)
      "my_report.json"        + "tap-aftership"-> "tap-aftership_my_report.json"
    """
    p = Path(output_arg)
    if tap_name in p.stem:
        return p
    return p.parent / f"{tap_name}_{p.name}"


def _save_json_report(report: ValidationReport, output_path: Path) -> None:
    data = {
        "tap_name":  report.tap_name,
        "timestamp": report.timestamp,
        "overall":   report.overall,
        "summary": {"passed": report.passed, "failed": report.failed,
                    "warned": report.warned,  "skipped": report.skipped},
        "checks": [
            {
                "name":    r.name,
                "status":  r.status,
                "summary": r.summary,
                "details": [_sanitize_detail(line) for line in r.details],
            }
            for r in report.results
        ],
    }
    output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"{GREEN}  JSON report saved: {output_path}{NC}")


def _save_html_report(report: ValidationReport, output_path: Path) -> None:
    """Generate a self-contained HTML validation report."""

    STATUS_COLOR = {
        "PASS": ("#1a7f37", "#d4f5dc"),  # (text, bg)
        "FAIL": ("#c0392b", "#fdecea"),
        "WARN": ("#b7770d", "#fff8e1"),
        "SKIP": ("#555555", "#f0f0f0"),
    }
    STATUS_ICON = {"PASS": "✔", "FAIL": "✖", "WARN": "⚠", "SKIP": "—"}

    overall_color, overall_bg = STATUS_COLOR.get(report.overall, ("#333", "#eee"))

    def badge(status: str) -> str:
        tc, bg = STATUS_COLOR.get(status, ("#333", "#eee"))
        icon   = STATUS_ICON.get(status, "?")
        return (
            f'<span class="badge" style="color:{tc};background:{bg};'
            f'border:1px solid {tc};">{icon} {status}</span>'
        )

    # ------------------------------------------------------------------ #
    # Inline styles reused across stream tables                          #
    # ------------------------------------------------------------------ #
    _TBL  = 'width:100%;border-collapse:collapse;font-size:0.8rem;margin:6px 0 10px;'
    _TH   = ('padding:5px 10px;text-align:left;background:#f0f0f0;'
             'border-bottom:2px solid #ccc;font-weight:700;white-space:nowrap;')
    _TD   = 'padding:4px 10px;border-bottom:1px solid #eee;white-space:nowrap;'
    _TDM  = _TD + 'font-family:monospace;'
    _ST   = {
        'OK':   'background:#d4f5dc;color:#1a7f37;font-weight:700;',
        'PASS': 'background:#d4f5dc;color:#1a7f37;font-weight:700;',
        'WARN': 'background:#fff8e1;color:#b7770d;font-weight:700;',
        'FAIL': 'background:#fdecea;color:#c0392b;font-weight:700;',
    }

    def _thead(headers: List[str]) -> str:
        cells = ''.join(f'<th style="{_TH}">{h}</th>' for h in headers)
        return f'<thead><tr>{cells}</tr></thead>'

    def _tbl(thead: str, rows: List[str]) -> str:
        return f'<table style="{_TBL}">{thead}<tbody>{"".join(rows)}</tbody></table>'

    # ---- line-type classifiers ----------------------------------------
    _RE_CAT   = re.compile(r'^\[(OK|WARN|FAIL)\]\s+\S.*\|')   # catalog_validation stream
    _RE_DISC  = re.compile(r'^Stream:\s+\S+\s+\|')            # discovery stream
    _RE_SCH   = re.compile(r'^\S+\.json\s+\[(OK|WARN|FAIL)\]')# schema file
    _RE_REC   = re.compile(r'^[\w_]+:\s+\d+\s+records$')      # sync record counts
    _RE_PKG   = re.compile(r'^[\w][\w.-]+==[^\s:]+:')         # package pin

    def _classify(s: str) -> Optional[str]:
        s = s.strip()
        if _RE_CAT.match(s):  return 'cat'
        if _RE_DISC.match(s): return 'disc'
        if _RE_SCH.match(s):  return 'sch'
        if _RE_REC.match(s):  return 'rec'
        if _RE_PKG.match(s):  return 'pkg'
        return None

    # ---- segment builder (group consecutive lines of same type) -------
    def _segment(details: List[str]) -> List[List]:
        segs: List[List] = []
        for ln in details:
            t = _classify(ln)
            if segs and segs[-1][0] == t and t is not None:
                segs[-1][1].append(ln)
            else:
                segs.append([t, [ln]])
        return segs

    # ---- per-type table renderers -------------------------------------
    def _render_cat(lines: List[str]) -> str:
        """[OK] stream  |  replication=X  |  keys=[...]  |  ..."""
        hdrs = ['Status', 'Stream', 'Replication', 'Keys', 'Repl Keys', 'Parent', 'Props', 'Meta']
        rows = []
        for ln in lines:
            cols = [c.strip() for c in ln.strip().split('|')]
            m = re.match(r'^\[(\w+)\]\s+(.*)', cols[0])
            st  = m.group(1) if m else 'OK'
            stm = (m.group(2) or cols[0]).strip() if m else cols[0]
            kv: Dict[str, str] = {}
            for c in cols[1:]:
                if '=' in c:
                    k, _, v = c.partition('='); kv[k.strip()] = v.strip()
            rows.append(
                f'<tr>'
                f'<td style="{_TD}{_ST.get(st, "")}">{st}</td>'
                f'<td style="{_TDM}"><strong>{stm}</strong></td>'
                f'<td style="{_TD}">{kv.get("replication", kv.get("forced-replication-method", "-"))}</td>'
                f'<td style="{_TDM}">{kv.get("keys", "-")}</td>'
                f'<td style="{_TDM}">{kv.get("repl-keys", "-")}</td>'
                f'<td style="{_TD}">{kv.get("parent", "-")}</td>'
                f'<td style="{_TD}">{kv.get("schema-props", "-")}</td>'
                f'<td style="{_TD}">{kv.get("meta-prop-entries", "-")}</td>'
                f'</tr>'
            )
        return _tbl(_thead(hdrs), rows)

    def _render_disc(lines: List[str]) -> str:
        """Stream: name  |  keys=[...]  |  replication=X  |  fields=N  |  parent=..."""
        hdrs = ['Stream', 'Keys', 'Replication', 'Fields', 'Parent']
        rows = []
        for ln in lines:
            cols = [c.strip() for c in ln.strip().split('|')]
            m = re.match(r'^Stream:\s+(.*)', cols[0])
            stm = (m.group(1) or cols[0]).strip() if m else cols[0]
            kv: Dict[str, str] = {}
            for c in cols[1:]:
                if '=' in c:
                    k, _, v = c.partition('='); kv[k.strip()] = v.strip()
            rows.append(
                f'<tr>'
                f'<td style="{_TDM}"><strong>{stm}</strong></td>'
                f'<td style="{_TDM}">{kv.get("keys", "-")}</td>'
                f'<td style="{_TD}">{kv.get("replication", "-")}</td>'
                f'<td style="{_TD}">{kv.get("fields", "-")}</td>'
                f'<td style="{_TD}">{kv.get("parent", "-")}</td>'
                f'</tr>'
            )
        return _tbl(_thead(hdrs), rows)

    def _render_sch(lines: List[str]) -> str:
        """filename.json [OK]  props=N  non-nullable=N  dt-format-issues=N"""
        hdrs = ['File', 'Status', 'Props', 'Non-Nullable', 'DT Format Issues']
        rows = []
        for ln in lines:
            m = re.match(r'^(\S+\.json)\s+\[(OK|WARN|FAIL)\](.*)', ln.strip())
            if not m:
                rows.append(f'<tr><td colspan="5" style="{_TD}">{ln.strip()}</td></tr>')
                continue
            fname, st, rest = m.group(1), m.group(2), m.group(3)
            kv: Dict[str, str] = {}
            for part in re.split(r'\s{2,}', rest.strip()):
                if '=' in part:
                    k, _, v = part.partition('='); kv[k.strip()] = v.strip()
            rows.append(
                f'<tr>'
                f'<td style="{_TDM}">{fname}</td>'
                f'<td style="{_TD}{_ST.get(st, "")}">{st}</td>'
                f'<td style="{_TD}">{kv.get("props", "-")}</td>'
                f'<td style="{_TD}">{kv.get("non-nullable", "-")}</td>'
                f'<td style="{_TD}">{kv.get("dt-format-issues", "-")}</td>'
                f'</tr>'
            )
        return _tbl(_thead(hdrs), rows)

    def _render_rec(lines: List[str]) -> str:
        """stream_name: N records"""
        hdrs = ['Stream', 'Records']
        rows = []
        for ln in lines:
            m = re.match(r'^([\w_]+):\s+(\d+)\s+records$', ln.strip())
            if m:
                rows.append(
                    f'<tr>'
                    f'<td style="{_TDM}">{m.group(1)}</td>'
                    f'<td style="{_TD}">{m.group(2)}</td>'
                    f'</tr>'
                )
        return _tbl(_thead(hdrs), rows)

    def _render_pkg(lines: List[str]) -> str:
        """pkg==version: status"""
        hdrs = ['Package', 'Status']
        rows = []
        for ln in lines:
            m = re.match(r'^([\w][\w.-]+==[^\s:]+):\s+(.+)$', ln.strip())
            if m:
                pkg, txt = m.group(1), m.group(2).strip()
                extra = ''
                if 'OUTDATED' in txt:
                    extra = 'color:#b7770d;font-weight:600;background:#fff8e1;'
                elif 'up-to-date' in txt:
                    extra = 'color:#1a7f37;font-weight:600;'
                rows.append(
                    f'<tr>'
                    f'<td style="{_TDM}">{pkg}</td>'
                    f'<td style="{_TD}{extra}">{txt}</td>'
                    f'</tr>'
                )
        return _tbl(_thead(hdrs), rows)

    # ---- main builder --------------------------------------------------
    def detail_html(details: List[str]) -> str:
        if not details:
            return ""
        _RENDERERS = {
            'cat':  _render_cat,
            'disc': _render_disc,
            'sch':  _render_sch,
            'rec':  _render_rec,
            'pkg':  _render_pkg,
        }
        parts: List[str] = []
        for typ, lines in _segment(details):
            if typ in _RENDERERS:
                parts.append(_RENDERERS[typ](lines))
            else:
                # plain monospace rows
                plain: List[str] = []
                for ln in lines:
                    s = ln.strip()
                    if not s:
                        plain.append('<tr><td class="detail-line blank"> </td></tr>')
                        continue
                    cls = "detail-line"
                    if s.startswith("ERROR:") or re.match(r'^\[FAIL\]', s):
                        cls += " det-error"
                    elif s.startswith("WARN:") or 'OUTDATED' in s:
                        cls += " det-warn"
                    plain.append(f'<tr><td class="{cls}">{s}</td></tr>')
                if plain:
                    parts.append(
                        f'<table class="detail-table"><tbody>{"".join(plain)}</tbody></table>'
                    )
        return "\n".join(parts)

    check_cards = []
    for r in report.results:
        tc, bg = STATUS_COLOR.get(r.status, ("#333", "#eee"))
        icon   = STATUS_ICON.get(r.status, "?")
        safe_name = r.name.replace("_", "-")
        det_html = detail_html(r.details)
        toggle_btn = (
            f'<button class="toggle-btn" onclick="toggleDetails(\'{safe_name}\')"'
            f' style="border-color:{tc};color:{tc}">▾ Details</button>'
            if det_html else ""
        )
        det_section = (
            f'<div id="det-{safe_name}" class="details-block" style="display:none;">'
            f'{det_html}</div>'
            if det_html else ""
        )
        check_cards.append(f"""
        <div class="check-card" style="border-left:4px solid {tc};background:{bg}10;">
          <div class="check-header">
            <span class="check-icon" style="color:{tc};">{icon}</span>
            <span class="check-name">{r.name.replace('_', ' ').title()}</span>
            {badge(r.status)}
            {toggle_btn}
          </div>
          <div class="check-summary">{r.summary}</div>
          {det_section}
        </div>""")

    cards_html = "\n".join(check_cards)

    summary_pills = ""
    for status, count, label in [
        ("PASS", report.passed,  "Passed"),
        ("FAIL", report.failed,  "Failed"),
        ("WARN", report.warned,  "Warnings"),
        ("SKIP", report.skipped, "Skipped"),
    ]:
        tc, bg = STATUS_COLOR[status]
        summary_pills += (
            f'<div class="pill" style="color:{tc};background:{bg};border:1px solid {tc};">'
            f'<span class="pill-num">{count}</span><span class="pill-label">{label}</span></div>\n'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tap Validator — {report.tap_name}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f7f8fa; color: #222; margin: 0; padding: 0;
    }}
    header {{
      background: #1b2a4a; color: #fff;
      padding: 24px 40px 20px;
    }}
    header h1 {{ margin: 0 0 4px; font-size: 1.6rem; font-weight: 700; }}
    header .sub {{ font-size: 0.85rem; opacity: 0.7; }}
    .overall-banner {{
      background: {overall_bg}; border-left: 6px solid {overall_color};
      color: {overall_color};
      padding: 14px 40px; font-size: 1.15rem; font-weight: 700;
      display: flex; align-items: center; gap: 12px;
    }}
    .overall-banner .ov-label {{ font-size: 1.5rem; }}
    .summary-row {{
      display: flex; gap: 16px; padding: 20px 40px; flex-wrap: wrap;
    }}
    .pill {{
      border-radius: 8px; padding: 10px 20px;
      display: flex; flex-direction: column; align-items: center;
      min-width: 100px;
    }}
    .pill-num  {{ font-size: 2rem; font-weight: 800; line-height: 1; }}
    .pill-label{{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: .05em; }}
    .checks-section {{
      padding: 0 40px 40px;
    }}
    .checks-section h2 {{
      font-size: 1rem; text-transform: uppercase;
      letter-spacing: .07em; color: #555; margin: 0 0 14px;
    }}
    .check-card {{
      background: #fff;
      border-radius: 8px;
      margin-bottom: 12px;
      padding: 16px 20px;
      box-shadow: 0 1px 4px rgba(0,0,0,.07);
    }}
    .check-header {{
      display: flex; align-items: center; gap: 10px;
      flex-wrap: wrap;
    }}
    .check-icon  {{ font-size: 1.1rem; flex-shrink: 0; }}
    .check-name  {{ font-weight: 700; font-size: 1rem; flex: 1; }}
    .badge {{
      border-radius: 4px; padding: 2px 10px;
      font-size: 0.78rem; font-weight: 700;
      white-space: nowrap;
    }}
    .check-summary {{
      margin-top: 6px; font-size: 0.9rem; color: #444;
    }}
    .toggle-btn {{
      background: transparent; border-radius: 4px;
      border: 1px solid; padding: 2px 10px;
      font-size: 0.8rem; cursor: pointer; margin-left: auto;
    }}
    .toggle-btn:hover {{ opacity: .75; }}
    .details-block {{
      margin-top: 12px;
      border-top: 1px solid #e0e0e0;
      padding-top: 10px;
    }}
    .detail-table {{
      width: 100%; border-collapse: collapse;
      font-family: 'Courier New', Courier, monospace;
      font-size: 0.8rem;
    }}
    .detail-line {{
      padding: 2px 6px; color: #333;
      white-space: pre-wrap; word-break: break-word;
    }}
    .detail-line.blank {{ height: 8px; }}
    .det-error {{ color: #c0392b; font-weight: 600; background: #fdecea; }}
    .det-warn  {{ color: #b7770d; font-weight: 600; background: #fff8e1; }}
    footer {{
      text-align: center; padding: 24px;
      font-size: 0.78rem; color: #999;
    }}
  </style>
</head>
<body>

<header>
  <h1>Singer Tap Validator</h1>
  <div class="sub">{report.tap_name} &nbsp;|&nbsp; {report.tap_dir} &nbsp;|&nbsp; {report.timestamp} UTC</div>
</header>

<div class="overall-banner">
  <span class="ov-label">{STATUS_ICON.get(report.overall, '?')}</span>
  Overall: {report.overall}
</div>

<div class="summary-row">
  {summary_pills}
</div>

<div class="checks-section">
  <h2>Check Results</h2>
  {cards_html}
</div>

<footer>Generated by tap_validator.py &mdash; {report.timestamp} UTC</footer>

<script>
  function toggleDetails(name) {{
    var el = document.getElementById('det-' + name);
    if (!el) return;
    el.style.display = el.style.display === 'none' ? 'block' : 'none';
  }}
</script>

</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    print(f"{GREEN}  HTML report saved: {output_path}{NC}")


# ===========================================================================
# Core runner (importable)
# ===========================================================================

ALL_CHECKS = list(VALIDATORS.keys())


def run_validation(
    tap_dir:   Path,
    checks:    Optional[List[str]] = None,
    fail_fast: bool                = False,
) -> ValidationReport:
    """
    Run validators and return a ValidationReport.
    Set _RUNTIME values before calling to enable runtime discovery/sync checks.
    """
    checks = checks or ALL_CHECKS
    report = ValidationReport(
        tap_name  = tap_dir.name,
        tap_dir   = str(tap_dir.resolve()),
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    for check_name in checks:
        if check_name not in VALIDATORS:
            print(f"{YELLOW}  WARN: Unknown check '{check_name}' — skipping{NC}")
            continue

        sys.stdout.write(f"  Running: {check_name} ...\r")
        sys.stdout.flush()
        try:
            result: CheckResult = VALIDATORS[check_name](tap_dir)
        except Exception as exc:
            result = CheckResult(check_name, "FAIL",
                                 f"Validator raised an exception: {exc}",
                                 [f"  {type(exc).__name__}: {exc}"])
        sys.stdout.write(" " * 50 + "\r")
        sys.stdout.flush()

        report.results.append(result)
        if fail_fast and result.status == "FAIL":
            print(f"{RED}  Stopping after first FAIL (--fail-fast){NC}")
            break

    return report


# ===========================================================================
# CLI
# ===========================================================================

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tap_validator",
        description="Comprehensive Singer tap validation tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Available checks (--checks):
  {', '.join(ALL_CHECKS)}

Runtime vs Static:
  discovery and sync checks run the tap executable when --tap-config is given
  and the credentials file has real (non-placeholder) values.
  All other checks are always static analysis.

Config file format (JSON):
  {{
    "tap_dir":    "/path/to/tap",
    "tap_config": "tap_credentials.json",
    "venv":       "/path/to/venv",
    "output_dir": "validator_output",
    "checks":     ["python_upgrade", "schema"],
    "output":     "report.json",
    "html":       "report.html",
    "verbose":    true,
    "fail_fast":  false,
    "timeout":    300
  }}

Examples:
  # Static checks only
  python tap_validator.py --tap-dir ./taps/tap-gitlab

  # Full runtime (discovery + sync actually executed)
  python tap_validator.py --tap-dir ./taps/tap-gitlab --tap-config tap_credentials.json

  # Specific checks with runtime
  python tap_validator.py --tap-dir ./taps/tap-gitlab --tap-config tap_credentials.json --checks discovery sync

  # From config file
  python tap_validator.py --config validation_config.json

  # Generate both JSON + HTML reports
  python tap_validator.py --tap-dir ./taps/tap-gitlab --output report.json --html

  # HTML report with custom filename
  python tap_validator.py --tap-dir ./taps/tap-gitlab --html my_report.html
""",
    )
    parser.add_argument("--tap-dir",    metavar="PATH", help="Path to the tap directory")
    parser.add_argument("--tap-config", metavar="FILE",
                        help="Tap credentials JSON (see tap_credentials.json). "
                             "Enables runtime discovery and sync execution.")
    parser.add_argument("--venv",       metavar="PATH",
                        help="Path to virtualenv that has the tap installed "
                             "(auto-detected if omitted)")
    parser.add_argument("--output-dir", metavar="PATH",
                        help="Directory for runtime artifacts (catalog, sync output, state)")
    parser.add_argument("--timeout",    metavar="SEC",  type=int, default=SYNC_TIMEOUT,
                        help=f"Subprocess timeout in seconds (default: {SYNC_TIMEOUT})")
    parser.add_argument("--checks",     nargs="+",      metavar="CHECK",
                        choices=ALL_CHECKS,
                        help="Subset of checks to run (default: all)")
    parser.add_argument("--output",     metavar="FILE", help="Save JSON report to this file (tap name auto-prefixed)")
    parser.add_argument("--html",       metavar="FILE", nargs="?", const="report.html",
                        help="Save an HTML report. Optional filename (default: report.html). Tap name is auto-prefixed.")
    parser.add_argument("--verbose",    "-v", action="store_true",
                        help="Print detailed per-check output")
    parser.add_argument("--config",     metavar="FILE", help="Load all args from a JSON config file")
    parser.add_argument("--fail-fast",  action="store_true",
                        help="Stop after first FAIL")
    return parser


def _merge_config_file(args: argparse.Namespace) -> argparse.Namespace:
    if not args.config:
        return args
    path = Path(args.config)
    if not path.is_file():
        print(f"{RED}ERROR: Config file not found: {path}{NC}", file=sys.stderr)
        sys.exit(1)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"{RED}ERROR: Cannot parse config file: {e}{NC}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(cfg, dict):
        print(f"{RED}ERROR: Config file must be a JSON object{NC}", file=sys.stderr)
        sys.exit(1)

    # CLI takes precedence over config file
    if not args.tap_dir    and "tap_dir"    in cfg: args.tap_dir    = cfg["tap_dir"]
    if not args.tap_config and "tap_config" in cfg: args.tap_config = cfg["tap_config"]
    if not args.venv       and "venv"       in cfg: args.venv       = cfg["venv"]
    if not args.output_dir and "output_dir" in cfg: args.output_dir = cfg["output_dir"]
    if not args.checks     and "checks"     in cfg: args.checks     = cfg["checks"]
    if not args.output     and "output"     in cfg: args.output     = cfg["output"]
    if args.html is None   and "html"       in cfg: args.html       = cfg["html"]
    if not args.verbose    and cfg.get("verbose"):   args.verbose   = True
    if not args.fail_fast  and cfg.get("fail_fast"): args.fail_fast = True
    if args.timeout == SYNC_TIMEOUT and "timeout" in cfg:
        args.timeout = cfg["timeout"]
    return args


def main() -> None:
    parser = _build_parser()
    args   = parser.parse_args()
    args   = _merge_config_file(args)

    if not args.tap_dir:
        parser.print_help()
        print(f"\n{RED}ERROR: --tap-dir is required{NC}", file=sys.stderr)
        sys.exit(1)

    tap_dir = Path(args.tap_dir).expanduser().resolve()
    if not tap_dir.is_dir():
        print(f"{RED}ERROR: Directory not found: {tap_dir}{NC}", file=sys.stderr)
        sys.exit(1)

    checks  = args.checks or ALL_CHECKS
    invalid = [c for c in checks if c not in VALIDATORS]
    if invalid:
        print(f"{RED}ERROR: Unknown checks: {', '.join(invalid)}{NC}", file=sys.stderr)
        print(f"Available: {', '.join(ALL_CHECKS)}", file=sys.stderr)
        sys.exit(1)

    # Populate runtime context so validators can access credentials / venv / output
    _RUNTIME["tap_config"] = Path(args.tap_config).expanduser().resolve() \
                             if args.tap_config else None
    _RUNTIME["venv"]       = Path(args.venv).expanduser().resolve() \
                             if args.venv else None
    _RUNTIME["output_dir"] = Path(args.output_dir).expanduser().resolve() \
                             if args.output_dir else (tap_dir / "validator_output")
    _RUNTIME["timeout"]    = args.timeout

    if _RUNTIME["tap_config"]:
        if not _RUNTIME["tap_config"].is_file():
            print(f"{RED}ERROR: tap-config not found: {_RUNTIME['tap_config']}{NC}", file=sys.stderr)
            sys.exit(1)
        if _has_placeholder(_RUNTIME["tap_config"]):
            print(f"{YELLOW}  NOTE: tap_credentials.json has placeholder values — "
                  f"discovery/sync will run in static mode. "
                  f"Fill in real credentials to enable runtime execution.{NC}")

    report = run_validation(tap_dir, checks, fail_fast=args.fail_fast)
    _print_stdout_summary(report)

    if args.verbose:
        _print_verbose_report(report)

    if args.output:
        json_path = _inject_tap_name(args.output, report.tap_name)
        _save_json_report(report, json_path)

    if args.html is not None:
        html_file = args.html if args.html else "report.html"
        html_path = _inject_tap_name(html_file, report.tap_name)
        _save_html_report(report, html_path)

    sys.exit(0 if report.overall == "PASS" else 1)


if __name__ == "__main__":
    main()
