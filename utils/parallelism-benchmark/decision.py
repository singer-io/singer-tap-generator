"""Safety evaluation: can this tap extract streams in parallel, and with how many workers?

Three separate questions are answered from the *measured* runs (nothing is assumed from configuration):

1. ``can_parallel`` (YES/NO)            - did at least one parallel worker count technically work and pass every check?
2. ``max_workers_successfully_tested``  - the highest number of workers that actually ran at once in a run that passed
                                          every check (capped by the number of streams the tap has).
3. ``max_workers_without_rate_limit``   - the highest such number where it, and every lower tested count, ran
                                          without any rate-limit signal.

``evaluation_status`` says how much to trust the answer: CONCLUSIVE, INCONCLUSIVE (e.g. the sequential baseline failed,
or the only failures were network/auth/environment errors that have nothing to do with concurrency) or NOT_EVALUATED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from config import BenchmarkConfig
from error_analyzer import AUTH, ENVIRONMENT, NETWORK
from metrics import (COMPLETED, COMPLETED_WITH_LIMIT, OK_STATUSES, PARTIAL, RunResult, stream_outcomes)

BASIS_EVALUATED = "EVALUATED"
BASIS_SKIPPED = "NOT_EVALUATED_SKIPPED"
BASIS_NO_PARALLEL_RUN = "NOT_EVALUATED_NO_PARALLEL_RUN"
BASIS_BASELINE_FAILED = "NOT_EVALUATED_BASELINE_FAILED"
BASIS_ENVIRONMENT = "NOT_EVALUATED_ENVIRONMENT"
BASIS_TRANSIENT = "EVALUATED_TRANSIENT_FAILURES_ONLY"

CONCLUSIVE = "CONCLUSIVE"
INCONCLUSIVE = "INCONCLUSIVE"
NOT_EVALUATED = "NOT_EVALUATED"

# Failures in these categories are not caused by running streams concurrently.
NON_CONCURRENCY_CATEGORIES = (NETWORK, AUTH, ENVIRONMENT)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class WorkerVerdict:
    workers: int
    safe: bool                               # technically OK and any rate limiting was recovered
    technical_ok: bool
    rate_limited: bool                       # any rate-limit signal observed in this run
    speedup: Optional[float]
    checks: List[Check] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    non_concurrency_failures_only: bool = False

    @property
    def failed_checks(self) -> List[Check]:
        return [c for c in self.checks if not c.passed]


@dataclass
class Decision:
    tap_name: str
    can_parallel: str                        # "YES" | "NO"
    recommended_workers: int                 # 0 = none, 1 = parallel viable but no gain, >=2 recommended
    basis: str
    evaluation_status: str = CONCLUSIVE
    max_workers_successfully_tested: int = 0
    max_workers_without_rate_limit: int = 0
    best_speedup: Optional[float] = None
    verdicts: Dict[int, WorkerVerdict] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    technical_findings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tap_name": self.tap_name, "can_parallel_extraction_be_done": self.can_parallel,
            "max_workers_successfully_tested": self.max_workers_successfully_tested,
            "max_recommended_workers_without_rate_limit": self.max_workers_without_rate_limit,
            "recommended_worker_count": self.recommended_workers, "evaluation_status": self.evaluation_status,
            "basis": self.basis, "best_safe_speedup": self.best_speedup, "reasons": self.reasons,
            "warnings": self.warnings, "technical_findings": self.technical_findings,
            "worker_verdicts": {
                str(w): {"safe": v.safe, "technical_ok": v.technical_ok, "rate_limited": v.rate_limited,
                         "speedup": v.speedup, "warnings": v.warnings,
                         "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in v.checks]}
                for w, v in sorted(self.verdicts.items())}}


def not_evaluated_decision(tap_name: str, reasons: List[str], basis: str = BASIS_SKIPPED,
                           findings: Optional[List[str]] = None) -> Decision:
    """Decision for a tap that could not be benchmarked (skipped, bad environment, ...). The column stays YES/NO,
    so the answer is NO - but ``evaluation_status`` makes clear that nothing was actually evaluated."""
    return Decision(tap_name=tap_name, can_parallel="NO", recommended_workers=0, basis=basis,
                    evaluation_status=NOT_EVALUATED, reasons=["Tap was not benchmarked: " + "; ".join(reasons)],
                    technical_findings=findings or [])


skipped_decision = not_evaluated_decision      # backwards-compatible name


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


def _new_failure_categories(run: RunResult, baseline: RunResult) -> Set[str]:
    """Error categories of the process-level failures of units that failed only in the parallel run."""
    base_failed = _failed_streams(stream_outcomes(baseline))
    categories: Set[str] = set()
    for unit in run.units:
        if unit.status in OK_STATUSES or all(s in base_failed for s in unit.streams):
            continue
        process = [e.category for e in unit.errors if e.source == "process"]
        categories.update(process or ["UNKNOWN"])
    return categories


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
    technical_ok = all(c.passed for c in checks)

    # 6. rate limiting must be recoverable (reported separately from the technical result)
    requests = metrics["api_request_count"] or 0
    c429 = metrics["http_429_count"] or 0
    ratio = (c429 / requests) if requests else 0.0
    giveups = (metrics["giveup_count"] or 0) - (base_metrics["giveup_count"] or 0)
    throttled_failures = [e for e in run.errors if e.source == "process" and e.category in ("RATE_LIMIT", "THROTTLING")]
    throttle_ok = not throttled_failures and giveups <= 0 and ratio <= config.max_429_ratio
    rate_limited = metrics.get("rate_limit_encountered") == "YES"
    if throttle_ok:
        detail = "no unrecovered rate limiting"
        if rate_limited:
            detail = "%d HTTP 429 (%.1f%% of %d requests) and %d throttle signal(s), all recovered by the tap's retries" % (
                c429, ratio * 100, requests, metrics["throttling_count"] or 0)
            warnings.append(detail)
    else:
        detail = "rate limiting not recoverable: %d stream failure(s) caused by throttling, %d retry give-up(s), " \
                 "429 ratio %.1f%% (limit %.0f%%)" % (len(throttled_failures), giveups, ratio * 100, config.max_429_ratio * 100)
    checks.append(Check("rate_limiting_recoverable", throttle_ok, detail))

    safe = technical_ok and throttle_ok
    non_concurrency_only = False
    if not safe:
        categories = _new_failure_categories(run, baseline)
        non_concurrency_only = bool(categories) and categories <= set(NON_CONCURRENCY_CATEGORIES)
        if non_concurrency_only:
            warnings.append("failures were %s errors, which are not caused by concurrency (likely transient or "
                            "environmental) - re-run before trusting this result" % "/".join(sorted(categories)))
    if metrics.get("rate_limit_encountered") == "UNKNOWN":
        warnings.append("HTTP traffic could not be measured (requests not used by this tap), so the absence of rate "
                        "limiting cannot be confirmed")
    return WorkerVerdict(workers=workers, safe=safe, technical_ok=technical_ok, rate_limited=rate_limited,
                         speedup=speedup, checks=checks, warnings=warnings,
                         non_concurrency_failures_only=non_concurrency_only)


def evaluate_tap(tap_name: str, runs: Dict[int, RunResult], metrics: Dict[int, Dict[str, Any]],
                 speedups: Dict[int, Optional[float]], config: BenchmarkConfig,
                 technical_findings: Optional[List[str]] = None) -> Decision:
    """Decide YES/NO, the safe worker counts and the rate-limit-free worker count from the actual runs."""
    decision = Decision(tap_name=tap_name, can_parallel="NO", recommended_workers=0, basis=BASIS_EVALUATED,
                        technical_findings=list(technical_findings or []))
    baseline, base_metrics = runs.get(1), metrics.get(1)
    if baseline is None or base_metrics is None or base_metrics["successful_streams"] == 0:
        decision.basis = BASIS_BASELINE_FAILED
        decision.evaluation_status = INCONCLUSIVE
        categories = sorted({e.category for e in baseline.errors if e.source == "process"}) if baseline else []
        decision.reasons.append(
            "The sequential (1-worker) baseline did not extract any stream successfully (status %s%s), so parallel "
            "behaviour cannot be judged. This is not evidence against parallel extraction: fix the cause and re-run." % (
                base_metrics["benchmark_status"] if base_metrics else "missing",
                ", errors: " + "/".join(categories) if categories else ""))
        return decision
    decision.max_workers_successfully_tested = 1
    base_rate_limited = base_metrics.get("rate_limit_encountered") == "YES"
    decision.max_workers_without_rate_limit = 0 if base_rate_limited else 1
    if base_rate_limited:
        decision.warnings.append("the sequential baseline itself encountered rate limiting: even 1 worker is not rate-limit free")

    parallel = sorted(w for w in runs if w > 1)
    if not parallel:
        decision.basis = BASIS_NO_PARALLEL_RUN
        decision.evaluation_status = INCONCLUSIVE
        decision.reasons.append("Only the sequential baseline was run; add a worker count > 1 to evaluate parallel extraction.")
        return decision

    for workers in parallel:
        metrics[workers]["status_ok"] = (
            metrics[workers]["benchmark_status"] in (COMPLETED, COMPLETED_WITH_LIMIT, PARTIAL)
            and metrics[workers]["successful_streams"] > 0)
        decision.verdicts[workers] = _verdict(workers, runs[workers], metrics[workers], baseline, base_metrics,
                                              speedups.get(workers), config)
    for workers, verdict in sorted(decision.verdicts.items()):
        units = metrics[workers].get("scheduling_units") or 0
        if units and workers > units:
            decision.warnings.append(
                "%d workers requested but this tap has only %d work units, so at most %d streams ran at once (%d worker(s) "
                "idle). Counts above %d cannot show more concurrency; treat them as the same test as %d." % (
                    workers, units, units, workers - units, units, units))
    safe = {w: v for w, v in decision.verdicts.items() if v.safe}
    for workers, verdict in sorted(decision.verdicts.items()):
        if verdict.safe:
            decision.reasons.append("%d workers PASSED all safety checks%s: %s" % (
                workers, " (rate limiting encountered but recovered)" if verdict.rate_limited else "",
                "; ".join("%s - %s" % (c.name, c.detail) for c in verdict.checks[:4])))
            decision.warnings.extend("%d workers: %s" % (workers, w) for w in verdict.warnings)
        else:
            decision.reasons.append("%d workers FAILED: %s" % (workers, "; ".join(
                "%s - %s" % (c.name, c.detail) for c in verdict.failed_checks)))
            decision.warnings.extend("%d workers: %s" % (workers, w) for w in verdict.warnings)

    def effective(workers: int) -> int:
        """Workers that could actually run at once: a tap with 11 streams cannot use 12 workers."""
        units = metrics[workers].get("scheduling_units") or workers
        return min(workers, units)

    # Highest number of workers that actually ran concurrently in a run that passed every check.
    if safe:
        decision.max_workers_successfully_tested = max(effective(w) for w in safe)

    # Highest number of workers that, together with every lower tested count, had no rate-limit signal.
    clean_counts: Set[int] = set()
    if not base_rate_limited:
        for workers in parallel:
            verdict = decision.verdicts[workers]
            if verdict.safe and not verdict.rate_limited and metrics[workers].get("rate_limit_encountered") == "NO":
                decision.max_workers_without_rate_limit = effective(workers)
                clean_counts.add(workers)
            else:
                break

    if not safe:
        only_transient = all(v.non_concurrency_failures_only for v in decision.verdicts.values())
        if only_transient:
            decision.evaluation_status = INCONCLUSIVE
            decision.basis = BASIS_TRANSIENT
            decision.reasons.append(
                "No parallel configuration passed, but every failure was a network/auth/environment error unrelated to "
                "concurrency. This is INCONCLUSIVE rather than evidence that the tap cannot run in parallel: re-run.")
        else:
            decision.reasons.append("No tested parallel configuration satisfied all safety checks, so parallel extraction "
                                    "is not considered safe for this tap on the tested data.")
        return decision

    decision.can_parallel = "YES"
    speed = {w: (v.speedup or 0.0) for w, v in safe.items()}
    # Prefer worker counts that never touched a rate limit; fall back to safe counts that recovered from it.
    clean = {w: s for w, s in speed.items() if w in clean_counts}
    pool = clean or speed
    best = max(pool.values())
    chosen = min(w for w, s in pool.items() if s >= 0.95 * best)
    decision.best_speedup = pool[chosen]
    if not clean:
        decision.warnings.append("every safe parallel worker count met rate limiting (recovered by the tap's retries); "
                                 "the recommendation is the best of those")
    if pool[chosen] <= 1.05:
        decision.recommended_workers = 1
        decision.reasons.append("Parallel extraction is technically viable but gave no measurable speedup "
                                "(best safe %.2fx), so sequential execution (1) is recommended." % pool[chosen])
    else:
        decision.recommended_workers = chosen
        decision.reasons.append(
            "Recommended: %d workers (%.2fx). It is the smallest %sworker count reaching >= 95%% of the best speedup "
            "(%.2fx) in its pool; more workers add risk without meaningful gain." % (
                chosen, pool[chosen], "rate-limit-free safe " if clean else "safe ", best)
            if len(pool) > 1 else
            "Recommended: %d workers (%.2fx), the only %sparallel configuration." % (
                chosen, pool[chosen], "rate-limit-free safe " if clean else "safe "))
        if pool[chosen] < config.recommendation_min_speedup:
            decision.warnings.append("Speedup %.2fx is below the %.2fx threshold for a worthwhile gain." % (
                pool[chosen], config.recommendation_min_speedup))
    return decision
