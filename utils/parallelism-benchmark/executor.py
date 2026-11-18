"""Stream scheduler and subprocess executor.

Stream-level parallelism is achieved *outside* the tap: the selected streams are split into
independent work units (a stream plus its child streams) and a pool of N worker slots pulls units
from a shared queue. Each unit runs the tap's own, unmodified entry point in a separate process with
a trimmed catalog, its own stdout file, stderr log and HTTP-event file, so no state, session,
rate-limit counter or output file is shared between concurrently running streams.

Record limits are enforced at the Singer protocol layer: every RECORD on a unit's stdout is counted
per stream; records beyond a stream's limit are dropped, and when a unit's driving (root) stream hits
its limit the tap process tree is stopped immediately so no further API pages are requested.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from catalog_utils import RUNNER_PATH, WorkUnit
from config import utc_iso
from error_analyzer import Scrubber, StderrScanner, build_unit_errors
from metrics import (RunResult, StreamTally, UnitResult, summarize_events, concurrency_stats,
                     COMPLETED, COMPLETED_WITH_LIMIT, FAILED, SKIPPED, TIMED_OUT, OK_STATUSES)
from tap_registry import SyncCommand, TapSpec

_MESSAGE_PREFIX = re.compile(rb'^\{"type": ?"(\w+)"(?:, ?"stream": ?"((?:[^"\\]|\\.)*)")?')


class _UtcFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):  # noqa: N802
        return utc_iso(record.created)


def make_run_logger(name: str, path: str, console: bool = True, level: str = "INFO") -> logging.Logger:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    formatter = _UtcFormatter("%(asctime)s %(levelname)-7s %(message)s")
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if console:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    return logger


def close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a process and all of its children."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # pylint: disable=no-member
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


class UnitExecutor:
    """Runs one work unit (one tap process) and turns its outcome into a UnitResult."""

    def __init__(self, spec: TapSpec, sync_command: SyncCommand, python: str, scrubber: Scrubber,
                 run_dir: str, worker_count: int, logger: logging.Logger,
                 config_path: Optional[str] = None, enforce_limits: bool = True) -> None:
        self.spec = spec
        self.sync_command = sync_command
        self.python = python
        self.scrubber = scrubber
        self.run_dir = run_dir
        self.worker_count = worker_count
        self.logger = logger
        self.config_path = config_path
        self.enforce_limits = enforce_limits
        self.active: set = set()
        self._active_lock = threading.Lock()

    def terminate_all(self) -> None:
        with self._active_lock:
            procs = list(self.active)
        for proc in procs:
            kill_process_tree(proc)

    def _command(self, unit: WorkUnit) -> List[str]:
        args = self.sync_command.build_args(unit.catalog_path, self.config_path or self.spec.abs_config)
        cmd = [self.python, RUNNER_PATH, "--script", self.sync_command.script]
        if self.spec.entry_point:
            cmd += ["--entry", self.spec.entry_point]
        return cmd + ["--"] + args

    def execute(self, unit: WorkUnit, worker_id: int, timeout: float) -> UnitResult:
        stem = "_unit_%03d_%s" % (unit.index, re.sub(r"[^A-Za-z0-9_.-]", "_", unit.unit_id)[:60])
        result = UnitResult(
            tap_name=self.spec.name, worker_count=self.worker_count, unit_id=unit.unit_id,
            streams=list(unit.selected), roots=list(unit.roots), worker_id=worker_id,
            output_path=os.path.join(self.run_dir, stem + "_output.jsonl"),
            stderr_path=os.path.join(self.run_dir, stem + "_stderr.log"),
            events_path=os.path.join(self.run_dir, stem + "_http_events.jsonl"))
        env = os.environ.copy()
        env.update(self.spec.extra_env)
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "BENCH_EVENTS_FILE": result.events_path,
                    "BENCH_WORKER_ID": str(worker_id), "BENCH_UNIT": unit.unit_id})
        popen_kwargs: Dict[str, Any] = {}
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True

        limits = unit.limits if self.enforce_limits else {}
        if any(v is not None for v in limits.values()) and len(unit.roots) == 1:
            env["BENCH_LIMIT_RULE"] = json.dumps({"roots": unit.roots, "limits": limits, "selected": unit.selected})
        result.tallies = {s: StreamTally(limit=limits.get(s)) for s in unit.selected}
        scanner = StderrScanner()
        timed_out = False
        result.start_ts = time.time()
        try:
            proc = subprocess.Popen(self._command(unit), cwd=self.spec.repo_path, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs)
        except OSError as exc:
            result.end_ts = time.time()
            result.status = FAILED
            result.note = "could not start process: %s" % exc
            return result
        with self._active_lock:
            self.active.add(proc)

        counters: Dict[str, Any] = {"invalid": 0, "schema": 0, "state": 0, "stopped": False, "limit_hit_ts": None}
        out_thread = threading.Thread(target=self._pump_stdout, args=(proc, unit, result, counters), daemon=True)
        err_thread = threading.Thread(target=self._pump_stderr, args=(proc, result.stderr_path, scanner), daemon=True)
        out_thread.start()
        err_thread.start()
        try:
            proc.wait(timeout=max(1.0, timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(proc)
            proc.wait()
        finally:
            result.end_ts = time.time()
            with self._active_lock:
                self.active.discard(proc)
        out_thread.join(timeout=30)
        err_thread.join(timeout=30)

        result.exit_code = proc.returncode
        result.invalid_lines = counters["invalid"]
        result.schema_messages = counters["schema"]
        result.state_messages = counters["state"]
        result.throttle_log_mentions = scanner.throttle_mentions
        result.stopped_by_limit = bool(counters["stopped"])
        result.limit_hit_ts = counters["limit_hit_ts"]
        stopped = result.stopped_by_limit
        if stopped:
            result.status = COMPLETED_WITH_LIMIT
            result.note = "stopped at the record limit"
        elif timed_out:
            result.status = TIMED_OUT
        elif proc.returncode == 0:
            result.status = COMPLETED_WITH_LIMIT if any(t.limit_reached for t in result.tallies.values()) else COMPLETED
        else:
            result.status = FAILED

        events = self._read_events(result.events_path)
        result.http = summarize_events(events)
        stop_events = [e.get("ts") for e in events if e.get("ev") == "limit_stop" and e.get("ts")]
        if stop_events:
            result.limit_hit_ts = min([result.limit_hit_ts] + stop_events) if result.limit_hit_ts else min(stop_events)
        if result.limit_hit_ts is not None:
            result.requests_after_limit = sum(
                1 for e in events if e.get("ev") == "http" and (e.get("ts") or 0) > result.limit_hit_ts)
        self._attribute_streams(unit, result, events)
        succeeded = result.status in OK_STATUSES
        result.errors = build_unit_errors(
            tap=self.spec.name, unit_id=unit.unit_id, worker_id=worker_id, worker_count=self.worker_count,
            events=events, succeeded=succeeded, retry_count=result.http.retries, scanner=scanner,
            exit_code=proc.returncode, timed_out=timed_out and not stopped,
            scrub=self.scrubber.scrub, end_ts=result.end_ts)
        if result.invalid_lines:
            result.note = (result.note + "; " if result.note else "") + "%d non-Singer line(s) on stdout" % result.invalid_lines
        return result

    @staticmethod
    def _attribute_streams(unit: WorkUnit, result: UnitResult, events: List[Dict[str, Any]]) -> None:
        """For batched units, derive per-stream time windows from SCHEMA/RECORD arrival and split HTTP events."""
        if len(unit.roots) < 2:
            return
        starts: Dict[str, float] = {}
        for stream in unit.selected:
            tally = result.tallies.get(stream)
            marker = (tally.first_schema_ts or tally.first_record_ts) if tally else None
            if marker is not None:
                starts[stream] = marker
        order = sorted(starts, key=starts.get)  # type: ignore[arg-type]
        for index, stream in enumerate(order):
            end = starts[order[index + 1]] if index + 1 < len(order) else (result.end_ts or starts[stream])
            result.stream_intervals[stream] = (starts[stream], end)
        result.metrics_scope = "time_attributed"
        for stream, (begin, end) in result.stream_intervals.items():
            subset = [e for e in events if e.get("ev") in ("http", "retry", "giveup") and begin <= (e.get("ts") or 0) < end]
            summary = summarize_events(subset)
            summary.measured = result.http.measured
            result.stream_http[stream] = summary

    def _pump_stdout(self, proc: subprocess.Popen, unit: WorkUnit, result: UnitResult, counters: Dict[str, Any]) -> None:
        tallies = result.tallies
        roots = set(unit.roots)
        single_root = len(unit.roots) == 1
        selected = set(unit.selected)

        def should_stop(stream: str) -> bool:
            if not single_root:
                return False
            if stream in roots:
                return True
            return all(t.limit is not None and t.limit_reached for s, t in tallies.items() if s in selected)

        with open(result.output_path, "wb") as out:
            for line in proc.stdout:  # type: ignore[union-attr]
                if not line.strip():
                    out.write(line)
                    continue
                match = _MESSAGE_PREFIX.match(line)
                if match:
                    kind = match.group(1).decode("ascii", "replace")
                    stream = match.group(2).decode("utf-8", "replace") if match.group(2) else None
                else:
                    try:
                        parsed = json.loads(line)
                        kind, stream = parsed.get("type"), parsed.get("stream")
                    except (ValueError, AttributeError):
                        kind = None
                    if not isinstance(kind, str):
                        out.write(line)
                        counters["invalid"] += 1
                        continue
                now = time.time()
                if kind == "RECORD":
                    key = stream or "?"
                    tally = tallies.get(key)
                    if tally is None:
                        tally = tallies[key] = StreamTally()
                    if tally.limit is not None and tally.records >= tally.limit:
                        tally.dropped += 1          # beyond this stream's limit: never written
                        continue
                    tally.records += 1
                    tally.first_record_ts = tally.first_record_ts or now
                    tally.last_record_ts = now
                    out.write(line)
                    if tally.limit is not None and tally.records >= tally.limit:
                        tally.limit_reached = True
                        if should_stop(key):
                            counters["stopped"] = True
                            counters["limit_hit_ts"] = now
                            kill_process_tree(proc)
                            break
                    continue
                if kind == "SCHEMA":
                    counters["schema"] += 1
                    tally = tallies.get(stream or "?")
                    if tally is not None and tally.first_schema_ts is None:
                        tally.first_schema_ts = now
                elif kind == "STATE":
                    counters["state"] += 1
                out.write(line)

    def _pump_stderr(self, proc: subprocess.Popen, path: str, scanner: StderrScanner) -> None:
        with open(path, "w", encoding="utf-8") as out:
            for raw in proc.stderr:  # type: ignore[union-attr]
                text = raw.decode("utf-8", "replace")
                clean = self.scrubber.scrub(text)
                scanner.feed(clean)
                out.write(clean if clean.endswith("\n") else clean + "\n")

    @staticmethod
    def _read_events(path: str) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        if not os.path.isfile(path):
            return events
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
        return events


class StreamScheduler:
    """Distributes work units over N worker slots and records who ran what, and when."""

    def __init__(self, executor: UnitExecutor, worker_count: int, timeout_seconds: float,
                 stream_timeout_seconds: float, continue_on_failure: bool, logger: logging.Logger) -> None:
        self.executor = executor
        self.worker_count = worker_count
        self.timeout_seconds = timeout_seconds
        self.stream_timeout_seconds = stream_timeout_seconds
        self.continue_on_failure = continue_on_failure
        self.logger = logger

    def run(self, run: RunResult, units: List[WorkUnit]) -> RunResult:
        queue: Deque[WorkUnit] = deque(units)
        queue_lock = threading.Lock()
        stop = threading.Event()
        results: Dict[int, UnitResult] = {}
        worker_log: Dict[int, List[UnitResult]] = {}
        thread_count = min(self.worker_count, len(units)) or 0
        run.worker_threads_started = thread_count
        run.started_ts = time.time()
        deadline = run.started_ts + self.timeout_seconds

        self.logger.info("[SCHEDULER] start tap=%s configured_workers=%d worker_threads=%d units=%d run_timeout=%ss",
                         run.tap_name, self.worker_count, thread_count, len(units), self.timeout_seconds)
        for unit in units:
            self.logger.info("[SCHEDULER] queued unit=%s index=%d streams=%s limits=%s", unit.unit_id, unit.index,
                             ",".join(unit.selected), {k: v for k, v in unit.limits.items() if v is not None} or "none")

        def worker_loop(worker_id: int) -> None:
            worker_log[worker_id] = []
            while not stop.is_set():
                with queue_lock:
                    if not queue:
                        break
                    unit = queue.popleft()
                remaining = deadline - time.time()
                if remaining <= 0:
                    results[unit.index] = UnitResult(
                        tap_name=run.tap_name, worker_count=self.worker_count, unit_id=unit.unit_id,
                        streams=list(unit.selected), roots=list(unit.roots), worker_id=worker_id, status=SKIPPED,
                        note="run timeout budget exhausted before start")
                    self.logger.warning("[WORKER-%d] SKIPPED unit=%s reason=run_timeout_budget_exhausted",
                                        worker_id, unit.unit_id)
                    run.run_timed_out = True
                    continue
                self.logger.info("[WORKER-%d] START unit=%s streams=%s", worker_id, unit.unit_id, ",".join(unit.selected))
                unit_result = self.executor.execute(unit, worker_id, min(self.stream_timeout_seconds, remaining))
                if unit_result.status == TIMED_OUT and time.time() >= deadline - 1:
                    run.run_timed_out = True
                results[unit.index] = unit_result
                worker_log[worker_id].append(unit_result)
                self._log_unit_end(worker_id, unit_result)
                if unit_result.status not in OK_STATUSES and not self.continue_on_failure:
                    self.logger.error("[SCHEDULER] stopping: continue_on_stream_failure=false and unit=%s failed",
                                      unit.unit_id)
                    run.stopped_early = True
                    stop.set()

        threads = [threading.Thread(target=worker_loop, args=(wid,), name="worker-%d" % wid, daemon=True)
                   for wid in range(1, thread_count + 1)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                while thread.is_alive():
                    thread.join(timeout=0.5)
        except KeyboardInterrupt:
            self.logger.error("[SCHEDULER] interrupted - terminating running streams")
            stop.set()
            self.executor.terminate_all()
            for thread in threads:
                thread.join(timeout=10)
            run.stopped_early = True
            raise
        finally:
            run.ended_ts = time.time()

        # Anything still queued (stop requested) was never attempted.
        for unit in queue:
            results[unit.index] = UnitResult(
                tap_name=run.tap_name, worker_count=self.worker_count, unit_id=unit.unit_id,
                streams=list(unit.selected), roots=list(unit.roots), status=SKIPPED,
                note="not started: scheduling stopped after a failure")
        run.units = [results[index] for index in sorted(results)]   # catalog order, whatever order they were scheduled in
        for unit_result in run.units:
            run.errors.extend(unit_result.errors)
        self._log_summary(run, worker_log)
        return run

    def _log_unit_end(self, worker_id: int, result: UnitResult) -> None:
        http = result.http
        level = logging.INFO if result.status in OK_STATUSES else logging.ERROR
        counts = ",".join("%s=%d%s" % (s, t.records, "(limit)" if t.limit_reached else "")
                          for s, t in result.tallies.items() if t.records or t.limit_reached) or "none"
        self.logger.log(
            level, "[WORKER-%d] END   unit=%s status=%s exit=%s start=%s end=%s duration=%.2fs records=%d "
                   "requests=%d requests_after_limit=%d http429=%d throttle_signals=%d retries=%d startup=%.2fs "
                   "stream_records=%s", worker_id, result.unit_id, result.status, result.exit_code,
            utc_iso(result.start_ts), utc_iso(result.end_ts), result.duration or 0.0, result.records, http.requests,
            result.requests_after_limit, http.http_429, http.throttle_signals, http.retries,
            result.startup_seconds or 0.0, counts)
        for error in result.errors:
            level = logging.WARNING if error.stream_eventually_succeeded else logging.ERROR
            if error.category in ("RATE_LIMIT", "THROTTLING"):
                tag = "[THROTTLE]"
            elif error.source == "process":
                tag = "[STREAM-FAILURE]"
            else:
                tag = "[HTTP-ERROR]"
            self.logger.log(level, "%s worker=%d unit=%s ts=%s category=%s status=%s retries=%d eventually_succeeded=%s "
                                   "url=%s message=%s", tag, worker_id, result.unit_id, error.timestamp,
                            error.category, error.http_status, error.retry_count, error.stream_eventually_succeeded,
                            error.url, error.message.replace("\n", " "))

    def _log_summary(self, run: RunResult, worker_log: Dict[int, List[UnitResult]]) -> None:
        wall = run.wall_seconds or 0.0
        self.logger.info("[TIMELINE] Worker | Stream/Unit | Start | End | Duration | Status")
        for unit in sorted((u for u in run.units if u.start_ts), key=lambda u: u.start_ts):  # type: ignore[arg-type]
            self.logger.info("[TIMELINE] Worker %s | %s | %s | %s | %.2fs | %s", unit.worker_id, unit.unit_id,
                             utc_iso(unit.start_ts)[11:23], utc_iso(unit.end_ts)[11:23], unit.duration or 0.0, unit.status)
        for worker_id, units in sorted(worker_log.items()):
            busy = sum(u.duration or 0.0 for u in units)
            failed = sum(1 for u in units if u.status not in OK_STATUSES)
            self.logger.info("[WORKER-%d] FINAL units_run=%d failed=%d busy=%.2fs idle=%.2fs", worker_id, len(units),
                             failed, busy, max(0.0, wall - busy))
        intervals = [(u.start_ts, u.end_ts) for u in run.units if u.start_ts and u.end_ts]
        stats = concurrency_stats(intervals, (run.started_ts, run.ended_ts))  # type: ignore[arg-type]
        self.logger.info("[CONCURRENCY] configured_workers=%d worker_threads=%d max_observed=%d avg_observed=%.2f "
                         "total_stream_time=%.2fs wall_clock=%.2fs histogram_seconds_by_concurrency=%s",
                         self.worker_count, run.worker_threads_started, stats["max"], stats["avg"],
                         stats["total_busy"], wall, stats["histogram"])
        if stats["max"] > self.worker_count:
            self.logger.error("[CONCURRENCY] INVALID: observed concurrency exceeds configured workers")
        elif run.worker_threads_started and stats["max"] < min(self.worker_count, len(run.units)):
            self.logger.warning("[CONCURRENCY] observed concurrency (%d) below configured worker count (%d)",
                                stats["max"], min(self.worker_count, len(run.units)))
