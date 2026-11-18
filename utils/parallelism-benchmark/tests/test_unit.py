"""Unit tests: configuration, record limits, planning, metrics, decisions, reports, results store."""
import csv
import json
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import error_analyzer as ea  # noqa: E402
from catalog_utils import plan_units  # noqa: E402
from config import (BenchmarkConfig, ConfigError, RecordLimitPolicy, build_arg_parser, load_config,  # noqa: E402
                    new_run_id)
from decision import BASIS_NO_PARALLEL_RUN, evaluate_tap  # noqa: E402
from metrics import (COMPLETED, COMPLETED_WITH_LIMIT, FAILED, PARTIAL, RunResult, StreamTally,  # noqa: E402
                     UnitResult, build_stream_results, compare_to_baseline, concurrency_stats, run_metrics,
                     summarize_events)
from output_merger import merge_unit_outputs  # noqa: E402
from report_generator import CSV_COLUMNS, TapSection, render_table, write_csv, write_stream_csv  # noqa: E402
from results_store import list_runs, read_latest, resolve_run, write_latest, create_run_dir  # noqa: E402
from tap_registry import TapSpec, parse_sync_command  # noqa: E402


# ----------------------------------------------------------------------------- helpers

def _stream(name, selected=True, parent=None):
    meta = {"selected": selected}
    if parent:
        meta["parent-tap-stream-id"] = parent
    return {"tap_stream_id": name, "stream": name, "metadata": [{"breadcrumb": [], "metadata": meta}]}


def _spec(**kw):
    return TapSpec(name="t", repo_path=".", config_path="c.json", sync_command_path="s.txt", catalog_path="k.json", **kw)


def _args(argv, config_path=None):
    args = build_arg_parser().parse_args(argv)
    args.config = config_path or ""
    return args


def _write_config(tmp_path, data):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data))
    return str(path)


def _unit(name, start, end, status=COMPLETED, records=10, streams=None, limit=None, worker=1, workers=2):
    streams = streams or [name]
    unit = UnitResult(tap_name="t", worker_count=workers, unit_id=name, streams=streams, roots=[streams[0]],
                      status=status, start_ts=start, end_ts=end, worker_id=worker)
    unit.tallies = {s: StreamTally(limit=limit, records=records,
                                   limit_reached=(status == COMPLETED_WITH_LIMIT)) for s in streams}
    unit.http = summarize_events([{"ev": "process_start", "ts": start}])
    return unit


# ----------------------------------------------------------------------------- configuration

def test_config_file_then_cli_override(tmp_path):
    path = _write_config(tmp_path, {"workers": [1, 2], "enabled_taps": ["tap-jira"], "timeout_seconds": 99})
    config = load_config(_args([], path))
    assert config.workers == [1, 2] and config.sources["workers"] == "config-file"
    config = load_config(_args(["--workers", "3", "4", "--taps", "tap-x", "--timeout", "5"], path))
    assert config.workers == [1, 3, 4]               # baseline 1 always added
    assert config.enabled_taps == ["tap-x"] and config.timeout_seconds == 5
    assert config.sources["workers"] == "cli" and config.sources["enabled_taps"] == "cli"


def test_config_rejects_unknown_and_invalid_values(tmp_path):
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(_args([], _write_config(tmp_path, {"worker": [1]})))
    with pytest.raises(ConfigError, match="worker count"):
        load_config(_args([], _write_config(tmp_path, {"workers": [0, 2]})))
    with pytest.raises(ConfigError, match="positive integer"):
        load_config(_args([], _write_config(tmp_path, {"default_record_limit": "abc"})))
    with pytest.raises(ConfigError, match="not found"):
        load_config(_args([], str(tmp_path / "missing.json")))


def test_record_limit_alias_in_config_file(tmp_path):
    config = load_config(_args([], _write_config(tmp_path, {"record_limit": 1000})))
    assert config.default_record_limit == 1000


def test_record_limit_precedence(tmp_path):
    path = _write_config(tmp_path, {
        "default_record_limit": 1000, "tap_record_limits": {"tap-a": 500},
        "stream_record_limits": {"tap-a": {"contacts": 50}}})
    policy = RecordLimitPolicy(load_config(_args([], path)))
    assert policy.resolve("tap-a", "contacts") == (50, "config-stream")
    assert policy.resolve("tap-a", "other") == (500, "config-tap")
    assert policy.resolve("tap-b", "x") == (1000, "config-default")
    # CLI --record-limit overrides every config-file limit
    policy = RecordLimitPolicy(load_config(_args(["--record-limit", "7"], path)))
    assert policy.resolve("tap-a", "contacts") == (7, "cli-global")
    assert policy.resolve("tap-b", "x") == (7, "cli-global")
    # finer CLI flags beat the uniform one
    policy = RecordLimitPolicy(load_config(_args(
        ["--record-limit", "7", "--tap-record-limit", "tap-a=9", "--stream-record-limit", "tap-a:contacts=3"], path)))
    assert policy.resolve("tap-a", "contacts") == (3, "cli-stream")
    assert policy.resolve("tap-a", "x") == (9, "cli-tap")
    assert policy.resolve("tap-b", "x") == (7, "cli-global")
    # no limit at all => unlimited
    assert RecordLimitPolicy(BenchmarkConfig()).resolve("tap-a", "s") == (None, "unlimited")
    assert RecordLimitPolicy(load_config(_args(["--no-record-limit"], path))).resolve("tap-a", "s")[0] is None


def test_record_limit_policy_resolves_tap_aliases():
    config = BenchmarkConfig(tap_record_limits={"short": 5})
    policy = RecordLimitPolicy(config, lambda name: {"short": "tap-long"}.get(name, name))
    assert policy.resolve("tap-long", "s") == (5, "config-tap")
    assert policy.describe("tap-long", ["a", "b"]) == "5"


def test_new_run_id_is_unique(tmp_path):
    first = new_run_id(str(tmp_path))
    os.makedirs(tmp_path / first)
    assert new_run_id(str(tmp_path)) != first


# ----------------------------------------------------------------------------- planning

def test_parse_sync_command_strips_redirect_and_rebinds_catalog(tmp_path):
    path = tmp_path / "sync.txt"
    path.write_text("tap-x --config ./tmp/c.json --properties ./tmp/k.json > ./tmp/out.json\n")
    cmd = parse_sync_command(str(path))
    assert cmd.script == "tap-x" and cmd.catalog_flag == "--properties"
    assert cmd.build_args("NEW.json") == ["--config", "./tmp/c.json", "--properties", "NEW.json"]


def test_plan_units_groups_children_and_preserves_selection():
    catalog = {"streams": [_stream("a"), _stream("a_child", parent="a"), _stream("b", selected=False), _stream("c")]}
    plan = plan_units(catalog, _spec())
    assert [u.unit_id for u in plan.units] == ["a", "c"]
    assert plan.units[0].members == ["a", "a_child"]
    assert plan.excluded_streams == ["b"] and plan.selected_streams == ["a", "a_child", "c"]


def test_plan_units_independent_stream_is_detached():
    catalog = {"streams": [_stream("contacts"), _stream("email_activities", parent="contacts")]}
    assert len(plan_units(catalog, _spec()).units) == 1
    plan = plan_units(catalog, _spec(independent_streams=["email_activities"]))
    assert [u.unit_id for u in plan.units] == ["contacts", "email_activities"]


def test_plan_units_unselected_parent_with_selected_child_still_runs():
    catalog = {"streams": [_stream("p", selected=False), _stream("k", parent="p")]}
    plan = plan_units(catalog, _spec())
    assert len(plan.units) == 1 and plan.units[0].selected == ["k"] and "p" in plan.units[0].members


def test_plan_units_batches_only_unlimited_streams():
    catalog = {"streams": [_stream("s%d" % i) for i in range(6)]}
    plan = plan_units(catalog, _spec(streams_per_task=3))
    assert [len(u.roots) for u in plan.units] == [3, 3]
    limited = plan_units(catalog, _spec(streams_per_task=3), limit_for=lambda s: 10 if s == "s1" else None)
    assert [u.roots for u in limited.units] == [["s0"], ["s1"], ["s2", "s3", "s4"], ["s5"]]
    assert limited.units[1].limits == {"s1": 10} and limited.units[1].has_limits
    capped = plan_units(catalog, _spec(), max_units=2)
    assert len(capped.units) == 2 and capped.limited and len(capped.filtered_streams) == 4


# ----------------------------------------------------------------------------- metrics

def test_concurrency_stats_overlap_and_back_to_back():
    stats = concurrency_stats([(0, 10), (2, 8), (5, 12)], (0, 12))
    assert stats["max"] == 3 and stats["total_busy"] == pytest.approx(23)
    sequential = concurrency_stats([(0, 5), (5, 10)], (0, 10))
    assert sequential["max"] == 1 and sequential["avg"] == pytest.approx(1.0)


def test_classification():
    assert ea.classify_http(429) == ea.RATE_LIMIT
    assert ea.classify_http(503, headers={"retry-after": "5"}) == ea.THROTTLING
    assert ea.classify_http(503) == ea.SERVER_ERROR
    assert ea.classify_http(401) == ea.AUTH
    assert ea.classify_http(504) == ea.TIMEOUT
    assert ea.classify_http(404) == ea.CLIENT_ERROR
    assert ea.classify_http(None, "ReadTimeout") == ea.TIMEOUT
    assert ea.classify_http(None, "ConnectionError") == ea.NETWORK
    assert ea.classify_text("HTTP-error-code: 429, Error: x") == ea.RATE_LIMIT
    assert ea.classify_text("something odd") is None


def test_ratelimit_header_signals():
    assert ea.rate_headers_signal_throttle({"ratelimit": '"burst";r=0;t=3'})
    assert not ea.rate_headers_signal_throttle({"ratelimit": '"burst";r=349;t=1'})
    assert ea.rate_headers_signal_throttle({"x-ratelimit-remaining": "0"})
    assert not ea.rate_headers_signal_throttle({"x-ratelimit-limit": "350"})
    # an informational Retry-After on a 200 (ActiveCampaign sends one on every response) is not throttling
    assert not ea.rate_headers_signal_throttle({"retry-after": "1"}, 200)
    assert ea.rate_headers_signal_throttle({"retry-after": "1"}, 503)
    assert not ea.is_throttle_signal({"status": 200, "rate_headers": {"retry-after": "1", "ratelimit-remaining": "997"}})
    assert ea.is_near_limit({"rate_headers": {"x-ratelimit-nearlimit": "true"}})


def test_scrubber_removes_known_and_pattern_secrets():
    scrub = ea.Scrubber(["s3cr3t-value"]).scrub
    assert "s3cr3t-value" not in scrub("password is s3cr3t-value here")
    assert "abcdefghijk" not in scrub("Authorization: Bearer abcdefghijk")
    assert "FAKE-TEST-VALUE" not in scrub("client_secret=FAKE-TEST-VALUE&x=1")


def test_summarize_events_counts():
    events = [
        {"ev": "process_start", "ts": 100.0},
        {"ev": "http", "ts": 101.0, "dur": 0.5, "status": 200},
        {"ev": "http", "ts": 102.0, "dur": 0.5, "status": 429, "rate_headers": {"retry-after": "3"}},
        {"ev": "http", "ts": 103.0, "dur": 0.5, "status": 503},
        {"ev": "http", "ts": 104.0, "dur": 9.0, "status": None, "exc": "ReadTimeout"},
        {"ev": "retry", "ts": 104.1}]
    summary = summarize_events(events)
    assert (summary.requests, summary.http_429, summary.http_4xx, summary.http_5xx) == (4, 1, 1, 1)
    assert summary.timeouts == 1 and summary.retries == 1 and summary.throttle_signals == 1


def test_run_metrics_statuses_and_limits():
    run = RunResult(tap_name="t", worker_count=2, started_ts=0, ended_ts=10, selected_streams=["a", "b"],
                    excluded_streams=["x"], units=[_unit("a", 0, 10), _unit("b", 0, 6, FAILED)])
    metrics = run_metrics(run)
    assert metrics["benchmark_status"] == PARTIAL
    assert (metrics["successful_streams"], metrics["failed_streams"], metrics["skipped_streams"]) == (1, 1, 1)
    assert metrics["max_observed_concurrency"] == 2 and metrics["concurrency_verdict"] == "FULL"
    assert metrics["success_rate_percentage"] == 50.0
    limited = RunResult(tap_name="t", worker_count=1, started_ts=0, ended_ts=4, selected_streams=["a", "b"],
                        units=[_unit("a", 0, 2, COMPLETED_WITH_LIMIT, limit=10), _unit("b", 2, 4)])
    metrics = run_metrics(limited)
    assert metrics["benchmark_status"] == COMPLETED_WITH_LIMIT and metrics["limited_streams"] == 1
    assert metrics["failed_streams"] == 0 and metrics["limited_stream_names"] == "a"
    comparison = compare_to_baseline({"execution_duration_seconds": 10.0}, {"execution_duration_seconds": 20.0}, 2)
    assert comparison["speedup"] == 2.0 and comparison["improvement_percentage"] == 50.0
    assert compare_to_baseline({}, None, 2)["speedup"] is None


def test_stream_results_overlap_flags():
    run = RunResult(tap_name="t", worker_count=2, started_ts=0, ended_ts=10, selected_streams=["a", "b", "c"],
                    units=[_unit("a", 0, 5), _unit("b", 3, 8, worker=2), _unit("c", 9, 10)])
    by_name = {r.stream_name: r for r in build_stream_results(run)}
    assert by_name["a"].overlapped == "YES" and by_name["b"].overlapped == "YES" and by_name["c"].overlapped == "NO"


# ----------------------------------------------------------------------------- decision

def _evaluate(base_status=COMPLETED, par_status=COMPLETED, par_end=5.0, par_workers=2, extra_429=0, par_failed=False):
    config = BenchmarkConfig()
    base = RunResult(tap_name="t", worker_count=1, started_ts=0, ended_ts=10, selected_streams=["a", "b"],
                     units=[_unit("a", 0, 5, base_status, workers=1), _unit("b", 5, 10, base_status, workers=1)])
    base.merge_consistent = True
    units = [_unit("a", 0, par_end, par_status, workers=par_workers),
             _unit("b", 0 if par_workers > 1 else par_end, par_end * 2 if par_workers == 1 else par_end,
                   FAILED if par_failed else par_status, workers=par_workers, worker=2)]
    par = RunResult(tap_name="t", worker_count=par_workers, started_ts=0, ended_ts=par_end, selected_streams=["a", "b"],
                    units=units)
    par.merge_consistent = True
    if extra_429:
        par.units[0].http = summarize_events([{"ev": "process_start", "ts": 0}] + [
            {"ev": "http", "ts": 1.0 + i, "dur": 0.1, "status": 429} for i in range(extra_429)] + [
            {"ev": "http", "ts": 20.0, "dur": 0.1, "status": 200}] * 20)
    runs = {1: base, par_workers: par} if par_workers > 1 else {1: base}
    metrics = {w: run_metrics(r) for w, r in runs.items()}
    speed = {w: (10.0 / (m["execution_duration_seconds"] or 1)) for w, m in metrics.items()}
    return evaluate_tap("t", runs, metrics, speed, config, ["finding"])


def test_decision_yes_when_parallel_run_is_safe():
    decision = _evaluate()
    assert decision.can_parallel == "YES" and decision.recommended_workers == 2
    assert decision.verdicts[2].safe and decision.best_speedup == pytest.approx(2.0)


def test_decision_no_when_streams_only_fail_in_parallel():
    decision = _evaluate(par_failed=True)
    assert decision.can_parallel == "NO" and decision.recommended_workers == 0
    assert any(c.name == "streams_complete" for c in decision.verdicts[2].failed_checks)
    assert any("FAILED" in reason for reason in decision.reasons)


def test_decision_tolerates_recovered_429_but_not_excessive_429():
    assert _evaluate(extra_429=1).can_parallel == "YES"              # 1/21 requests recovered
    assert _evaluate(extra_429=15).can_parallel == "NO"              # far above max_429_ratio


def test_decision_no_gain_recommends_one_worker():
    decision = _evaluate(par_end=10.0)                                # same wall-clock as baseline
    assert decision.can_parallel == "YES" and decision.recommended_workers == 1


def test_decision_requires_a_parallel_run():
    decision = _evaluate(par_workers=1)
    assert decision.can_parallel == "NO" and decision.basis == BASIS_NO_PARALLEL_RUN


# ----------------------------------------------------------------------------- output / pump / reports

def test_merge_keeps_order_dedupes_schema_and_folds_state():
    with tempfile.TemporaryDirectory() as tmp:
        units = []
        for name, bookmark in (("a", "1"), ("b", "2")):
            path = os.path.join(tmp, name + ".jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for message in ({"type": "SCHEMA", "stream": "shared", "schema": {}},
                                {"type": "SCHEMA", "stream": name, "schema": {}},
                                {"type": "RECORD", "stream": name, "record": {"id": 1}},
                                {"type": "STATE", "value": {"currently_syncing": name, "bookmarks": {name: bookmark}}}):
                    handle.write(json.dumps(message) + "\n")
            unit = _unit(name, 0, 1, records=1)
            unit.output_path = path
            units.append(unit)
        report = merge_unit_outputs(units, os.path.join(tmp, "merged.jsonl"))
        assert report.consistent and report.records == 2 and report.schemas_deduplicated == 1
        lines = [json.loads(line) for line in open(report.path, encoding="utf-8")]
        assert lines[-1] == {"type": "STATE", "value": {"bookmarks": {"a": "1", "b": "2"}}}
        assert [m["type"] for m in lines].count("STATE") == 1


def _pump(tmp_path, limits, roots, selected, total=1000):
    from catalog_utils import WorkUnit
    from executor import UnitExecutor
    script = ("import time\n"
              "for s in %r:\n"
              "    print('{\"type\": \"SCHEMA\", \"stream\": \"%%s\", \"schema\": {}}' %% s, flush=True)\n"
              "for i in range(%d):\n"
              "    for s in %r:\n"
              "        print('{\"type\": \"RECORD\", \"stream\": \"%%s\", \"record\": {\"id\": %%d}}' %% (s, i), flush=True)\n"
              "    time.sleep(0.005)\n") % (selected, total, selected)
    unit = WorkUnit("u", 0, roots, selected, selected, limits)
    result = UnitResult(tap_name="t", worker_count=1, unit_id="u", streams=selected, roots=roots,
                        output_path=str(tmp_path / "out.jsonl"))
    result.tallies = {s: StreamTally(limit=limits.get(s)) for s in selected}
    executor = UnitExecutor.__new__(UnitExecutor)
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
    counters = {"invalid": 0, "schema": 0, "state": 0, "stopped": False, "limit_hit_ts": None}
    executor._pump_stdout(proc, unit, result, counters)  # pylint: disable=protected-access
    proc.wait(timeout=20)
    return result, counters, tmp_path / "out.jsonl"


def test_record_limit_stops_process_at_exact_limit(tmp_path):
    result, counters, out = _pump(tmp_path, {"s": 5}, ["s"], ["s"])
    assert counters["stopped"] and result.tallies["s"].records == 5 and result.tallies["s"].limit_reached
    assert len(out.read_text().splitlines()) == 6                      # schema + exactly 5 records


def test_record_limit_child_limit_drops_extra_records_without_stopping(tmp_path):
    result, counters, _ = _pump(tmp_path, {"child": 3, "root": None}, ["root"], ["root", "child"], total=20)
    assert not counters["stopped"]                                     # unlimited root keeps running
    assert result.tallies["child"].records == 3 and result.tallies["child"].dropped == 17
    assert result.tallies["root"].records == 20


def test_record_limit_root_limit_stops_unit_and_trims_children(tmp_path):
    result, counters, _ = _pump(tmp_path, {"root": 4}, ["root"], ["root", "child"])
    assert counters["stopped"] and result.tallies["root"].records == 4
    assert result.tallies["child"].records <= 4


def test_reports_have_required_columns_and_values(tmp_path):
    required = ["tap_name", "benchmark_status", "can_parallel_extraction_be_done", "recommended_worker_count",
                "worker_count", "total_streams", "successful_streams", "failed_streams", "limited_streams",
                "total_records", "record_limit", "sequential_duration_seconds", "execution_duration_seconds",
                "improvement_percentage", "speedup", "throughput_records_per_second", "max_observed_concurrency",
                "http_429_count", "throttling_count", "retry_count", "http_4xx_count", "http_5xx_count",
                "timeout_count", "extraction_error_count", "failed_stream_names", "error_summary", "start_time",
                "end_time"]
    assert set(required) <= set(CSV_COLUMNS)
    run = RunResult(tap_name="t", worker_count=1, started_ts=0, ended_ts=5, selected_streams=["a"],
                    units=[_unit("a", 0, 5, COMPLETED_WITH_LIMIT, limit=10, workers=1)])
    section = TapSection(tap_name="t", stream_results=build_stream_results(run))
    row = {"tap_name": "t", "worker_count": 1, "can_parallel_extraction_be_done": "YES", "speedup": None}
    section.rows.append(row)
    write_csv(str(tmp_path / "r.csv"), [section])
    write_stream_csv(str(tmp_path / "s.csv"), [section])
    rows = list(csv.DictReader(open(tmp_path / "r.csv", encoding="utf-8")))
    assert rows[0]["can_parallel_extraction_be_done"] == "YES" and rows[0]["speedup"] == ""
    stream_rows = list(csv.DictReader(open(tmp_path / "s.csv", encoding="utf-8")))
    assert stream_rows[0]["stream_status"] == COMPLETED_WITH_LIMIT and stream_rows[0]["record_limit"] == "10"


def test_render_table_alignment():
    lines = render_table(["A", "Long header"], [["1", "x"], ["22", "yy"]])
    assert len({len(line) for line in lines}) == 1


def test_results_store_latest_pointer_and_listing(tmp_path):
    results = str(tmp_path)
    for run_id in ("2026-01-01_10-00-00", "2026-01-02_10-00-00"):
        path = create_run_dir(results, run_id)
        with open(os.path.join(path, "execution_summary.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")
    with pytest.raises(FileExistsError):
        create_run_dir(results, "2026-01-01_10-00-00")
    assert list_runs(results) == ["2026-01-01_10-00-00", "2026-01-02_10-00-00"]
    write_latest(results, "2026-01-02_10-00-00", "completed")
    assert read_latest(results)["run_id"] == "2026-01-02_10-00-00"
    assert resolve_run(results, "latest").endswith("2026-01-02_10-00-00")
    with pytest.raises(FileNotFoundError):
        resolve_run(results, "nope")
