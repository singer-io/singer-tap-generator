"""Compare two benchmark runs.

    python compare_runs.py --list
    python compare_runs.py latest 2026-11-18_16-30-00
    python compare_runs.py RUN_A RUN_B --tap tap-jira
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Dict, List, Optional, Tuple

from config import RESULTS_DIR
from report_generator import render_table
from results_store import REPORT_CSV, list_runs, read_latest, resolve_run


def _load_rows(run_dir: str) -> Dict[Tuple[str, str], Dict[str, str]]:
    rows: Dict[Tuple[str, str], Dict[str, str]] = {}
    with open(os.path.join(run_dir, REPORT_CSV), "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("execution_mode") == "stream_pool":
                rows[(row["tap_name"], row["worker_count"])] = row
    return rows


def _num(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _delta(a: Optional[float], b: Optional[float]) -> str:
    if a is None or b is None or a == 0:
        return "n/a"
    return "%+.1f%%" % ((b - a) / a * 100.0)


def compare(run_a: str, run_b: str, tap: Optional[str] = None) -> List[str]:
    rows_a, rows_b = _load_rows(run_a), _load_rows(run_b)
    table: List[List[str]] = []
    for key in sorted(set(rows_a) | set(rows_b), key=lambda k: (k[0], int(k[1]))):
        if tap and key[0] != tap:
            continue
        a, b = rows_a.get(key, {}), rows_b.get(key, {})
        da, db = _num(a.get("execution_duration_seconds")), _num(b.get("execution_duration_seconds"))
        table.append([key[0], key[1], a.get("benchmark_status", "-"), b.get("benchmark_status", "-"),
                      "%.1f" % da if da is not None else "-", "%.1f" % db if db is not None else "-", _delta(da, db),
                      a.get("speedup") or "-", b.get("speedup") or "-",
                      "%s/%s" % (a.get("can_parallel_extraction_be_done", "-"), b.get("can_parallel_extraction_be_done", "-")),
                      "%s/%s" % (a.get("max_workers_successfully_tested", "-"), b.get("max_workers_successfully_tested", "-")),
                      "%s/%s" % (a.get("max_recommended_workers_without_rate_limit", "-"),
                                 b.get("max_recommended_workers_without_rate_limit", "-")),
                      "%s/%s" % (a.get("recommended_worker_count", "-"), b.get("recommended_worker_count", "-")),
                      "%s/%s" % (a.get("evaluation_status", "-"), b.get("evaluation_status", "-"))])
    return render_table(["Tap", "Workers", "Status A", "Status B", "Duration A (s)", "Duration B (s)", "Change",
                         "Speedup A", "Speedup B", "Parallel? A/B", "Max OK A/B", "Max w/o RL A/B", "Recommended A/B",
                         "Evaluation A/B"], table)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two benchmark runs (ids, paths or 'latest').")
    parser.add_argument("run_a", nargs="?")
    parser.add_argument("run_b", nargs="?")
    parser.add_argument("--tap", help="only this tap")
    parser.add_argument("--results-dir", default=RESULTS_DIR)
    parser.add_argument("--list", action="store_true", help="list available runs")
    args = parser.parse_args(argv)
    if args.list:
        latest = read_latest(args.results_dir)
        for run in list_runs(args.results_dir):
            print("%s%s" % (run, "   <- latest" if latest and latest.get("run_id") == run else ""))
        return 0
    if not (args.run_a and args.run_b):
        parser.error("two runs are required (or --list)")
    try:
        print("\n".join(compare(resolve_run(args.results_dir, args.run_a), resolve_run(args.results_dir, args.run_b), args.tap)))
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
