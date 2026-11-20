"""Data model and metric computation for benchmark runs."""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from error_analyzer import (ErrorRecord, RATE_LIMIT_CATEGORIES, is_near_limit, is_throttle_signal, EXTRACTION_ERROR,
                            UNKNOWN)

# Stream / work-unit / run statuses
COMPLETED = "COMPLETED"
COMPLETED_WITH_LIMIT = "COMPLETED_WITH_LIMIT"
FAILED = "FAILED"
TIMED_OUT = "TIMED_OUT"
SKIPPED = "SKIPPED"
PARTIAL = "PARTIAL"
OK_STATUSES = (COMPLETED, COMPLETED_WITH_LIMIT)

# Backwards-friendly aliases used by the executor
UNIT_COMPLETED, UNIT_LIMITED, UNIT_FAILED, UNIT_TIMED_OUT, UNIT_SKIPPED = (
    COMPLETED, COMPLETED_WITH_LIMIT, FAILED, TIMED_OUT, SKIPPED)
UNIT_OK = OK_STATUSES


@dataclass
class HttpSummary:
    requests: int = 0
    status_counts: Dict[str, int] = field(default_factory=dict)
    http_429: int = 0
    http_4xx: int = 0
    http_5xx: int = 0
    timeouts: int = 0
    network_errors: int = 0
    throttle_signals: int = 0
    near_limit: int = 0
    min_remaining: Optional[int] = None
    retries: int = 0
    giveups: int = 0
    first_request_ts: Optional[float] = None
    last_request_ts: Optional[float] = None
    process_start_ts: Optional[float] = None
    rate_limit_headers: Dict[str, str] = field(default_factory=dict)
    request_seconds: float = 0.0
    measured: bool = False
    unavailable: bool = False


def summarize_events(events: List[Dict[str, Any]]) -> HttpSummary:
    summary = HttpSummary()
    statuses: Counter = Counter()
    for event in events:
        kind = event.get("ev")
        if kind == "process_start":
            summary.measured = True
            summary.process_start_ts = event.get("ts")
        elif kind == "http_unavailable":
            summary.unavailable = True
        elif kind == "retry":
            summary.retries += 1
        elif kind == "giveup":
            summary.giveups += 1
        elif kind == "http":
            summary.measured = True
            summary.requests += 1
            ts = event.get("ts")
            if ts is not None:
                summary.first_request_ts = ts if summary.first_request_ts is None else min(summary.first_request_ts, ts)
                end = ts + (event.get("dur") or 0)
                summary.last_request_ts = end if summary.last_request_ts is None else max(summary.last_request_ts, end)
            summary.request_seconds += event.get("dur") or 0
            status = event.get("status")
            exc = event.get("exc")
            if exc:
                statuses["EXC:" + exc] += 1
                if "timeout" in exc.lower() or "timed out" in exc.lower():
                    summary.timeouts += 1
                else:
                    summary.network_errors += 1
            else:
                statuses[str(status)] += 1
                if status == 429:
                    summary.http_429 += 1
                if status is not None and 400 <= status < 500:
                    summary.http_4xx += 1
                if status is not None and status >= 500:
                    summary.http_5xx += 1
                if status in (408, 504):
                    summary.timeouts += 1
            if is_throttle_signal(event):
                summary.throttle_signals += 1
            if is_near_limit(event):
                summary.near_limit += 1
            for name, value in (event.get("rate_headers") or {}).items():
                if name.endswith("remaining") and str(value).strip().isdigit():
                    remaining = int(str(value).strip())
                    summary.min_remaining = remaining if summary.min_remaining is None else min(summary.min_remaining, remaining)
                # Only static limit/policy headers describe the limit; 'ratelimit' carries live remaining/reset.
                if "limit" in name and name != "ratelimit" and not any(k in name for k in ("remaining", "reset", "near")):
                    summary.rate_limit_headers.setdefault(name, value)
    summary.status_counts = dict(sorted(statuses.items()))
    if summary.unavailable:
        summary.measured = False     # requests was not importable: HTTP figures would be invented, so mark unmeasured
    return summary


@dataclass
class StreamTally:
    """Per-stream accounting while a work unit's stdout is streaming."""
    limit: Optional[int] = None
    records: int = 0
    dropped: int = 0
    limit_reached: bool = False
    first_schema_ts: Optional[float] = None
    first_record_ts: Optional[float] = None
    last_record_ts: Optional[float] = None


@dataclass
class UnitResult:
    tap_name: str
    worker_count: int
    unit_id: str
    streams: List[str]
    roots: List[str] = field(default_factory=list)
    worker_id: Optional[int] = None
    status: str = SKIPPED
    exit_code: Optional[int] = None
    start_ts: Optional[float] = None
    end_ts: Optional[float] = None
    tallies: Dict[str, StreamTally] = field(default_factory=dict)
    invalid_lines: int = 0
    schema_messages: int = 0
    state_messages: int = 0
    http: HttpSummary = field(default_factory=HttpSummary)
    stream_http: Dict[str, HttpSummary] = field(default_factory=dict)
    stream_intervals: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    metrics_scope: str = "unit"
    stopped_by_limit: bool = False
    limit_hit_ts: Optional[float] = None
    requests_after_limit: int = 0
    errors: List[ErrorRecord] = field(default_factory=list)
    throttle_log_mentions: int = 0
    stderr_path: str = ""
    output_path: str = ""
    events_path: str = ""
    note: str = ""

    @property
    def duration(self) -> Optional[float]:
        if self.start_ts is None or self.end_ts is None:
            return None
        return max(0.0, self.end_ts - self.start_ts)

    @property
    def records_by_stream(self) -> Dict[str, int]:
        return {s: t.records for s, t in self.tallies.items() if t.records}

    @property
    def records(self) -> int:
        return sum(t.records for t in self.tallies.values())

    @property
    def startup_seconds(self) -> Optional[float]:
        """Time from spawn to the first HTTP request: per-process overhead (imports + authentication)."""
        if self.start_ts is None or self.http.first_request_ts is None:
            return None
        return max(0.0, self.http.first_request_ts - self.start_ts)

    def stream_status(self, stream: str) -> str:
        """Status of one stream inside this unit."""
        if self.status == SKIPPED:
            return SKIPPED
        if self.status in (FAILED, TIMED_OUT):
            return self.status
        tally = self.tallies.get(stream)
        if (tally is not None and tally.limit_reached) or self.stopped_by_limit:
            return COMPLETED_WITH_LIMIT
        return COMPLETED

    def interval(self, stream: str) -> Optional[Tuple[float, float]]:
        if stream in self.stream_intervals:
            return self.stream_intervals[stream]
        if self.start_ts is not None and self.end_ts is not None:
            return (self.start_ts, self.end_ts)
        return None


@dataclass
class RunResult:
    tap_name: str
    worker_count: int
    mode: str = "stream_pool"
    started_ts: Optional[float] = None
    ended_ts: Optional[float] = None
    units: List[UnitResult] = field(default_factory=list)
    selected_streams: List[str] = field(default_factory=list)
    excluded_streams: List[str] = field(default_factory=list)
    filtered_streams: List[str] = field(default_factory=list)
    errors: List[ErrorRecord] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    run_timed_out: bool = False
    stopped_early: bool = False
    log_path: str = ""
    merged_output_path: str = ""
    merge_consistent: Optional[bool] = None
    configured_rate_limit: str = "UNKNOWN"
    worker_threads_started: int = 0
    record_limit_text: str = "unlimited"
    environment_failure: str = ""        # set when a work unit could not even start the tap (missing package, ...)
    stop_reason: str = ""                # why scheduling stopped early (empty when every unit was attempted)

    @property
    def wall_seconds(self) -> Optional[float]:
        if self.started_ts is None or self.ended_ts is None:
            return None
        return max(0.0, self.ended_ts - self.started_ts)


def concurrency_stats(intervals: List[Tuple[float, float]], window: Optional[Tuple[float, float]] = None) -> Dict[str, Any]:
    """Sweep-line concurrency over (start, end) intervals.

    ``avg`` is time-weighted over the wall-clock window (default: first start .. last end), so it
    measures how many streams were genuinely in flight on average, not how many threads existed.
    """
    intervals = [(s, e) for s, e in intervals if s is not None and e is not None and e >= s]
    if not intervals:
        return {"max": 0, "avg": 0.0, "total_busy": 0.0, "histogram": {}, "window": 0.0}
    points: List[Tuple[float, int]] = []
    for start, end in intervals:
        points.append((start, 1))
        points.append((end, -1))
    # Ends sort before starts at equal timestamps so back-to-back streams do not count as overlap.
    points.sort(key=lambda p: (p[0], p[1]))
    win_start, win_end = window if window else (min(s for s, _ in intervals), max(e for _, e in intervals))
    current = 0
    peak = 0
    histogram: Dict[int, float] = {}
    previous = points[0][0]
    for ts, delta in points:
        if ts > previous:
            histogram[current] = histogram.get(current, 0.0) + (ts - previous)
        current += delta
        peak = max(peak, current)
        previous = ts
    total_busy = sum(e - s for s, e in intervals)
    span = max(win_end - win_start, 1e-9)
    return {"max": peak, "avg": total_busy / span, "total_busy": total_busy,
            "histogram": {k: round(v, 3) for k, v in sorted(histogram.items())}, "window": span}


def stream_outcomes(run: RunResult) -> Dict[str, str]:
    """Per selected stream: COMPLETED / COMPLETED_WITH_LIMIT / FAILED / TIMED_OUT / SKIPPED."""
    outcomes: Dict[str, str] = {}
    for unit in run.units:
        for stream in unit.streams:
            outcomes[stream] = unit.stream_status(stream)
    return outcomes


@dataclass
class StreamResult:
    """One row of the stream-level report."""
    tap_name: str
    stream_name: str
    worker_count: int
    unit_id: str
    worker_id: Optional[int]
    start_ts: Optional[float]
    end_ts: Optional[float]
    duration: Optional[float]
    records: int
    record_limit: Optional[int]
    status: str
    http_429: Optional[int]
    retries: Optional[int]
    throttling: Optional[int]
    error_category: str
    error_message: str
    overlapped: str
    metrics_scope: str
    timing_basis: str
    api_requests_after_limit: int


def build_stream_results(run: RunResult) -> List[StreamResult]:
    """Flatten a run into one StreamResult per selected stream (with overlap detection)."""
    intervals: List[Tuple[str, str, Tuple[float, float]]] = []     # (unit_id, stream, interval)
    for unit in run.units:
        for stream in unit.streams:
            span = unit.interval(stream)
            if span:
                intervals.append((unit.unit_id, stream, span))
    results: List[StreamResult] = []
    for unit in run.units:
        failure = next((e for e in reversed(unit.errors) if e.source == "process"), None)
        for stream in unit.streams:
            span = unit.interval(stream)
            overlapped = "N/A"
            if span:
                overlapped = "YES" if any(
                    uid != unit.unit_id and other[0] < span[1] and span[0] < other[1]
                    for uid, _s, other in intervals) else "NO"
            http = unit.stream_http.get(stream) or unit.http
            tally = unit.tallies.get(stream)
            measured = http.measured
            results.append(StreamResult(
                tap_name=run.tap_name, stream_name=stream, worker_count=run.worker_count, unit_id=unit.unit_id,
                worker_id=unit.worker_id, start_ts=span[0] if span else None, end_ts=span[1] if span else None,
                duration=(span[1] - span[0]) if span else None, records=tally.records if tally else 0,
                record_limit=tally.limit if tally else None,
                status=unit.stream_status(stream),
                http_429=http.http_429 if measured else None, retries=http.retries if measured else None,
                throttling=http.throttle_signals if measured else None,
                error_category=failure.category if failure and unit.stream_status(stream) in (FAILED, TIMED_OUT) else "",
                error_message=failure.message if failure and unit.stream_status(stream) in (FAILED, TIMED_OUT) else "",
                overlapped=overlapped, metrics_scope=unit.metrics_scope,
                timing_basis="unit" if stream not in unit.stream_intervals else "schema_markers",
                api_requests_after_limit=unit.requests_after_limit))
    return results


def format_status_counts(counts: Dict[str, int]) -> str:
    return ";".join("%s:%d" % (k, v) for k, v in counts.items())


def run_metrics(run: RunResult) -> Dict[str, Any]:
    """Flatten a RunResult into the measurable metrics (None == not measurable)."""
    units = run.units
    ran = [u for u in units if u.status != SKIPPED]
    outcomes = stream_outcomes(run)
    successful = sum(1 for v in outcomes.values() if v in OK_STATUSES)
    limited = sum(1 for v in outcomes.values() if v == COMPLETED_WITH_LIMIT)
    failed = sum(1 for v in outcomes.values() if v in (FAILED, TIMED_OUT))
    not_attempted = sum(1 for v in outcomes.values() if v == SKIPPED)
    attempted = successful + failed
    wall = run.wall_seconds

    http = [u.http for u in ran]
    http_measured = bool(http) and all(h.measured for h in http)

    def total(attr: str) -> Optional[int]:
        return sum(getattr(h, attr) for h in http) if http_measured else None

    intervals = [(u.start_ts, u.end_ts) for u in ran if u.start_ts is not None and u.end_ts is not None]
    window = (run.started_ts, run.ended_ts) if run.started_ts and run.ended_ts else None
    conc = concurrency_stats(intervals, window)
    http_intervals = [(h.first_request_ts, h.last_request_ts) for h in http
                      if h.first_request_ts is not None and h.last_request_ts is not None]
    http_conc = concurrency_stats(http_intervals, window)

    records = sum(u.records for u in ran) if ran else None
    startups = [u.startup_seconds for u in ran if u.startup_seconds is not None]
    durations = [(u.duration, u.unit_id) for u in ran if u.duration is not None]
    longest = max(durations) if durations else (None, "")

    status_counts: Counter = Counter()
    for h in http:
        status_counts.update(h.status_counts)
    rate_headers: Dict[str, str] = {}
    for h in http:
        for name, value in h.rate_limit_headers.items():
            rate_headers.setdefault(name, value)

    extraction_errors = sum(1 for e in run.errors if e.source == "process" and e.category in (EXTRACTION_ERROR, UNKNOWN))
    timed_out_units = sum(1 for u in ran if u.status == TIMED_OUT)

    # Rate limiting: HTTP-level signals (429 / Retry-After on errors / exhausted quota header), rate-limit wording in
    # the tap's logs, or a stream that failed because of it. UNKNOWN when HTTP could not be measured and nothing else shows it.
    log_mentions = sum(u.throttle_log_mentions for u in ran)
    process_rate_errors = sum(1 for e in run.errors if e.source == "process" and e.category in RATE_LIMIT_CATEGORIES)
    http_signals = total("throttle_signals")
    rate_signals = (http_signals or 0) + log_mentions + process_rate_errors
    if not ran:
        rate_limit_encountered = "N/A"
    elif rate_signals > 0:
        rate_limit_encountered = "YES"
    else:
        rate_limit_encountered = "NO" if http_measured else "UNKNOWN"

    if not units or not ran:
        status = SKIPPED if not units else FAILED
    elif failed == 0 and not_attempted == 0:
        status = COMPLETED_WITH_LIMIT if limited else COMPLETED
    elif successful == 0:
        status = TIMED_OUT if (timed_out_units and timed_out_units == len(ran)) or (run.run_timed_out and not successful) \
            else FAILED
    else:
        status = PARTIAL

    cap = run.worker_count
    if run.mode != "stream_pool":
        concurrency_verdict = "NOT_APPLICABLE"
    elif not ran:
        concurrency_verdict = "NOT_MEASURED"
    elif conc["max"] > cap:
        concurrency_verdict = "INVALID_EXCEEDS_WORKERS"
    elif cap == 1:
        concurrency_verdict = "SEQUENTIAL_CONFIRMED" if conc["max"] <= 1 else "INVALID"
    elif conc["max"] >= min(cap, len(units)):
        concurrency_verdict = "FULL"
    elif conc["max"] > 1:
        concurrency_verdict = "PARTIAL_%d_OF_%d" % (conc["max"], min(cap, len(units)))
    else:
        concurrency_verdict = "NO_OVERLAP_OBSERVED"

    limited_names = sorted(s for s, v in outcomes.items() if v == COMPLETED_WITH_LIMIT)
    extraction_status = {COMPLETED: "SUCCESS", COMPLETED_WITH_LIMIT: "SUCCESS", PARTIAL: "PARTIAL",
                         FAILED: "FAILURE", TIMED_OUT: "FAILURE", SKIPPED: "SKIPPED"}.get(status, "FAILURE")
    return {
        "benchmark_status": status,
        "extraction_status": extraction_status,
        "rate_limit_encountered": rate_limit_encountered,
        "rate_limit_signals": rate_signals if ran else None,
        "total_streams": len(run.selected_streams),
        "scheduled_streams": len(outcomes),
        "successful_streams": successful,
        "limited_streams": limited,
        "limited_stream_names": ";".join(limited_names),
        "failed_streams": failed,
        "not_attempted_streams": not_attempted,
        "skipped_streams": len(run.excluded_streams),
        "attempted_streams": attempted,
        "success_rate_percentage": round(100.0 * successful / attempted, 2) if attempted else None,
        "execution_duration_seconds": round(wall, 3) if wall is not None else None,
        "total_records": records,
        "record_limit": run.record_limit_text,
        "throughput_records_per_second": round(records / wall, 3) if (records is not None and wall) else None,
        "api_request_count": total("requests"),
        "http_429_count": total("http_429"),
        "throttling_count": total("throttle_signals"),
        "retry_count": total("retries"),
        "giveup_count": total("giveups"),
        "http_4xx_count": total("http_4xx"),
        "http_5xx_count": total("http_5xx"),
        "timeout_count": ((total("timeouts") or 0) + timed_out_units) if http_measured else timed_out_units,
        "network_error_count": total("network_errors"),
        "extraction_error_count": extraction_errors,
        "throttle_log_mentions": sum(u.throttle_log_mentions for u in ran),
        "http_status_counts": format_status_counts(dict(sorted(status_counts.items()))) if http_measured else None,
        "detected_rate_limit_headers": " | ".join("%s: %s" % kv for kv in sorted(rate_headers.items())),
        "rate_limit_near_count": total("near_limit"),
        "rate_limit_min_remaining": min([h.min_remaining for h in http if h.min_remaining is not None], default=None),
        "max_observed_concurrency": conc["max"],
        "avg_observed_concurrency": round(conc["avg"], 3),
        "total_stream_execution_seconds": round(conc["total_busy"], 3),
        "max_concurrent_http_active_streams": http_conc["max"],
        "concurrency_verdict": concurrency_verdict,
        "concurrency_histogram": conc["histogram"],
        "scheduling_units": len(units),
        "effective_workers": min(run.worker_count, len(units)) if (units and run.mode == "stream_pool") else None,
        "longest_unit_seconds": round(longest[0], 3) if longest[0] is not None else None,
        "longest_unit_name": longest[1],
        "avg_process_startup_seconds": round(statistics.mean(startups), 3) if startups else None,
        "api_requests_after_limit": sum(u.requests_after_limit for u in ran),
        "error_categories": dict(Counter(e.category for e in run.errors if e.source == "process"
                                         or not e.stream_eventually_succeeded)),
    }


def compare_to_baseline(current: Dict[str, Any], baseline: Optional[Dict[str, Any]],
                        worker_count: int) -> Dict[str, Any]:
    """Improvement, speedup and efficiency against the 1-worker baseline (None if not computable)."""
    result: Dict[str, Any] = {"sequential_duration_seconds": None, "improvement_percentage": None,
                              "speedup": None, "parallel_efficiency_percentage": None}
    if not baseline:
        return result
    seq = baseline.get("execution_duration_seconds")
    cur = current.get("execution_duration_seconds")
    result["sequential_duration_seconds"] = seq
    if seq and cur:
        result["improvement_percentage"] = round((seq - cur) / seq * 100.0, 2)
        result["speedup"] = round(seq / cur, 3)
        result["parallel_efficiency_percentage"] = round(seq / cur / worker_count * 100.0, 2)
    return result


def _totals(run: RunResult) -> Dict[str, int]:
    merged: Dict[str, int] = {}
    for unit in run.units:
        for stream in unit.streams:
            merged[stream] = merged.get(stream, 0) + (unit.tallies[stream].records if stream in unit.tallies else 0)
    return merged


def compare_records(current: RunResult, baseline: Optional[RunResult], tolerance_pct: float = 0.0) -> str:
    """MATCH if every stream's record count equals the baseline's (within tolerance); else list differences."""
    if baseline is None:
        return "N/A"
    cur, base = _totals(current), _totals(baseline)
    diffs = []
    for stream in sorted(set(cur) | set(base)):
        a, b = cur.get(stream, 0), base.get(stream, 0)
        if a != b and abs(a - b) > max(2, b * tolerance_pct / 100.0):
            diffs.append("%s:%+d" % (stream, a - b))
    if not diffs:
        return "MATCH"
    shown = ";".join(diffs[:5]) + (";+%d more" % (len(diffs) - 5) if len(diffs) > 5 else "")
    return "DIFF(" + shown + ")"
