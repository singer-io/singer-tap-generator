"""CSV, TXT and JSON report generation (deterministic: same input => same output)."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from config import utc_iso
from decision import Decision

CSV_COLUMNS = [
    "tap_name", "benchmark_status", "can_parallel_extraction_be_done", "recommended_worker_count",
    "worker_count", "worker_count_safe", "total_streams", "successful_streams", "failed_streams", "limited_streams",
    "total_records", "record_limit", "sequential_duration_seconds", "execution_duration_seconds",
    "improvement_percentage", "speedup", "throughput_records_per_second", "max_observed_concurrency",
    "http_429_count", "throttling_count", "retry_count", "http_4xx_count", "http_5xx_count", "timeout_count",
    "extraction_error_count", "failed_stream_names", "error_summary", "start_time", "end_time",
    # additional columns
    "decision_scope", "decision_basis", "decision_summary", "worker_safety_notes", "execution_mode", "run_id",
    "rate_limit_info", "skipped_streams", "attempted_streams", "not_attempted_streams", "success_rate_percentage",
    "limited_stream_names", "scheduling_units", "avg_observed_concurrency", "concurrency_verdict",
    "total_stream_execution_seconds", "parallel_efficiency_percentage", "api_request_count",
    "api_requests_after_limit", "network_error_count", "throttle_log_mentions", "http_status_counts",
    "detected_rate_limit_headers", "rate_limit_near_count", "rate_limit_min_remaining", "records_match_baseline", "longest_unit_seconds",
    "longest_unit_name", "avg_process_startup_seconds", "skipped_stream_names", "notes", "log_file", "output_file",
]

STREAM_COLUMNS = ["tap_name", "stream_name", "worker_count", "worker_id", "start_time", "end_time",
                  "duration_seconds", "records_extracted", "record_limit", "stream_status", "http_429_count",
                  "retry_count", "throttling_count", "error_category", "error_message", "overlapped_with_other_stream",
                  "work_unit", "metrics_scope", "timing_basis", "api_requests_after_limit"]

ERROR_COLUMNS = ["tap_name", "stream_name", "worker_id", "worker_count", "timestamp", "category", "http_status",
                 "message", "retry_count", "stream_eventually_succeeded", "source", "url"]

DECISION_SCOPE = "TAP_LEVEL: same value on every row of this tap (derived from all worker counts); " \
                 "worker_count_safe is per row"


@dataclass
class TapSection:
    tap_name: str
    status: str = "EVALUATED"                    # EVALUATED | SKIPPED | SKIPPED_INVALID_CONFIG | SKIPPED_ENVIRONMENT
    skip_reasons: List[str] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    selected_streams: int = 0
    excluded_streams: int = 0
    excluded_names: List[str] = field(default_factory=list)
    record_limit_text: str = "unlimited"
    notes: List[str] = field(default_factory=list)
    stream_outcomes: Dict[int, Dict[str, str]] = field(default_factory=dict)
    stream_results: List[Any] = field(default_factory=list)
    errors: List[Any] = field(default_factory=list)
    decision: Optional[Decision] = None
    rate_limit_info: str = "UNKNOWN"

    @property
    def skipped(self) -> bool:
        return self.status.startswith("SKIPPED")


def skipped_row(tap_name: str, status: str, reasons: Sequence[str], run_id: str, rate_limit: str = "UNKNOWN") -> Dict[str, Any]:
    now = utc_iso()
    return {"tap_name": tap_name, "benchmark_status": status, "can_parallel_extraction_be_done": "NO",
            "recommended_worker_count": 0, "decision_scope": DECISION_SCOPE, "decision_basis": "NOT_EVALUATED_SKIPPED",
            "decision_summary": "not benchmarked: " + "; ".join(reasons), "rate_limit_info": rate_limit,
            "error_summary": "; ".join(reasons), "start_time": now, "end_time": now, "execution_mode": "none",
            "run_id": run_id, "notes": "not executed"}


def _csv_value(value: Any) -> Any:
    return "" if value is None else value


def write_csv(path: str, sections: Sequence[TapSection]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for section in sections:
            for row in section.rows:
                writer.writerow({k: _csv_value(row.get(k)) for k in CSV_COLUMNS})


def write_stream_csv(path: str, sections: Sequence[TapSection]) -> int:
    count = 0
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STREAM_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for section in sections:
            for r in section.stream_results:
                writer.writerow({
                    "tap_name": r.tap_name, "stream_name": r.stream_name, "worker_count": r.worker_count,
                    "worker_id": _csv_value(r.worker_id),
                    "start_time": utc_iso(r.start_ts) if r.start_ts else "", "end_time": utc_iso(r.end_ts) if r.end_ts else "",
                    "duration_seconds": round(r.duration, 3) if r.duration is not None else "",
                    "records_extracted": r.records, "record_limit": _csv_value(r.record_limit) if r.record_limit else "unlimited",
                    "stream_status": r.status, "http_429_count": _csv_value(r.http_429), "retry_count": _csv_value(r.retries),
                    "throttling_count": _csv_value(r.throttling), "error_category": r.error_category,
                    "error_message": r.error_message, "overlapped_with_other_stream": r.overlapped,
                    "work_unit": r.unit_id, "metrics_scope": r.metrics_scope, "timing_basis": r.timing_basis,
                    "api_requests_after_limit": r.api_requests_after_limit})
                count += 1
    return count


def write_error_csv(path: str, sections: Sequence[TapSection]) -> int:
    count = 0
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ERROR_COLUMNS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for section in sections:
            for error in section.errors:
                writer.writerow({k: _csv_value(v) for k, v in error.as_dict().items()})
                count += 1
    return count


def write_summary_json(path: str, meta: Dict[str, Any], sections: Sequence[TapSection]) -> None:
    taps = []
    for section in sections:
        taps.append({
            "tap_name": section.tap_name, "status": section.status, "skip_reasons": section.skip_reasons,
            "selected_streams": section.selected_streams, "excluded_streams": section.excluded_streams,
            "excluded_stream_names": section.excluded_names, "record_limit": section.record_limit_text,
            "rate_limit_info": section.rate_limit_info, "notes": section.notes,
            "decision": section.decision.as_dict() if section.decision else None,
            "runs": [{k: v for k, v in row.items() if k not in ("decision_scope",)} for row in section.rows],
            "error_event_count": len(section.errors),
            "stream_result_count": len(section.stream_results)})
    summary = dict(meta)
    tested = [s for s in sections if not s.skipped]
    summary["totals"] = {
        "taps_total": len(sections), "taps_tested": len(tested),
        "taps_parallel_yes": sum(1 for s in tested if s.decision and s.decision.can_parallel == "YES"),
        "taps_parallel_no": sum(1 for s in tested if s.decision and s.decision.can_parallel == "NO"),
        "taps_skipped": len(sections) - len(tested)}
    summary["taps"] = taps
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, indent=2, sort_keys=False)
        handle.write("\n")


# ----------------------------------------------------------------------------- TXT helpers

def render_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], indent: str = "") -> List[str]:
    cells = [[str(c) for c in row] for row in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    border = indent + "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def fmt(values: Sequence[str]) -> str:
        return indent + "|" + "|".join(" " + v.ljust(w) + " " for v, w in zip(values, widths)) + "|"

    lines = [border, fmt(headers), border]
    lines.extend(fmt(r) for r in cells)
    lines.append(border)
    return lines


def _fmt(value: Any, suffix: str = "", digits: int = 2) -> str:
    if value is None or value == "":
        return "n/a"
    if isinstance(value, float):
        return ("%." + str(digits) + "f") % value + suffix
    return str(value) + suffix


def _is_pool(row: Dict[str, Any]) -> bool:
    return row.get("execution_mode") == "stream_pool"


def observations(section: TapSection) -> List[str]:
    rows = [r for r in section.rows if _is_pool(r) and r.get("worker_count") not in ("", None)]
    rows.sort(key=lambda r: int(r["worker_count"]))
    if not rows:
        return ["No stream-pool runs were executed."]
    base = rows[0] if int(rows[0]["worker_count"]) == 1 else None
    lines: List[str] = []
    for row in rows:
        workers = int(row["worker_count"])
        if workers == 1:
            lines.append("1 worker (baseline): %s in %s s, %s/%s streams succeeded (%s limited by the record limit)."
                         % (row.get("benchmark_status"), _fmt(row.get("execution_duration_seconds")),
                            row.get("successful_streams"), row.get("attempted_streams"), row.get("limited_streams") or 0))
            continue
        speed = row.get("speedup")
        verb = "no baseline to compare"
        if speed is not None:
            verb = ("%.2fx faster" % speed) if speed >= 1 else ("%.2fx (slower than baseline)" % speed)
        parts = ["%d workers: %s" % (workers, verb),
                 "max concurrent streams observed %s of %d (%s)" % (row.get("max_observed_concurrency"), workers,
                                                                  row.get("concurrency_verdict"))]
        if base is not None:
            extra_fail = (row.get("failed_streams") or 0) - (base.get("failed_streams") or 0)
            if extra_fail > 0:
                parts.append("%d more failed stream(s) than baseline" % extra_fail)
            if (row.get("http_429_count") or 0) > (base.get("http_429_count") or 0):
                parts.append("introduced %d HTTP 429 response(s)" % (row.get("http_429_count") or 0))
            if (row.get("throttling_count") or 0) > (base.get("throttling_count") or 0):
                parts.append("throttling signals rose to %d" % (row.get("throttling_count") or 0))
            if (row.get("retry_count") or 0) > (base.get("retry_count") or 0):
                parts.append("retries rose to %d" % (row.get("retry_count") or 0))
            if (row.get("rate_limit_near_count") or 0) > (base.get("rate_limit_near_count") or 0):
                parts.append("server flagged 'near rate limit' on %d response(s)" % (row.get("rate_limit_near_count") or 0))
            if (row.get("timeout_count") or 0) > (base.get("timeout_count") or 0):
                parts.append("timeouts rose to %d" % (row.get("timeout_count") or 0))
            if str(row.get("records_match_baseline", "")).startswith("DIFF"):
                parts.append("record counts differ from baseline (%s)" % row.get("records_match_baseline"))
            base_sum, row_sum = base.get("total_stream_execution_seconds"), row.get("total_stream_execution_seconds")
            if base_sum and row_sum and abs(row_sum - base_sum) / base_sum > 0.10:
                parts.append("summed per-stream time was %.0f%% %s than the baseline (%.1fs vs %.1fs), so part of the "
                             "difference is run-to-run API/network variance rather than parallelism - repeat the run "
                             "before relying on the exact figure"
                             % (abs(row_sum - base_sum) / base_sum * 100, "lower" if row_sum < base_sum else "higher",
                                row_sum, base_sum))
        lines.append("; ".join(parts) + ".")
    if base:
        longest, total = base.get("longest_unit_seconds"), base.get("total_stream_execution_seconds")
        if longest and total:
            lines.append("Scheduling is bounded by the longest unit (%s, %.1fs of %.1fs total): speedup cannot exceed ~%.2fx "
                         "with this stream granularity." % (base.get("longest_unit_name"), longest, total, total / longest))
        if base.get("avg_process_startup_seconds") is not None:
            lines.append("Each work unit ran in its own process; average spawn-to-first-request overhead was %.2fs "
                         "(present in every run, so it does not bias the comparison)." % base["avg_process_startup_seconds"])
        if (base.get("limited_streams") or 0):
            lines.append("%s stream(s) were stopped by the record limit; api_requests_after_limit=%s "
                         "(requests started after the limit was reached)." % (
                             base["limited_streams"], base.get("api_requests_after_limit")))
    return lines


def failure_patterns(section: TapSection) -> List[List[str]]:
    """Per failed stream: which worker counts it failed at, and whether that is consistent."""
    all_workers = sorted(section.stream_outcomes)
    ok = ("COMPLETED", "COMPLETED_WITH_LIMIT")
    streams = sorted({s for outcome in section.stream_outcomes.values() for s, st in outcome.items() if st not in ok})
    table: List[List[str]] = []
    for stream in streams:
        failed = [w for w in all_workers if section.stream_outcomes[w].get(stream) not in (None,) + ok]
        passed = [w for w in all_workers if section.stream_outcomes[w].get(stream) in ok]
        if not passed:
            pattern = "CONSISTENT (fails at every worker count, not caused by parallelism)"
        elif 1 in passed:
            pattern = "PARALLEL-ONLY (passes sequentially)"
        else:
            pattern = "INTERMITTENT"
        table.append([stream[:60], ",".join(map(str, failed)), ",".join(map(str, passed)) or "-", pattern])
    return table


def write_txt(path: str, sections: Sequence[TapSection], meta: Dict[str, Any]) -> None:
    width = 110
    tested = [s for s in sections if not s.skipped]
    yes = [s for s in tested if s.decision and s.decision.can_parallel == "YES"]
    no = [s for s in tested if s.decision and s.decision.can_parallel == "NO"]
    skipped = [s for s in sections if s.skipped]
    lines: List[str] = ["=" * width, "PARALLELISM BENCHMARK SUMMARY".center(width), "=" * width, "",
                        "Run ID         : %s" % meta.get("run_id"),
                        "Started        : %s" % meta.get("started"),
                        "Finished       : %s" % meta.get("finished"),
                        "Worker counts  : %s" % ", ".join(map(str, meta.get("workers", []))),
                        "Timeouts       : run=%ss, per-stream=%ss" % (meta.get("timeout_seconds"), meta.get("stream_timeout_seconds")),
                        "Model          : each selected stream (plus its child streams) is a work unit executed by the unmodified",
                        "                 tap in its own process; N workers pull units from one queue. Durations are extraction",
                        "                 wall-clock only (merge/report excluded). Record limits are applied identically to every",
                        "                 worker count.", ""]

    # ---- A. overall summary
    lines += ["-" * width, "A. OVERALL SUMMARY", "-" * width, "",
              "Total taps in registry       : %d" % len(sections),
              "Taps tested                  : %d" % len(tested),
              "Taps supporting parallelism  : %d  (%s)" % (len(yes), ", ".join(s.tap_name for s in yes) or "-"),
              "Taps NOT supporting          : %d  (%s)" % (len(no), ", ".join(s.tap_name for s in no) or "-"),
              "Taps skipped                 : %d  (%s)" % (len(skipped), ", ".join(
                  "%s: %s" % (s.tap_name, s.status) for s in skipped) or "-"), ""]
    summary_rows = []
    improvements = []
    for s in sections:
        if s.skipped:
            summary_rows.append([s.tap_name, s.status, "NO", "0", "-", "-", "-"])
            continue
        base = next((r for r in s.rows if _is_pool(r) and r.get("worker_count") == 1), {})
        rec = s.decision.recommended_workers if s.decision else 0
        best = next((r for r in s.rows if _is_pool(r) and r.get("worker_count") == rec and rec > 1), None)
        if best and best.get("improvement_percentage") is not None:
            improvements.append(best["improvement_percentage"])
        summary_rows.append([
            s.tap_name, base.get("benchmark_status", "n/a"), s.decision.can_parallel if s.decision else "NO", str(rec),
            _fmt(best.get("speedup"), "x") if best else "-", _fmt(best.get("improvement_percentage"), "%") if best else "-",
            s.record_limit_text])
    lines += render_table(["Tap", "Baseline status", "Parallel OK?", "Recommended workers", "Speedup", "Improvement",
                           "Record limit"], summary_rows)
    if improvements:
        lines += ["", "Average execution-time improvement at the recommended worker counts: %.2f%% (over %d tap(s))." % (
            sum(improvements) / len(improvements), len(improvements))]
    lines.append("")

    # ---- B/C. per tap
    for section in sections:
        lines += ["=" * width, "TAP: %s" % section.tap_name, "=" * width, ""]
        if section.skipped:
            lines.append("Status: %s" % section.status)
            lines += ["  - %s" % r for r in section.skip_reasons]
            lines += ["", "Can Parallel Extraction Be Done? NO (not evaluated - tap skipped)", ""]
            continue
        decision = section.decision
        base = next((r for r in section.rows if _is_pool(r) and r.get("worker_count") == 1), {})
        lines += ["Selected streams      : %d (excluded/not selected in catalog: %d)" % (section.selected_streams, section.excluded_streams),
                  "Record limit applied  : %s per stream" % section.record_limit_text,
                  "Rate limit            : %s" % section.rate_limit_info,
                  "Sequential baseline   : %s s" % _fmt(base.get("execution_duration_seconds"), "", 1), ""]
        headers = ["Workers", "Mode", "Status", "Duration", "Speedup", "Improvement", "Success", "Failed", "Limited",
                   "Records", "Rec/s", "429", "Throttle", "Retries", "4xx", "5xx", "Timeouts", "MaxConc", "Safe"]
        table_rows = []
        for row in section.rows:
            table_rows.append([
                _fmt(row.get("worker_count")),
                {"stream_pool": "pool", "native_sequential": "native"}.get(row.get("execution_mode"), row.get("execution_mode")),
                row.get("benchmark_status"), _fmt(row.get("execution_duration_seconds"), " s", 1),
                _fmt(row.get("speedup"), "x"), _fmt(row.get("improvement_percentage"), "%"),
                _fmt(row.get("success_rate_percentage"), "%", 1), _fmt(row.get("failed_streams")),
                _fmt(row.get("limited_streams")), _fmt(row.get("total_records")),
                _fmt(row.get("throughput_records_per_second"), "", 1), _fmt(row.get("http_429_count")),
                _fmt(row.get("throttling_count")), _fmt(row.get("retry_count")), _fmt(row.get("http_4xx_count")),
                _fmt(row.get("http_5xx_count")), _fmt(row.get("timeout_count")), _fmt(row.get("max_observed_concurrency")),
                row.get("worker_count_safe") or "-"])
        lines += render_table(headers, table_rows) + [""]

        lines.append("Concurrency validation (from stream start/end timestamps):")
        conc_rows = [[_fmt(r.get("worker_count")), _fmt(r.get("max_observed_concurrency")),
                      _fmt(r.get("avg_observed_concurrency")), _fmt(r.get("total_stream_execution_seconds"), " s", 1),
                      _fmt(r.get("execution_duration_seconds"), " s", 1), _fmt(r.get("parallel_efficiency_percentage"), "%", 1),
                      r.get("concurrency_verdict"), _fmt(r.get("records_match_baseline"))] for r in section.rows]
        lines += render_table(["Workers", "MaxConc", "AvgConc", "Sum stream time", "Wall clock", "Efficiency",
                               "Verdict", "Records vs baseline"], conc_rows, "  ") + [""]

        lines += ["Can Parallel Extraction Be Done? %s" % (decision.can_parallel if decision else "NO"),
                  "Recommended Worker Count: %s%s" % (
                      decision.recommended_workers if decision else 0,
                      {0: "  (0 = no safe parallel configuration)", 1: "  (1 = parallel viable but no measurable gain)"}.get(
                          decision.recommended_workers if decision else 0, "")),
                  "Decision basis: %s" % (decision.basis if decision else "n/a"), ""]

        lines.append("Observations:")
        lines += ["- " + o for o in observations(section)]
        failures = failure_patterns(section)
        if failures:
            lines += ["", "Failed streams:"]
            lines += render_table(["Stream", "Failed at workers", "Passed at workers", "Pattern"], failures, "  ")
        else:
            lines += ["- No stream failures in any run."]
        category_totals: Dict[str, int] = {}
        for error in section.errors:
            category_totals[error.category] = category_totals.get(error.category, 0) + 1
        if category_totals:
            lines += ["", "Error / throttle events by category (all runs): " +
                      ", ".join("%s=%d" % kv for kv in sorted(category_totals.items())) + "  (details in errors.csv)"]

        if decision:
            lines += ["", "Reasons:"]
            lines += ["- " + r for r in decision.reasons]
            for warning in decision.warnings:
                lines.append("- Note: " + warning)
            if decision.can_parallel == "NO":
                lines.append("- (See section D below for the consolidated NO explanation.)")
            if decision.technical_findings:
                lines += ["", "Technical validation (code inspection + execution model):"]
                lines += ["- " + f for f in decision.technical_findings]
        for note in section.notes:
            lines += ["Note: " + note]
        lines.append("")

    # ---- D. NO decisions
    lines += ["-" * width, "D. REASONS FOR NO DECISIONS", "-" * width, ""]
    if not no and not skipped:
        lines += ["No tap was classified NO.", ""]
    for section in no:
        lines.append("%s - NO" % section.tap_name)
        lines += ["  * " + r for r in (section.decision.reasons if section.decision else [])]
        lines.append("")
    for section in skipped:
        lines.append("%s - NO (skipped, not evaluated)" % section.tap_name)
        lines += ["  * " + r for r in section.skip_reasons]
        lines.append("")
    lines += ["Notes:", "- One run per configuration: timings are subject to API/network variance.",
              "- Throttling = HTTP 429, or a Retry-After header on an error response, or an exhausted X-RateLimit-Remaining.",
              "- 4xx totals include 429. Some 4xx responses are handled by the tap and do not fail a stream.",
              "- COMPLETED_WITH_LIMIT streams were stopped on purpose at the record limit and are not failures.",
              "=" * width, ""]
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
