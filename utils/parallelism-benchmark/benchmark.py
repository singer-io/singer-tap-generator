"""Singer tap parallelism benchmark - command line entry point.

    python benchmark.py --workers 1 2 3 4
    python benchmark.py --taps tap-activecampaign --workers 1 2 3 --record-limit 500
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Dict, List, Optional

from adapters import get_adapter
from catalog_utils import (StreamPlan, WorkUnit, plan_units, run_discovery_into, write_unit_catalogs,
                           write_working_catalog)
from config import (BenchmarkConfig, ConfigError, FRAMEWORK_DIR, RecordLimitPolicy, StreamSamplePolicy,
                    build_arg_parser, load_config, new_run_id, unique_path, utc_iso)
from decision import (BASIS_ENVIRONMENT, Decision, evaluate_tap, not_evaluated_decision)
from error_analyzer import Scrubber
from executor import StreamScheduler, UnitExecutor, close_logger, make_run_logger
from metrics import (COMPLETED, COMPLETED_WITH_LIMIT, FAILED, PARTIAL, RunResult, SKIPPED, TIMED_OUT,
                     build_stream_results, compare_records, compare_to_baseline, run_metrics, stream_outcomes)
from output_merger import merge_unit_outputs
from probe import probe_environment
from report_generator import (DECISION_SCOPE, TapSection, skipped_row, write_csv, write_error_csv,
                              write_stream_csv, write_summary_json, write_txt)
from results_store import (REPORT_CSV, RUN_MARKER, create_run_dir, write_latest)
from tap_registry import (STATUS_READY, STATUS_ENV_ERROR, STATUS_INVALID_CONFIG, TapSpec, collect_secrets,
                          describe_environment, load_json_file, load_registry, parse_sync_command, preflight,
                          resolve_tap_name, RegistryError)


class _EnvironmentAbort(Exception):
    """A work unit could not start the tap (missing package/entry point): the tap's results are not valid."""


def _console(message: str) -> None:
    print("%s %s" % (utc_iso(), message), flush=True)


def _rel(path: str) -> str:
    return os.path.relpath(path, FRAMEWORK_DIR) if path else ""


def prepare_working_config(spec: TapSpec, tap_dir: str) -> Optional[str]:
    """Copy of the tap config with benchmark-only overrides; the original file is never modified."""
    if not spec.config_overrides:
        return None
    config = load_json_file(spec.abs_config)
    config.update(spec.config_overrides)
    path = unique_path(os.path.join(tap_dir, "_config.json"))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle)
    return path


def rate_limit_text(spec: TapSpec, metrics: Dict[str, Any]) -> str:
    detected = metrics.get("detected_rate_limit_headers")
    if detected:
        return ("%s; detected: %s" % (spec.rate_limit_info, detected)) if spec.rate_limit_info != "UNKNOWN" \
            else "DETECTED: " + detected
    return spec.rate_limit_info or "UNKNOWN"


def error_summary(run: RunResult) -> str:
    parts: List[str] = []
    categories: Dict[str, int] = {}
    for error in run.errors:
        categories[error.category] = categories.get(error.category, 0) + 1
    if categories:
        parts.append("events: " + ", ".join("%s=%d" % kv for kv in sorted(categories.items())))
    for unit in run.units:
        if unit.status in (FAILED, TIMED_OUT):
            process_errors = [e for e in unit.errors if e.source == "process"]
            detail = process_errors[-1] if process_errors else None
            parts.append("%s %s%s" % (unit.unit_id, unit.status, (": %s%s - %s" % (
                detail.category, (" " + str(detail.http_status)) if detail.http_status else "",
                detail.message[:100])) if detail else ""))
        elif unit.status == SKIPPED:
            parts.append("%s SKIPPED (%s)" % (unit.unit_id, unit.note))
    return (" | ".join(parts) if parts else "none")[:1500]


def build_row(spec: TapSpec, run: RunResult, baseline_metrics: Optional[Dict[str, Any]],
              baseline_run: Optional[RunResult], run_id: str, plan: StreamPlan,
              config: BenchmarkConfig) -> Dict[str, Any]:
    metrics = run_metrics(run)
    row: Dict[str, Any] = dict(metrics)
    completed = (baseline_metrics is not None and metrics["benchmark_status"] not in (FAILED, TIMED_OUT, SKIPPED)
                 and baseline_metrics.get("benchmark_status") not in (FAILED, TIMED_OUT, SKIPPED))
    # A speedup only means something if the same work was done: a run that got faster by failing streams (or by
    # succeeding at more streams than the baseline) is not comparable.
    same_work = completed and metrics["successful_streams"] == baseline_metrics.get("successful_streams")
    comparable = completed and same_work
    if run.mode == "stream_pool":
        row.update(compare_to_baseline(metrics, baseline_metrics if comparable else None, run.worker_count))
        if baseline_metrics is not None and not comparable:
            row["sequential_duration_seconds"] = baseline_metrics.get("execution_duration_seconds")
    else:
        row.update({"sequential_duration_seconds": baseline_metrics.get("execution_duration_seconds") if baseline_metrics else None,
                    "improvement_percentage": None, "speedup": None, "parallel_efficiency_percentage": None})
    outcomes = stream_outcomes(run)
    failed = [s for s, st in outcomes.items() if st in (FAILED, TIMED_OUT)]
    not_run = [s for s, st in outcomes.items() if st == SKIPPED]
    notes = list(run.notes)
    if run.mode == "stream_pool" and baseline_metrics is not None:
        if not completed:
            notes.append("speedup not computed: this run or the baseline failed completely")
        elif not same_work:
            notes.append("speedup not computed: not like-for-like (%s successful streams vs %s in the baseline) - a run "
                         "that fails streams is faster for the wrong reason" % (
                             metrics["successful_streams"], baseline_metrics.get("successful_streams")))
    if plan.limited and plan.filtered_streams:
        notes.append("limited run: %d selected stream(s) not scheduled (filters/--max-streams)" % len(plan.filtered_streams))
    if run.mode == "stream_pool" and metrics.get("scheduling_units") and run.worker_count > metrics["scheduling_units"]:
        notes.append("%d workers requested but only %d work units: at most %d streams can run at once" % (
            run.worker_count, metrics["scheduling_units"], metrics["scheduling_units"]))
    if run.run_timed_out:
        notes.append("run timeout budget exhausted")
    if run.stop_reason:
        notes.append(run.stop_reason)
    if spec.config_overrides:
        notes.append("config overrides applied to a working copy: %s" % ", ".join(sorted(spec.config_overrides)))
    row.update({
        "tap_name": spec.name,
        "worker_count": run.worker_count,
        "rate_limit_info": rate_limit_text(spec, metrics),
        "error_summary": error_summary(run),
        "failed_stream_names": ";".join(failed + ["%s(not_run)" % s for s in not_run])[:2000],
        "skipped_stream_names": ";".join(run.excluded_streams)[:2000],
        "start_time": utc_iso(run.started_ts) if run.started_ts else "",
        "end_time": utc_iso(run.ended_ts) if run.ended_ts else "",
        "execution_mode": run.mode,
        "run_id": run_id,
        "records_match_baseline": compare_records(run, baseline_run, config.record_tolerance_pct)
        if run.mode == "stream_pool" and run.worker_count != 1 else ("BASELINE" if run.mode == "stream_pool" else "N/A"),
        "notes": " | ".join(notes),
        "log_file": _rel(run.log_path),
        "output_file": _rel(run.merged_output_path),
    })
    return row


def execute_run(spec: TapSpec, config: BenchmarkConfig, plan: StreamPlan, units: List[WorkUnit], worker_count: int,
                mode: str, tap_dir: str, sync_cmd, scrubber: Scrubber, working_config: Optional[str],
                run_id: str, python: str, limit_text: str) -> RunResult:
    label = "native" if mode == "native_sequential" else "workers_%d" % worker_count
    run_dir = os.path.join(tap_dir, label)
    os.makedirs(run_dir, exist_ok=True)
    log_path = os.path.join(config.logs_dir, spec.name, run_id, "%s.log" % label)
    logger = make_run_logger("bench.%s.%s.%s" % (spec.name, run_id, label), log_path, level=config.log_level)
    run = RunResult(tap_name=spec.name, worker_count=worker_count, mode=mode, log_path=log_path,
                    selected_streams=[s for u in units for s in u.selected],
                    excluded_streams=plan.excluded_streams, filtered_streams=plan.filtered_streams,
                    configured_rate_limit=spec.rate_limit_info, record_limit_text=limit_text)
    try:
        logger.info("[RUN] tap=%s mode=%s configured_workers=%d selected_streams=%d excluded_streams=%d record_limit=%s "
                    "python=%s", spec.name, mode, worker_count, len(run.selected_streams), len(plan.excluded_streams),
                    limit_text, describe_environment())
        if spec.config_overrides:
            logger.info("[RUN] config overrides applied to working copy: %s", ", ".join(sorted(spec.config_overrides)))
        executor = UnitExecutor(spec, sync_cmd, python, scrubber, run_dir, worker_count, logger, working_config,
                                enforce_limits=(mode == "stream_pool"))
        scheduler = StreamScheduler(executor, worker_count, config.timeout_seconds, config.effective_stream_timeout,
                                    config.continue_on_stream_failure, logger,
                                    fail_fast_after=config.baseline_fail_fast
                                    if (mode == "stream_pool" and worker_count == 1) else None)
        try:
            scheduler.run(run, units)
        except KeyboardInterrupt:
            run.notes.append("interrupted by user")
            raise
        finally:
            executor.terminate_all()

        if config.merge_output and mode == "stream_pool":
            merged = merge_unit_outputs(run.units, unique_path(os.path.join(run_dir, "_output_workers_%d.jsonl" % worker_count)))
            run.merged_output_path = merged.path
            run.merge_consistent = merged.consistent
            logger.info("[MERGE] merged_file=%s lines=%d records=%d schemas=%d (identical repeats removed=%d) consistent=%s",
                        merged.path, merged.lines_written, merged.records, merged.schemas,
                        merged.schemas_deduplicated, merged.consistent)
            for problem in merged.problems:
                logger.error("[MERGE] %s", problem)
                run.notes.append("merge: " + problem)
            if not config.keep_unit_outputs and merged.consistent:
                for unit in run.units:
                    if unit.output_path and os.path.isfile(unit.output_path):
                        os.remove(unit.output_path)
                        unit.output_path = ""
        elif mode == "native_sequential" and run.units and run.units[0].output_path:
            run.merged_output_path = run.units[0].output_path
        logger.info("[RUN] complete wall_clock=%.2fs", run.wall_seconds or 0.0)
    finally:
        close_logger(logger)
    return run


def technical_findings(spec: TapSpec, plan: StreamPlan) -> List[str]:
    findings = list(get_adapter(spec.adapter).guarantees())
    findings += list(spec.isolation_findings)
    families = plan.dependency_families
    if families:
        findings.append("Stream dependencies: %d parent/child group(s) are kept in one work unit (%s)." % (
            len(families), ", ".join("%s -> %s" % (u.roots[0], "+".join(s for s in u.selected if s != u.roots[0]))
                                     for u in families[:6]) + (", ..." if len(families) > 6 else "")))
    if plan.detached_streams:
        findings.append("Streams scheduled independently despite a catalog parent (registry independent_streams): %s." %
                        ", ".join(plan.detached_streams))
    return findings


def apply_decision(section: TapSection, decision: Decision, baseline_workers: int = 1) -> None:
    """Copy the tap-level decision onto every row (explicit scope) and add the per-row safety flag."""
    summary = " ".join(decision.reasons[-2:])[:600]
    for row in section.rows:
        row["can_parallel_extraction_be_done"] = decision.can_parallel
        row["max_workers_successfully_tested"] = decision.max_workers_successfully_tested
        row["max_recommended_workers_without_rate_limit"] = decision.max_workers_without_rate_limit
        row["recommended_worker_count"] = decision.recommended_workers
        row["evaluation_status"] = decision.evaluation_status
        row["decision_scope"] = DECISION_SCOPE
        row["decision_basis"] = decision.basis
        row["decision_summary"] = summary
        workers = row.get("worker_count")
        if row.get("execution_mode") != "stream_pool":
            row["worker_count_safe"] = "N/A"
        elif workers == baseline_workers:
            row["worker_count_safe"] = "BASELINE"
        else:
            verdict = decision.verdicts.get(int(workers)) if workers not in (None, "") else None
            if verdict is None:
                row["worker_count_safe"] = "N/A"
            elif verdict.safe:
                row["worker_count_safe"] = "YES"
            elif verdict.non_concurrency_failures_only:
                row["worker_count_safe"] = "INCONCLUSIVE"       # only network/auth/environment errors: not a concurrency result
            else:
                row["worker_count_safe"] = "NO"
            if verdict:
                row["worker_safety_notes"] = "; ".join("%s: %s" % (c.name, c.detail) for c in verdict.failed_checks) or \
                    ("; ".join(verdict.warnings) or "all checks passed")


def benchmark_tap(spec: TapSpec, config: BenchmarkConfig, policy: RecordLimitPolicy, run_id: str, run_dir: str,
                  max_units: Optional[int], sample_source: str = "all-streams") -> TapSection:
    section = TapSection(tap_name=spec.name, rate_limit_info=spec.rate_limit_info)

    def skip(status: str, reasons: List[str], basis: str = "NOT_EVALUATED_SKIPPED") -> TapSection:
        section.status = status
        section.skip_reasons = reasons
        section.rows, section.errors, section.stream_results, section.stream_outcomes = [], [], [], {}
        section.rows.append(skipped_row(spec.name, status, reasons, run_id, spec.rate_limit_info))
        section.decision = not_evaluated_decision(spec.name, reasons, basis, list(spec.isolation_findings))
        for row in section.rows:
            row["decision_basis"] = basis
            row["recommended_worker_count"] = 0
        _console("[%s] %s: %s" % (spec.name, status, "; ".join(reasons)))
        return section

    pre = preflight(spec)
    if pre.status != STATUS_READY:
        return skip(pre.status, pre.reasons)
    try:
        adapter = get_adapter(spec.adapter)
        problems = adapter.preflight(spec)
    except ValueError as exc:
        return skip(STATUS_INVALID_CONFIG, [str(exc)])
    if problems:
        return skip(STATUS_INVALID_CONFIG, problems)
    if config.check_environment:
        probe = probe_environment(spec)
        if not probe.ok:
            reasons = ["tap cannot be started with its own virtualenv: %s" % probe.error]
            if probe.hint:
                reasons.append(probe.hint)
            return skip(STATUS_ENV_ERROR, reasons, BASIS_ENVIRONMENT)
        _console("[%s] environment OK (python %s, requests %s)" % (spec.name, probe.python, probe.requests_version))

    tap_dir = os.path.join(run_dir, "artifacts", spec.name)
    os.makedirs(tap_dir, exist_ok=True)
    try:
        catalog_source = run_discovery_into(spec, tap_dir, config.timeout_seconds) if pre.needs_discovery \
            else spec.abs_catalog
        catalog = load_json_file(catalog_source)
    except Exception as exc:  # pylint: disable=broad-except
        return skip(STATUS_INVALID_CONFIG, ["catalog could not be prepared: %s" % exc])

    plan = plan_units(catalog, spec, max_units, policy.limit_for(spec.name))
    section.selected_streams = len(plan.selected_streams)
    section.excluded_streams = len(plan.excluded_streams)
    section.excluded_names = plan.excluded_streams
    if not plan.units:
        return skip("SKIPPED", ["existing catalog has no selected streams to benchmark"])
    limit_text = policy.describe(spec.name, plan.scheduled_selected)
    section.record_limit_text = limit_text
    _console("[%s] catalog=%s streams=%d selected=%d excluded=%d work_units=%d record_limit=%s" % (
        spec.name, os.path.basename(catalog_source), plan.total_catalog_streams, len(plan.selected_streams),
        len(plan.excluded_streams), len(plan.units), limit_text))

    write_working_catalog(catalog, os.path.join(tap_dir, "_catalog.json"))
    write_unit_catalogs(catalog, plan.units, os.path.join(tap_dir, "_unit_catalogs"))
    scheduled_selected = plan.scheduled_selected
    any_limits = any(u.has_limits for u in plan.units)
    native_unit: Optional[WorkUnit] = None
    if config.native_baseline and not any_limits:
        native_unit = WorkUnit("native_sync", 0, [r for u in plan.units for r in u.roots],
                               [m for u in plan.units for m in u.members], scheduled_selected)
        write_unit_catalogs(catalog, [native_unit], os.path.join(tap_dir, "_native_catalog"))
    elif config.native_baseline:
        section.notes.append("native baseline skipped: record limits make a whole-tap run incomparable")
    del catalog

    scrubber = Scrubber(collect_secrets(spec))
    sync_cmd = parse_sync_command(spec.abs_sync_command)
    python = spec.python_executable()
    working_config = prepare_working_config(spec, tap_dir)
    if plan.filtered_streams:
        section.notes.append("SAMPLED run (%s): only %d of %d selected streams were benchmarked - the first %s work units "
                             "of the catalog - identically for every worker count. Change with max_streams / "
                             "tap_max_streams / --all-streams." % (
                                 sample_source, len(scheduled_selected), len(plan.selected_streams), max_units))
    if spec.config_overrides:
        section.notes.append("benchmark-only config overrides on a working copy of the config: %s. "
                             "The original config file was not modified." % json.dumps(spec.config_overrides))
    if any_limits:
        section.notes.append("record limit %s per stream: extraction of a stream is stopped as soon as its limit is "
                             "reached (status COMPLETED_WITH_LIMIT); the same limits apply to every worker count." % limit_text)
    if config.schedule_order == "longest_first":
        section.notes.append("schedule: parallel runs hand work units to workers longest-first (durations taken from the "
                             "sequential baseline, which itself runs in catalog order), so one slow stream cannot "
                             "start last and dominate the result.")
    if spec.notes:
        section.notes.append(spec.notes)

    runs: Dict[int, RunResult] = {}
    metrics_by_workers: Dict[int, Dict[str, Any]] = {}
    speedups: Dict[int, Optional[float]] = {}
    baseline_run: Optional[RunResult] = None
    baseline_metrics: Optional[Dict[str, Any]] = None
    try:
        if native_unit is not None:
            native = execute_run(spec, config, plan, [native_unit], 1, "native_sequential", tap_dir, sync_cmd, scrubber,
                                 working_config, run_id, python, limit_text)
            if native.environment_failure:
                raise _EnvironmentAbort(native.environment_failure)
            section.rows.append(build_row(spec, native, None, None, run_id, plan, config))
            section.errors.extend(native.errors)
            section.notes.append("Row mode 'native_sequential' is the tap's own unmodified single-process sync; "
                                 "it aborts at the first failing stream, unlike the per-stream pool.")

        for workers in config.workers:
            units_for_run = plan.units
            if workers > 1 and config.schedule_order == "longest_first" and baseline_run is not None:
                durations = {u.unit_id: (u.duration or 0.0) for u in baseline_run.units}
                units_for_run = sorted(plan.units, key=lambda u, d=durations: -d.get(u.unit_id, 0.0))
            run = execute_run(spec, config, plan, units_for_run, workers, "stream_pool", tap_dir, sync_cmd, scrubber,
                              working_config, run_id, python, limit_text)
            if run.environment_failure:
                raise _EnvironmentAbort(run.environment_failure)
            if workers > 1 and units_for_run is not plan.units:
                run.notes.append("work units scheduled longest-first (durations from the sequential baseline)")
            row = build_row(spec, run, baseline_metrics, baseline_run, run_id, plan, config)
            section.rows.append(row)
            section.errors.extend(run.errors)
            section.stream_outcomes[workers] = stream_outcomes(run)
            section.stream_results.extend(build_stream_results(run))
            runs[workers] = run
            metrics_by_workers[workers] = run_metrics(run)
            speedups[workers] = row.get("speedup")
            if workers == 1:
                baseline_run, baseline_metrics = run, metrics_by_workers[1]
            _console("[%s] workers=%d status=%s duration=%ss speedup=%s success=%s/%s limited=%s 429=%s max_concurrency=%s" % (
                spec.name, workers, row["benchmark_status"], row["execution_duration_seconds"], row["speedup"],
                row["successful_streams"], row["attempted_streams"], row["limited_streams"], row["http_429_count"],
                row["max_observed_concurrency"]))
            unstable = (row["failed_streams"] or 0) > ((baseline_metrics or {}).get("failed_streams") or 0) \
                or (row["http_429_count"] or 0) > 0
            if config.stop_on_unstable_run and workers > 1 and unstable:
                section.notes.append("higher worker counts not tried: run at %d workers was unstable" % workers)
                break
            if workers == 1 and metrics_by_workers[1]["successful_streams"] == 0:
                section.notes.append("higher worker counts were NOT run: the sequential baseline did not extract any "
                                     "stream, so there is nothing to compare against (fix the cause shown in the "
                                     "errors and re-run)")
                _console("[%s] baseline extracted nothing - skipping the parallel runs" % spec.name)
                break
    except _EnvironmentAbort as abort:
        return skip(STATUS_ENV_ERROR, [
            "the tap stopped being runnable during the benchmark: %s" % abort,
            "all results of this tap are discarded (they would measure the broken environment, not the tap). Verify "
            "with 'python benchmark.py --dry-run --taps %s', fix the tap's virtualenv, and re-run" % spec.name],
            BASIS_ENVIRONMENT)
    finally:
        if working_config and os.path.isfile(working_config):
            os.remove(working_config)

    decision = evaluate_tap(spec.name, runs, metrics_by_workers, speedups, config, technical_findings(spec, plan))
    section.decision = decision
    apply_decision(section, decision)
    _console("[%s] DECISION can_parallel=%s recommended_workers=%s" % (spec.name, decision.can_parallel,
                                                                       decision.recommended_workers))
    return section


def print_plan(spec: TapSpec, policy: RecordLimitPolicy, max_units: Optional[int], sample_source: str,
               config: BenchmarkConfig) -> None:
    pre = preflight(spec)
    print("%s: %s" % (spec.name, pre.status))
    for reason in pre.reasons:
        print("    - %s" % reason)
    if pre.status != STATUS_READY:
        return
    if config.check_environment:
        print("    environment: %s" % probe_environment(spec).summary())
    if pre.needs_discovery:
        print("    catalog: no usable existing catalog - discovery will run at benchmark time (one API call) and all "
              "streams will be selected; the plan is shown once discovered")
        print("    note: worker counts above the number of discovered streams add no extra concurrency "
              "(the benchmark report states this per run)")
        return
    plan = plan_units(load_json_file(spec.abs_catalog), spec, max_units, policy.limit_for(spec.name))
    print("    catalog streams=%d selected=%d excluded=%d benchmarked=%d work_units=%d record_limit=%s" % (
        plan.total_catalog_streams, len(plan.selected_streams), len(plan.excluded_streams),
        len(plan.scheduled_selected), len(plan.units), policy.describe(spec.name, plan.scheduled_selected)))
    if plan.filtered_streams:
        print("    SAMPLED (%s): %d selected stream(s) are not benchmarked" % (sample_source, len(plan.filtered_streams)))
    print("    python=%s" % spec.python_executable())
    print("    planned runs: %d worker count(s) %s x %d work unit(s) = %d tap process(es)" % (
        len(config.workers), config.workers, len(plan.units), len(config.workers) * len(plan.units)))
    too_many = [w for w in config.workers if w > len(plan.units)]
    if too_many:
        print("    WARNING: worker count(s) %s exceed the %d work units - at most %d streams can run at once, "
              "so those counts add no extra concurrency" % (too_many, len(plan.units), len(plan.units)))
    for unit in plan.units[:25]:
        limits = {s: policy.resolve(spec.name, s) for s in unit.selected}
        shown = ",".join("%s[%s]" % (s, "unlimited" if lim is None else "%s:%s" % (lim, src))
                         for s, (lim, src) in limits.items())
        print("      unit %-40s streams=%s" % (unit.unit_id[:40], shown[:110]))
    if len(plan.units) > 25:
        print("      ... %d more units" % (len(plan.units) - 25))


def _lenient_resolver(registry: Dict[str, TapSpec]):
    """Map aliases to canonical tap names, but leave unknown names alone: a config file may carry limits for taps that
    a smaller registry (e.g. a folder containing a single tap) does not list."""
    def resolve(name: str) -> str:
        try:
            return resolve_tap_name(registry, name)
        except RegistryError:
            return name
    return resolve


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        config = load_config(args)
        registry = load_registry(config.registry_path)
        policy = RecordLimitPolicy(config, _lenient_resolver(registry))
        sampler = StreamSamplePolicy(config, _lenient_resolver(registry))
        requested = [resolve_tap_name(registry, name) for name in (config.enabled_taps or list(registry))]
    except (ConfigError, RegistryError, OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.list_taps:
        for name, spec in registry.items():
            print("%-26s enabled=%s repo=%s" % (name, spec.enabled, spec.repo_path))
        return 0

    explicit = bool(args.taps)
    ordered = list(dict.fromkeys(requested))
    extras: List[str] = []
    if not explicit:
        # Registered-but-not-enabled taps still get an explicit SKIPPED row in the report.
        extras = [n for n in registry if n not in ordered]
    ordered += extras

    if args.dry_run:
        for name in ordered:
            limit, source = sampler.resolve(name)
            print_plan(registry[name], policy, limit, source, config)
        return 0

    run_id = args.run_id or new_run_id(config.results_dir)
    try:
        run_dir = create_run_dir(config.results_dir, run_id)
    except FileExistsError:
        print("error: results folder for run id %r already exists; refusing to overwrite" % run_id, file=sys.stderr)
        return 2
    started = utc_iso()
    _console("run_id=%s workers=%s taps=%s results=%s" % (run_id, config.workers, ", ".join(ordered), run_dir))

    sections: List[TapSection] = []
    interrupted = False
    try:
        for name in ordered:
            spec = registry[name]
            if name in extras:
                pre = preflight(spec)
                status = pre.status if pre.status != STATUS_READY else "SKIPPED"
                reasons = pre.reasons or ["not enabled for this run"]
                section = TapSection(tap_name=name, status=status, skip_reasons=reasons, rate_limit_info=spec.rate_limit_info)
                section.rows.append(skipped_row(name, status, reasons, run_id, spec.rate_limit_info))
                section.decision = not_evaluated_decision(name, reasons)
                sections.append(section)
                _console("[%s] %s: %s" % (name, status, "; ".join(reasons)))
                continue
            try:
                limit, source = sampler.resolve(name)
                sections.append(benchmark_tap(spec, config, policy, run_id, run_dir, limit, source))
            except Exception as exc:  # pylint: disable=broad-except
                reasons = ["framework error while benchmarking: %s: %s" % (type(exc).__name__, exc)]
                _console("[%s] ERROR %s\n%s" % (name, reasons[0], traceback.format_exc()))
                section = TapSection(tap_name=name, status="SKIPPED_ERROR", skip_reasons=reasons,
                                     rate_limit_info=spec.rate_limit_info)
                section.rows.append(skipped_row(name, "FAILED", reasons, run_id, spec.rate_limit_info))
                section.decision = not_evaluated_decision(name, reasons)
                sections.append(section)
    except KeyboardInterrupt:
        interrupted = True
        _console("interrupted - writing partial reports")

    finished = utc_iso()
    csv_path = os.path.join(run_dir, REPORT_CSV)
    write_csv(csv_path, sections)
    stream_count = write_stream_csv(os.path.join(run_dir, "stream_level_results.csv"), sections)
    error_count = write_error_csv(os.path.join(run_dir, "errors.csv"), sections)
    meta = {"run_id": run_id, "started": started, "finished": finished, "interrupted": interrupted,
            "workers": config.workers, "timeout_seconds": config.timeout_seconds,
            "stream_timeout_seconds": config.effective_stream_timeout, "environment": describe_environment(),
            "configuration": config.public_dict(),
            "files": {"consolidated_csv": REPORT_CSV, "stream_level_csv": "stream_level_results.csv",
                      "errors_csv": "errors.csv", "report_txt": "parallelism_report.txt", "summary_json": RUN_MARKER,
                      "artifacts_dir": "artifacts/", "logs_dir": _rel(os.path.join(config.logs_dir))},
            "stream_result_rows": stream_count, "error_events": error_count}
    write_txt(os.path.join(run_dir, "parallelism_report.txt"), sections, meta)
    with open(os.path.join(run_dir, "config_used.json"), "w", encoding="utf-8") as handle:
        json.dump(meta["configuration"], handle, indent=2)
    write_summary_json(os.path.join(run_dir, RUN_MARKER), meta, sections)

    tested_ok = any(not s.skipped and any(r.get("benchmark_status") in (COMPLETED, COMPLETED_WITH_LIMIT, PARTIAL)
                                          for r in s.rows) for s in sections)
    if tested_ok and not interrupted:
        write_latest(config.results_dir, run_id, "completed")
        _console("results/latest.json -> %s" % run_id)
    _console("Results folder : %s" % run_dir)
    _console("  %s, parallelism_report.txt, stream_level_results.csv (%d rows), errors.csv (%d events), %s" % (
        REPORT_CSV, stream_count, error_count, RUN_MARKER))
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
