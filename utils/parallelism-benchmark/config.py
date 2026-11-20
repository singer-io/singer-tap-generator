"""Benchmark settings: JSON config file + CLI overrides, record-limit policy, validation, shared helpers."""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

FRAMEWORK_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_ROOT = os.path.dirname(FRAMEWORK_DIR)
DEFAULT_CONFIG_PATH = os.path.join(FRAMEWORK_DIR, "configs", "benchmark_config.json")
DEFAULT_REGISTRY_PATH = os.path.join(FRAMEWORK_DIR, "configs", "tap_registry.json")
RESULTS_DIR = os.path.join(FRAMEWORK_DIR, "results")
LOGS_DIR = os.path.join(FRAMEWORK_DIR, "logs")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
SCHEDULE_ORDERS = ("catalog", "longest_first")

# Config-file spellings that are accepted for a canonical key.
CONFIG_ALIASES = {"record_limit": "default_record_limit"}


class ConfigError(ValueError):
    """Raised for invalid benchmark configuration (message is user-facing)."""


def utc_iso(epoch: Optional[float] = None) -> str:
    """RFC 3339 UTC timestamp with millisecond precision."""
    moment = datetime.fromtimestamp(time.time() if epoch is None else epoch, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def new_run_id(results_dir: Optional[str] = None) -> str:
    """Timestamped, unique run id such as ``2026-11-18_16-30-00`` (suffix ``_2`` if the folder exists)."""
    base = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    if not results_dir:
        return base
    candidate, counter = base, 1
    while os.path.exists(os.path.join(results_dir, candidate)):
        counter += 1
        candidate = "%s_%d" % (base, counter)
    return candidate


def unique_path(path: str) -> str:
    """Return ``path`` or, if it exists, the first free ``name_N.ext`` variant."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    counter = 1
    while os.path.exists("%s_%d%s" % (base, counter, ext)):
        counter += 1
    return "%s_%d%s" % (base, counter, ext)


@dataclass
class BenchmarkConfig:
    """All run settings. Every field can be set in the JSON config file; most also have a CLI flag."""
    workers: List[int] = field(default_factory=lambda: [1, 2, 3, 4])
    enabled_taps: List[str] = field(default_factory=list)
    timeout_seconds: int = 1800
    stream_timeout_seconds: Optional[int] = None
    continue_on_stream_failure: bool = True
    stop_on_unstable_run: bool = False
    merge_output: bool = True
    keep_unit_outputs: bool = False
    native_baseline: bool = False
    registry_path: str = DEFAULT_REGISTRY_PATH
    results_dir: str = RESULTS_DIR
    logs_dir: str = LOGS_DIR
    log_level: str = "INFO"
    default_record_limit: Optional[int] = None
    tap_record_limits: Dict[str, int] = field(default_factory=dict)
    stream_record_limits: Dict[str, Dict[str, int]] = field(default_factory=dict)
    max_429_ratio: float = 0.10
    record_tolerance_pct: float = 2.0
    recommendation_min_speedup: float = 1.2
    schedule_order: str = "longest_first"
    check_environment: bool = True
    baseline_fail_fast: int = 3
    max_streams: Optional[int] = None
    tap_max_streams: Dict[str, int] = field(default_factory=dict)
    sources: Dict[str, str] = field(default_factory=dict)
    # CLI-only record-limit layers (kept separate so precedence is explicit)
    cli_record_limit: Optional[int] = None
    cli_no_record_limit: bool = False
    cli_tap_record_limits: Dict[str, int] = field(default_factory=dict)
    cli_stream_record_limits: Dict[str, Dict[str, int]] = field(default_factory=dict)
    cli_max_streams: Optional[int] = None
    cli_all_streams: bool = False
    cli_tap_max_streams: Dict[str, int] = field(default_factory=dict)

    @property
    def effective_stream_timeout(self) -> int:
        return self.stream_timeout_seconds or self.timeout_seconds

    def public_dict(self) -> Dict[str, Any]:
        """Configuration as recorded in the results summary (includes where each value came from)."""
        data = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "sources"}
        data["value_sources"] = dict(self.sources)
        return data


# --------------------------------------------------------------------------- record limits

def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().lstrip("+").isdigit():
        raise ConfigError("%s must be a positive integer (got %r)" % (label, value))
    number = int(value)
    if number < 1:
        raise ConfigError("%s must be >= 1 (got %s)" % (label, value))
    return number


class RecordLimitPolicy:
    """Resolves the record limit of a stream.

    Precedence (highest first):
      1. CLI ``--stream-record-limit TAP:STREAM=N``
      2. CLI ``--tap-record-limit TAP=N``
      3. CLI ``--record-limit N`` (uniform limit, overrides every config-file limit) / ``--no-record-limit``
      4. config ``stream_record_limits``
      5. config ``tap_record_limits``
      6. config ``default_record_limit``
      7. unlimited
    """

    def __init__(self, config: BenchmarkConfig, canonical: Callable[[str], str] = lambda n: n) -> None:
        self.cfg_default = config.default_record_limit
        self.cfg_tap = {canonical(k): v for k, v in config.tap_record_limits.items()}
        self.cfg_stream = {canonical(t): dict(s) for t, s in config.stream_record_limits.items()}
        self.cli_all = config.cli_record_limit
        self.cli_off = config.cli_no_record_limit
        self.cli_tap = {canonical(k): v for k, v in config.cli_tap_record_limits.items()}
        self.cli_stream = {canonical(t): dict(s) for t, s in config.cli_stream_record_limits.items()}

    def resolve(self, tap: str, stream: str) -> Tuple[Optional[int], str]:
        if stream in self.cli_stream.get(tap, {}):
            return self.cli_stream[tap][stream], "cli-stream"
        if tap in self.cli_tap:
            return self.cli_tap[tap], "cli-tap"
        if self.cli_off:
            return None, "cli-unlimited"
        if self.cli_all is not None:
            return self.cli_all, "cli-global"
        if stream in self.cfg_stream.get(tap, {}):
            return self.cfg_stream[tap][stream], "config-stream"
        if tap in self.cfg_tap:
            return self.cfg_tap[tap], "config-tap"
        if self.cfg_default is not None:
            return self.cfg_default, "config-default"
        return None, "unlimited"

    def limit_for(self, tap: str) -> Callable[[str], Optional[int]]:
        return lambda stream: self.resolve(tap, stream)[0]

    def describe(self, tap: str, streams: List[str]) -> str:
        """Short human string: ``500``, ``unlimited`` or ``mixed (500 x 12, unlimited x 2)``."""
        counts: Dict[str, int] = {}
        for stream in streams:
            limit = self.resolve(tap, stream)[0]
            key = "unlimited" if limit is None else str(limit)
            counts[key] = counts.get(key, 0) + 1
        if len(counts) == 1:
            return next(iter(counts))
        if not counts:
            return "unlimited"
        return "mixed (%s)" % ", ".join("%s x %d" % kv for kv in sorted(counts.items()))


class StreamSamplePolicy:
    """How many of a tap's selected work units to benchmark (``None`` = all).

    Precedence (highest first): CLI ``--tap-max-streams TAP=N`` > CLI ``--all-streams`` / ``--max-streams N`` >
    config ``tap_max_streams`` > config ``max_streams`` > all. A limit keeps the *first N* work units of the catalog
    (deterministic, identical for every worker count), so a sampled run is still a like-for-like comparison.
    """

    def __init__(self, config: BenchmarkConfig, canonical: Callable[[str], str] = lambda n: n) -> None:
        self.cfg_all = config.max_streams
        self.cfg_tap = {canonical(k): v for k, v in config.tap_max_streams.items()}
        self.cli_all = config.cli_max_streams
        self.cli_off = config.cli_all_streams
        self.cli_tap = {canonical(k): v for k, v in config.cli_tap_max_streams.items()}

    def resolve(self, tap: str) -> Tuple[Optional[int], str]:
        if tap in self.cli_tap:
            return self.cli_tap[tap], "cli-tap"
        if self.cli_off:
            return None, "cli-all-streams"
        if self.cli_all is not None:
            return self.cli_all, "cli-global"
        if tap in self.cfg_tap:
            return self.cfg_tap[tap], "config-tap"
        if self.cfg_all is not None:
            return self.cfg_all, "config-global"
        return None, "all-streams"


def parse_limit_assignments(items: Optional[List[str]], nested: bool) -> Dict[Any, Any]:
    """Parse ``TAP=N`` (nested=False) or ``TAP:STREAM=N`` (nested=True) CLI assignments."""
    result: Dict[Any, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ConfigError("invalid limit %r: expected %s" % (item, "TAP:STREAM=N" if nested else "TAP=N"))
        key, value = item.rsplit("=", 1)
        number = _positive_int(value, "record limit in %r" % item)
        if nested:
            if ":" not in key:
                raise ConfigError("invalid limit %r: expected TAP:STREAM=N" % item)
            tap, stream = key.split(":", 1)
            result.setdefault(tap.strip(), {})[stream.strip()] = number
        else:
            result[key.strip()] = number
    return result


# --------------------------------------------------------------------------- loading / validation

def _normalise_workers(values: Any) -> List[int]:
    if not isinstance(values, list) or not values:
        raise ConfigError("workers must be a non-empty list of integers (got %r)" % (values,))
    cleaned: List[int] = []
    for value in values:
        number = _positive_int(value, "worker count")
        if number not in cleaned:
            cleaned.append(number)
    # The sequential baseline is always required for comparisons.
    if 1 not in cleaned:
        cleaned.append(1)
    return sorted(cleaned)


def validate_config(config: BenchmarkConfig) -> None:
    config.workers = _normalise_workers(config.workers)
    _positive_int(config.timeout_seconds, "timeout_seconds")
    if config.stream_timeout_seconds is not None:
        _positive_int(config.stream_timeout_seconds, "stream_timeout_seconds")
    if config.default_record_limit is not None:
        config.default_record_limit = _positive_int(config.default_record_limit, "default_record_limit")
    if not isinstance(config.tap_record_limits, dict):
        raise ConfigError("tap_record_limits must be an object {tap: limit}")
    config.tap_record_limits = {str(k): _positive_int(v, "tap_record_limits[%s]" % k)
                                for k, v in config.tap_record_limits.items()}
    if not isinstance(config.stream_record_limits, dict):
        raise ConfigError("stream_record_limits must be an object {tap: {stream: limit}}")
    cleaned: Dict[str, Dict[str, int]] = {}
    for tap, streams in config.stream_record_limits.items():
        if not isinstance(streams, dict):
            raise ConfigError("stream_record_limits[%s] must be an object {stream: limit}" % tap)
        cleaned[str(tap)] = {str(s): _positive_int(v, "stream_record_limits[%s][%s]" % (tap, s))
                             for s, v in streams.items()}
    config.stream_record_limits = cleaned
    if str(config.log_level).upper() not in LOG_LEVELS:
        raise ConfigError("log_level must be one of %s" % ", ".join(LOG_LEVELS))
    if config.schedule_order not in SCHEDULE_ORDERS:
        raise ConfigError("schedule_order must be one of %s" % ", ".join(SCHEDULE_ORDERS))
    if config.max_streams is not None:
        config.max_streams = _positive_int(config.max_streams, "max_streams")
    if not isinstance(config.tap_max_streams, dict):
        raise ConfigError("tap_max_streams must be an object {tap: N}")
    config.tap_max_streams = {str(k): _positive_int(v, "tap_max_streams[%s]" % k) for k, v in config.tap_max_streams.items()}
    config.baseline_fail_fast = _positive_int(config.baseline_fail_fast, "baseline_fail_fast")
    if not isinstance(config.check_environment, bool):
        raise ConfigError("check_environment must be true or false")
    config.log_level = str(config.log_level).upper()
    if not 0 <= float(config.max_429_ratio) <= 1:
        raise ConfigError("max_429_ratio must be between 0 and 1")
    if not isinstance(config.enabled_taps, list):
        raise ConfigError("enabled_taps must be a list of tap names")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark stream-level parallelism of Singer taps without modifying them. "
                    "Command-line values override the configuration file.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="benchmark_config.json path")
    parser.add_argument("--registry", default=None, help="tap_registry.json path")
    parser.add_argument("--workers", nargs="+", type=int, help="worker counts, e.g. 1 2 3 4 (1 is always added)")
    parser.add_argument("--taps", nargs="+", help="tap names to run (default: enabled_taps from the config)")
    parser.add_argument("--record-limit", type=int, metavar="N",
                        help="uniform per-stream record limit; overrides every record limit in the config file")
    parser.add_argument("--no-record-limit", action="store_true", help="disable all config-file record limits")
    parser.add_argument("--tap-record-limit", action="append", metavar="TAP=N",
                        help="per-tap record limit (repeatable; beats --record-limit)")
    parser.add_argument("--stream-record-limit", action="append", metavar="TAP:STREAM=N",
                        help="per-stream record limit (repeatable; highest precedence)")
    parser.add_argument("--timeout", type=int, help="per-run timeout in seconds (overrides timeout_seconds)")
    parser.add_argument("--stream-timeout", type=int, help="per-work-unit timeout in seconds")
    parser.add_argument("--stop-on-failure", action="store_true",
                        help="stop scheduling new streams after the first failure")
    parser.add_argument("--stop-on-unstable", action="store_true",
                        help="do not try higher worker counts after a run with failures or HTTP 429")
    parser.add_argument("--no-merge", action="store_true", help="skip merging per-stream outputs")
    parser.add_argument("--keep-unit-outputs", action="store_true",
                        help="keep per-stream output files after merging")
    parser.add_argument("--native-baseline", action="store_true",
                        help="also run the tap's own unmodified single-process sync as a reference row")
    parser.add_argument("--max-streams", type=int, metavar="N",
                        help="benchmark only the first N work units of every tap (cheaper runs; marked as a sampled run). "
                             "Overrides max_streams / tap_max_streams from the config file")
    parser.add_argument("--tap-max-streams", action="append", metavar="TAP=N",
                        help="per-tap work-unit limit (repeatable; beats --max-streams)")
    parser.add_argument("--all-streams", action="store_true", help="ignore max_streams / tap_max_streams from the config file")
    parser.add_argument("--skip-env-check", action="store_true",
                        help="do not run the tap readiness probe before benchmarking (not recommended)")
    parser.add_argument("--results-dir", help="where timestamped result folders are created")
    parser.add_argument("--logs-dir", help="where per-tap logs are written")
    parser.add_argument("--log-level", choices=LOG_LEVELS, help="framework log level")
    parser.add_argument("--schedule", choices=("catalog", "longest-first"),
                        help="order in which work units are handed to workers in parallel runs: longest-first "
                             "(default; uses the sequential baseline's durations so one slow stream cannot start last and "
                             "dominate the result) or catalog order")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate taps (including the readiness probe) and print the stream plan, record limits and "
                             "number of planned runs; run nothing")
    parser.add_argument("--run-id", default=None, help="override the generated run id (default: timestamp)")
    parser.add_argument("--list-taps", action="store_true", help="list registry taps and exit")
    return parser


def load_config(args: argparse.Namespace) -> BenchmarkConfig:
    """Defaults < JSON config file < command line. Records where each value came from."""
    config = BenchmarkConfig()
    sources = {f.name: "default" for f in fields(config) if f.name != "sources"}
    cli_only = {"sources", "cli_record_limit", "cli_no_record_limit", "cli_tap_record_limits",
                "cli_stream_record_limits", "cli_max_streams", "cli_all_streams", "cli_tap_max_streams"}
    known = {f.name for f in fields(config)} - cli_only
    if args.config:
        if not os.path.isfile(args.config):
            if os.path.abspath(args.config) != DEFAULT_CONFIG_PATH:
                raise ConfigError("config file not found: %s" % args.config)
        else:
            try:
                with open(args.config, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
            except ValueError as exc:
                raise ConfigError("config file %s is not valid JSON: %s" % (args.config, exc)) from exc
            if not isinstance(raw, dict):
                raise ConfigError("config file %s must contain a JSON object" % args.config)
            for key, value in raw.items():
                if key.startswith("_"):
                    continue
                key = CONFIG_ALIASES.get(key, key)
                if key not in known:
                    raise ConfigError("unknown key %r in %s (known keys: %s)" % (
                        key, args.config, ", ".join(sorted(known))))
                setattr(config, key, value)
                sources[key] = "config-file"

    def override(key: str, value: Any) -> None:
        setattr(config, key, value)
        sources[key] = "cli"

    if args.registry:
        override("registry_path", args.registry)
    if args.workers:
        override("workers", args.workers)
    if args.taps:
        override("enabled_taps", args.taps)
    if args.timeout:
        override("timeout_seconds", args.timeout)
    if args.stream_timeout:
        override("stream_timeout_seconds", args.stream_timeout)
    if args.stop_on_failure:
        override("continue_on_stream_failure", False)
    if args.stop_on_unstable:
        override("stop_on_unstable_run", True)
    if args.no_merge:
        override("merge_output", False)
    if args.keep_unit_outputs:
        override("keep_unit_outputs", True)
    if args.native_baseline:
        override("native_baseline", True)
    if args.results_dir:
        override("results_dir", os.path.abspath(args.results_dir))
    if args.logs_dir:
        override("logs_dir", os.path.abspath(args.logs_dir))
    if args.log_level:
        override("log_level", args.log_level)
    if args.schedule:
        override("schedule_order", args.schedule.replace("-", "_"))
    if args.skip_env_check:
        override("check_environment", False)
    if args.max_streams is not None:
        config.cli_max_streams = _positive_int(args.max_streams, "--max-streams")
        sources["max_streams"] = "cli (--max-streams overrides all config stream limits)"
    config.cli_all_streams = bool(args.all_streams)
    if args.all_streams:
        sources["max_streams"] = "cli (--all-streams)"
    config.cli_tap_max_streams = parse_limit_assignments(args.tap_max_streams, nested=False)
    if args.record_limit is not None:
        config.cli_record_limit = _positive_int(args.record_limit, "--record-limit")
        sources["default_record_limit"] = "cli (--record-limit overrides all config limits)"
    config.cli_no_record_limit = bool(args.no_record_limit)
    if args.no_record_limit:
        sources["default_record_limit"] = "cli (--no-record-limit)"
    config.cli_tap_record_limits = parse_limit_assignments(args.tap_record_limit, nested=False)
    config.cli_stream_record_limits = parse_limit_assignments(args.stream_record_limit, nested=True)
    config.sources = sources
    validate_config(config)
    return config
