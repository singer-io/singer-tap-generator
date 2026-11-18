"""Centralised registry of benchmarkable Singer taps plus tap-agnostic preflight checks."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import WORKSPACE_ROOT

STATUS_READY = "READY"
STATUS_DISABLED = "SKIPPED"
STATUS_INVALID_CONFIG = "SKIPPED_INVALID_CONFIG"
STATUS_ENV_ERROR = "SKIPPED_ENVIRONMENT"

_SECRET_KEY = re.compile(r"(pass|secret|token|key|credential|assertion|signature|private)", re.I)
_CATALOG_FLAGS = ("--catalog", "--properties")
_CONFIG_FLAGS = ("--config", "-c")


class RegistryError(Exception):
    pass


@dataclass
class TapSpec:
    """Everything the framework needs to know about one tap."""
    name: str
    repo_path: str
    config_path: str
    sync_command_path: str
    catalog_path: str
    discovery_command_path: Optional[str] = None
    output_path: Optional[str] = None
    add_selected_script: Optional[str] = None
    extra_secret_files: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    enabled: bool = True
    known_invalid_config: bool = False
    skip_reason: str = ""
    python_path: Optional[str] = None
    catalog_strategy: str = "preserve_existing"
    config_overrides: Dict[str, Any] = field(default_factory=dict)
    required_config_keys: List[str] = field(default_factory=list)
    parent_metadata_keys: List[str] = field(default_factory=lambda: ["parent-tap-stream-id", "parent-stream"])
    streams_per_task: int = 1
    adapter: str = "process_isolation"
    entry_point: Optional[str] = None
    independent_streams: List[str] = field(default_factory=list)
    isolation_findings: List[str] = field(default_factory=list)
    stream_include: List[str] = field(default_factory=list)
    stream_exclude: List[str] = field(default_factory=list)
    rate_limit_info: str = "UNKNOWN"
    notes: str = ""
    extra_env: Dict[str, str] = field(default_factory=dict)

    def repo_file(self, relative: Optional[str]) -> Optional[str]:
        if not relative:
            return None
        if os.path.isabs(relative):
            return relative
        return os.path.normpath(os.path.join(self.repo_path, relative))

    @property
    def abs_config(self) -> str:
        return self.repo_file(self.config_path)  # type: ignore[return-value]

    @property
    def abs_catalog(self) -> str:
        return self.repo_file(self.catalog_path)  # type: ignore[return-value]

    @property
    def abs_sync_command(self) -> str:
        return self.repo_file(self.sync_command_path)  # type: ignore[return-value]

    def python_executable(self) -> Optional[str]:
        candidates = []
        if self.python_path:
            candidates.append(self.repo_file(self.python_path))
        candidates.append(os.path.join(self.repo_path, "venv", "Scripts", "python.exe"))
        candidates.append(os.path.join(self.repo_path, "venv", "bin", "python"))
        candidates.append(os.path.join(self.repo_path, ".venv", "Scripts", "python.exe"))
        candidates.append(os.path.join(self.repo_path, ".venv", "bin", "python"))
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        return None


@dataclass
class SyncCommand:
    """The tap's own sync command, parsed so it can be re-targeted without editing the file."""
    script: str
    args: List[str]
    catalog_flag: str
    config_flag: str

    def build_args(self, catalog_path: str, config_path: Optional[str] = None) -> List[str]:
        rebuilt = list(self.args)
        for index, token in enumerate(rebuilt[:-1]):
            if token == self.catalog_flag:
                rebuilt[index + 1] = catalog_path
            elif token == self.config_flag and config_path:
                rebuilt[index + 1] = config_path
        return rebuilt


def tokenize_command(path: str) -> List[str]:
    """Tokenise the first command line of a file, dropping shell redirections."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        lines = [ln.strip() for ln in handle.read().splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise RegistryError("command file is empty: %s" % path)
    tokens = shlex.split(lines[0], posix=True)
    kept: List[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if re.fullmatch(r"\d?>>?", token):
            skip_next = True
            continue
        if re.match(r"^\d?>>?\S", token) or re.fullmatch(r"\d>&\d", token):
            continue
        kept.append(token)
    if not kept:
        raise RegistryError("no command found in %s" % path)
    return kept


def parse_sync_command(path: str) -> SyncCommand:
    """Parse ``tap-x --config c.json --catalog k.json > out.json`` into script + args."""
    kept = tokenize_command(path)
    script, args = kept[0], kept[1:]
    catalog_flag = next((f for f in _CATALOG_FLAGS if f in args), None)
    config_flag = next((f for f in _CONFIG_FLAGS if f in args), None)
    if not catalog_flag or not config_flag:
        raise RegistryError("sync command %s must contain a config flag and one of %s" % (path, _CATALOG_FLAGS))
    return SyncCommand(script=script, args=args, catalog_flag=catalog_flag, config_flag=config_flag)


def load_json_file(path: str) -> Any:
    """Load JSON regardless of BOM / UTF-16 (PowerShell redirect) encodings."""
    with open(path, "rb") as handle:
        raw = handle.read()
    if not raw.strip():
        raise ValueError("file is empty")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
    return json.loads(text)


def load_registry(path: str, workspace_root: str = WORKSPACE_ROOT) -> Dict[str, TapSpec]:
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    specs: Dict[str, TapSpec] = {}
    for name, entry in raw.get("taps", {}).items():
        entry = dict(entry)
        repo = entry.pop("repo_path", name)
        if not os.path.isabs(repo):
            repo = os.path.normpath(os.path.join(workspace_root, repo))
        entry = {k: v for k, v in entry.items() if not k.startswith("_")}
        try:
            specs[name] = TapSpec(name=name, repo_path=repo, **entry)
        except TypeError as exc:
            raise RegistryError("invalid registry entry for %s: %s" % (name, exc)) from exc
    return specs


def resolve_tap_name(specs: Dict[str, TapSpec], requested: str) -> str:
    if requested in specs:
        return requested
    for name, spec in specs.items():
        if requested in spec.aliases:
            return name
    raise RegistryError("unknown tap %r; known taps: %s" % (requested, ", ".join(sorted(specs))))


@dataclass
class PreflightResult:
    status: str
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    needs_discovery: bool = False


def preflight(spec: TapSpec) -> PreflightResult:
    """Cheap, offline validation. Never contacts the API."""
    reasons: List[str] = []
    warnings: List[str] = []
    needs_discovery = False

    if spec.known_invalid_config:
        reasons.append(spec.skip_reason or "registry marks this tap's configuration as invalid")

    if not os.path.isdir(spec.repo_path):
        reasons.append("repository not found: %s" % spec.repo_path)
    else:
        config_file = spec.abs_config
        if not os.path.isfile(config_file):
            reasons.append("config file missing: %s" % spec.config_path)
        else:
            try:
                config = load_json_file(config_file)
                if not isinstance(config, dict):
                    reasons.append("config file is not a JSON object")
                else:
                    missing = [k for k in spec.required_config_keys if not config.get(k)]
                    if missing:
                        reasons.append("config missing required keys: %s" % ", ".join(missing))
            except Exception as exc:  # pylint: disable=broad-except
                reasons.append("config file unreadable: %s" % type(exc).__name__)

        catalog_file = spec.abs_catalog
        catalog_ok = False
        if os.path.isfile(catalog_file):
            try:
                load_json_file(catalog_file)
                catalog_ok = True
            except Exception as exc:  # pylint: disable=broad-except
                reasons.append("existing catalog unusable (%s): %s" % (spec.catalog_path, exc))
        else:
            reasons.append("existing catalog missing: %s" % spec.catalog_path)
        if not catalog_ok and spec.catalog_strategy == "discover_if_missing" and not spec.known_invalid_config:
            reasons = [r for r in reasons if not r.startswith("existing catalog")]
            needs_discovery = True
            warnings.append("existing catalog unusable; a fresh discovery will be run into a new file")

        if not os.path.isfile(spec.abs_sync_command):
            reasons.append("sync command file missing: %s" % spec.sync_command_path)

    if reasons:
        return PreflightResult(STATUS_INVALID_CONFIG, reasons, warnings)
    if not spec.enabled:
        return PreflightResult(STATUS_DISABLED, ["tap disabled in registry"], warnings)
    if spec.python_executable() is None:
        return PreflightResult(STATUS_ENV_ERROR, ["no virtualenv python found under %s" % spec.repo_path], warnings)
    return PreflightResult(STATUS_READY, [], warnings, needs_discovery)


def collect_secrets(spec: TapSpec) -> List[str]:
    """Return secret-looking values from the tap's config files so logs can be scrubbed."""
    secrets: List[str] = []
    files = [spec.abs_config] + [spec.repo_file(p) for p in spec.extra_secret_files]
    for path in files:
        if not path or not os.path.isfile(path):
            continue
        try:
            data = load_json_file(path)
        except Exception:  # pylint: disable=broad-except
            continue
        _walk_secrets(data, secrets, forced=False)
    unique = sorted({s for s in secrets if len(s) >= 4}, key=len, reverse=True)
    return unique


def _walk_secrets(node: Any, out: List[str], forced: bool) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _walk_secrets(value, out, forced or bool(_SECRET_KEY.search(str(key))))
    elif isinstance(node, list):
        for value in node:
            _walk_secrets(value, out, forced)
    elif forced and isinstance(node, str):
        out.append(node)
        for line in node.splitlines():
            if len(line.strip()) >= 12:
                out.append(line.strip())


def describe_environment() -> str:
    return "python %s on %s" % (sys.version.split()[0], sys.platform)
