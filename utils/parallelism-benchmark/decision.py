"""Safety evaluation: can this tap extract streams in parallel, and with how many workers?"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from config import BenchmarkConfig
from metrics import (COMPLETED, COMPLETED_WITH_LIMIT, OK_STATUSES, PARTIAL, RunResult, stream_outcomes)

BASIS_EVALUATED = "EVALUATED"
BASIS_SKIPPED = "NOT_EVALUATED_SKIPPED"
BASIS_NO_PARALLEL_RUN = "NOT_EVALUATED_NO_PARALLEL_RUN"
BASIS_BASELINE_FAILED = "NOT_EVALUATED_BASELINE_FAILED"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class WorkerVerdict:
    workers: int
    safe: bool
    speedup: Optional[float]
    checks: List[Check] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def failed_checks(self) -> List[Check]:
        return [c for c in self.checks if not c.passed]


@dataclass
class Decision:
    tap_name: str
    can_parallel: str                      # "YES" | "NO"
    recommended_workers: int               # 0 = no safe parallel config, 1 = viable but no gain, >=2 recommended
    basis: str
    best_speedup: Optional[float] = None
    verdicts: Dict[int, WorkerVerdict] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    technical_findings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tap_name": self.tap_name, "can_parallel_extraction_be_done": self.can_parallel,
            "recommended_worker_count": self.recommended_workers, "basis": self.basis,
            "best_safe_speedup": self.best_speedup, "reasons": self.reasons, "warnings": self.warnings,
            "technical_findings": self.technical_findings,
            "worker_verdicts": {
                str(w): {"safe": v.safe, "speedup": v.speedup, "warnings": v.warnings,
                         "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in v.checks]}
                for w, v in sorted(self.verdicts.items())},
        }


def skipped_decision(tap_name: str, reasons: List[str], findings: Optional[List[str]] = None) -> Decision:
    return Decision(tap_name=tap_name, can_parallel="NO", recommended_workers=0, basis=BASIS_SKIPPED,
                    reasons=["Tap was not benchmarked: " + "; ".join(reasons)], technical_findings=findings or [])


def _failed_streams(outcomes: Dict[str, str]) -> Set[str]:
    return {s for s, st in outcomes.items() if st not in OK_STATUSES}


def _data_loss(current: RunResult, baseline: RunResult, tolerance_pct: float) -> List[str]:
    """Streams that completed in both runs but returned fewer records in the parallel run."""
    def totals(run: RunResult) -> Dict[str, int]:
        return {s: (u.tallies[s].records if s in u.tallies else 0) for u in run.units for s in u.streams}
    cur_status, base_status = stream_outcomes(current), stream_outcomes(baseline)
    cur, base = totals(current), totals(baseline)
    losses = []
    for stream, expected in base.items():
        if cur_status.get(stream) in OK_STATUSES and base_status.get(stream) in OK_STATUSES:
            slack = max(2, expected * tolerance_pct / 100.0)
            if cur.get(stream, 0) < expected - slack:
                losses.append("%s (%d vs baseline %d)" % (stream, cur.get(stream, 0), expected))
    return losses


def _verdict(workers: int, run: RunResult, metrics: Dict[str, Any], baseline: RunResult,
             base_metrics: Dict[str, Any], speedup: Optional[float], config: BenchmarkConfig) -> WorkerVerdict:
    checks: List[Check] = []
    warnings: List[str] = []
    cur_out, base_out = stream_outcomes(run), stream_outcomes(baseline)

    # 1. every stream that worked sequentially also works in parallel
    new_failures = sorted(_failed_streams(cur_out) - _failed_streams(base_out))
    checks.append(Check(
        "streams_complete", not new_failures and metrics["status_ok"],
        "all baseline-successful streams also succeeded (%d/%d)" % (metrics["successful_streams"], metrics["attempted_streams"])
        if not new_failures and metrics["status_ok"] else
        "run status %s; streams failing only in parallel: %s" % (metrics["benchmark_status"], ", ".join(new_failures[:8]) or "none")))

    # 2. real overlap was observed
    needed = min(workers, metrics["scheduling_units"])
    verified = metrics["scheduling_units"] >= 2 and metrics["max_observed_concurrency"] >= max(2, needed) \
        and metrics["max_observed_concurrency"] <= workers
    checks.append(Check(
        "concurrency_verified", verified,
        "max observed concurrency %d (configured %d, schedulable units %d)" % (
            metrics["max_observed_concurrency"], workers, metrics["scheduling_units"])))

    # 3. output / state isolation
    merge_ok = run.merge_consistent is not False
    invalid = sum(u.invalid_lines for u in run.units) > sum(u.invalid_lines for u in baseline.units)
    checks.append(Check(
        "output_integrity", merge_ok and not invalid,
        "merged Singer output consistent (record counts equal, every line valid JSON)" if merge_ok and not invalid
        else "merge inconsistent or non-Singer lines on stdout (%s)" % "; ".join(run.notes[-2:])))

    # 4. no data loss relative to sequential
    losses = _data_loss(run, baseline, config.record_tolerance_pct)
    checks.append(Check("data_consistency", not losses,
                        "per-stream record counts match the baseline (tolerance %.1f%%)" % config.record_tolerance_pct
                        if not losses else "fewer records than baseline: " + "; ".join(losses[:5])))

    # 5. timeouts
    extra_timeouts = (metrics["timeout_count"] or 0) - (base_metrics["timeout_count"] or 0)
    checks.append(Check("no_added_timeouts", extra_timeouts <= 0,
                        "no timeouts beyond the baseline" if extra_timeouts <= 0 else "%d additional timeout(s)" % extra_timeouts))

    # 6. rate limiting must be recoverable
    requests = metrics["api_request_count"] or 0
    c429 = metrics["http_429_count"] or 0
    ratio = (c429 / requests) if requests else 0.0
    giveups = (metrics["giveup_count"] or 0) - (base_metrics["giveup_count"] or 0)
    throttled_failures = [e for e in run.errors if e.source == "process" and e.category in ("RATE_LIMIT", "THROTTLING")]
    throttle_ok = not throttled_failures and giveups <= 0 and ratio <= config.max_429_ratio
    if throttle_ok:
        detail = "no unrecovered rate limiting"
        if c429 or (metrics["throttling_count"] or 0):
            detail = "%d HTTP 429 (%.1f%% of %d requests) and %d throttle signal(s), all recovered by the tap's retries" % (
                c429, ratio * 100, requests, metrics["throttling_count"] or 0)
            warnings.append(detail)
    else:
        detail = "rate limiting not recoverable: %d stream failure(s) caused by throttling, %d retry give-up(s), " \
                 "429 ratio %.1f%% (limit %.0f%%)" % (len(throttled_failures), giveups, ratio * 100, config.max_429_ratio * 100)
    checks.append(Check("rate_limiting_recoverable", throttle_ok, detail))

    safe = all(c.passed for c in checks)
    return WorkerVerdict(workers=workers, safe=safe, speedup=speedup, checks=checks, warnings=warnings)


def evaluate_tap(tap_name: str, runs: Dict[int, RunResult], metrics: Dict[int, Dict[str, Any]],
                 speedups: Dict[int, Optional[float]], config: BenchmarkConfig,
                 technical_findings: Optional[List[str]] = None) -> Decision:
    """Decide YES/NO and the recommended worker count from the actual benchmark runs."""
    decision = Decision(tap_name=tap_name, can_parallel="NO", recommended_workers=0, basis=BASIS_EVALUATED,
                        technical_findings=list(technical_findings or []))
    baseline = runs.get(1)
    base_metrics = metrics.get(1)
    if baseline is None or base_metrics is None or base_metrics["successful_streams"] == 0:
        decision.basis = BASIS_BASELINE_FAILED
        decision.reasons.append("The sequential (1-worker) baseline did not extract any stream successfully "
                                "(status %s), so parallel behaviour cannot be judged." % (
                                    base_metrics["benchmark_status"] if base_metrics else "missing"))
        return decision
    parallel = sorted(w for w in runs if w > 1)
    if not parallel:
        decision.basis = BASIS_NO_PARALLEL_RUN
        decision.reasons.append("Only the sequential baseline was run; add a worker count > 1 to evaluate parallel extraction.")
        return decision

    for workers in parallel:
        metrics[workers]["status_ok"] = (
            metrics[workers]["benchmark_status"] in (COMPLETED, COMPLETED_WITH_LIMIT, PARTIAL)
            and metrics[workers]["successful_streams"] > 0)
        decision.verdicts[workers] = _verdict(workers, runs[workers], metrics[workers], baseline, base_metrics,
                                              speedups.get(workers), config)
    safe = {w: v for w, v in decision.verdicts.items() if v.safe}
    for workers, verdict in sorted(decision.verdicts.items()):
        if verdict.safe:
            decision.reasons.append("%d workers PASSED all safety checks: %s" % (
                workers, "; ".join("%s - %s" % (c.name, c.detail) for c in verdict.checks[:4])))
            decision.warnings.extend("%d workers: %s" % (workers, w) for w in verdict.warnings)
        else:
            decision.reasons.append("%d workers FAILED: %s" % (workers, "; ".join(
                "%s - %s" % (c.name, c.detail) for c in verdict.failed_checks)))

    if not safe:
        decision.reasons.append("No tested parallel configuration satisfied all safety checks, so parallel extraction "
                                "is not considered safe for this tap on the tested data.")
        return decision

    decision.can_parallel = "YES"
    scored = {w: (v.speedup or 0.0) for w, v in safe.items()}
    best = max(scored.values())
    chosen = min(w for w, s in scored.items() if s >= 0.95 * best)
    decision.best_speedup = scored[chosen]
    if scored[chosen] <= 1.05:
        decision.recommended_workers = 1
        decision.reasons.append("Parallel extraction is technically viable but gave no measurable speedup "
                                "(best safe %.2fx), so sequential execution (1) is recommended." % scored[chosen])
    else:
        decision.recommended_workers = chosen
        decision.reasons.append(
            "Recommended: %d workers (%.2fx). It is the smallest safe worker count reaching >= 95%% of the best safe "
            "speedup (%.2fx); more workers add risk without meaningful gain." % (chosen, scored[chosen], best)
            if chosen != max(safe) or len(safe) > 1 else
            "Recommended: %d workers (%.2fx), the only safe tested parallel configuration." % (chosen, scored[chosen]))
        if scored[chosen] < config.recommendation_min_speedup:
            decision.warnings.append("Speedup %.2fx is below the %.2fx threshold for a worthwhile gain." % (
                scored[chosen], config.recommendation_min_speedup))
    return decision
