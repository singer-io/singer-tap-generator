"""End-to-end tests: the real benchmark workflow against a fake tap and a local HTTP API (no external services)."""
import csv
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import benchmark  # noqa: E402
import compare_runs  # noqa: E402

SECRET_PASSWORD = "FAKE-TEST-PASSWORD-not-a-secret"
SECRET_TOKEN = "FAKE-TEST-TOKEN-not-a-secret"


class FakeApi:
    """Local API: /streams/<name>?page=N. Behaviour per stream is configurable."""

    def __init__(self, streams):
        self.streams = streams
        self.lock = threading.Lock()
        self.requests = {}          # stream -> list of page numbers requested
        self.active = 0
        self.seen_429 = set()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # silence
                pass

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                name = parsed.path.rsplit("/", 1)[-1]
                page = int(parse_qs(parsed.query).get("page", ["0"])[0])
                spec = api.streams.get(name, {"pages": 1, "size": 10})
                with api.lock:
                    api.requests.setdefault(name, []).append(page)
                    others_active = api.active
                    api.active += 1
                try:
                    mode = spec.get("mode")
                    if mode == "429_first" and name not in api.seen_429:
                        api.seen_429.add(name)
                        return self._send(429, {"error": "slow down"}, {"Retry-After": "0"})
                    if mode == "fail500":
                        return self._send(500, {"error": "boom"})
                    if mode == "reject_concurrent" and others_active > 0:
                        return self._send(500, {"error": "concurrent access not allowed"})
                    time.sleep(spec.get("delay", 0.2))
                    size = spec.get("size", 10)
                    records = [{"id": page * size + i} for i in range(size)]
                    return self._send(200, {"records": records, "more": page < spec.get("pages", 1) - 1})
                finally:
                    with api.lock:
                        api.active -= 1

            def _send(self, status, body, headers=None):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _selected(name, parent=None, selected=True):
    meta = {"selected": selected}
    if parent:
        meta["parent-tap-stream-id"] = parent
    return {"tap_stream_id": name, "stream": name, "schema": {"type": "object"},
            "metadata": [{"breadcrumb": [], "metadata": meta}]}


def _hash_tree(path):
    digest = {}
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            full = os.path.join(root, name)
            with open(full, "rb") as handle:
                digest[full] = hashlib.sha256(handle.read()).hexdigest()
    return digest


class Env:
    """A throwaway workspace: fake tap repo + registry + benchmark config + results/logs folders."""

    def __init__(self, tmp_path, api_url, catalog_streams, bench_config=None, entry_point="fake_tap:main"):
        self.root = tmp_path
        self.repo = tmp_path / "ws" / "fake-tap"
        for sub in ("tmp/configs", "tmp/commands", "tmp/catalogs", "tmp/outputs"):
            (self.repo / sub).mkdir(parents=True)
        (self.repo / "tmp/configs/config.json").write_text(json.dumps(
            {"base_url": api_url, "start_date": "2026-01-01T00:00:00Z", "password": SECRET_PASSWORD,
             "api_token": SECRET_TOKEN}))
        (self.repo / "tmp/commands/sync_run_command.txt").write_text(
            "fake-tap --config ./tmp/configs/config.json --catalog ./tmp/catalogs/catalog.json > ./tmp/outputs/output.json\n")
        (self.repo / "tmp/catalogs/catalog.json").write_text(json.dumps({"streams": catalog_streams}))
        (self.repo / "tmp/outputs/output.json").write_text("EXISTING OUTPUT MUST SURVIVE\n")
        self.registry = tmp_path / "registry.json"
        self.registry.write_text(json.dumps({"taps": {
            "fake-tap": {
                "repo_path": str(self.repo), "enabled": True, "config_path": "tmp/configs/config.json",
                "sync_command_path": "tmp/commands/sync_run_command.txt", "catalog_path": "tmp/catalogs/catalog.json",
                "python_path": sys.executable, "entry_point": entry_point,
                "extra_env": {"PYTHONPATH": os.path.join(HERE, "fake_tap_support")},
                "required_config_keys": ["base_url"], "aliases": ["fake"],
                "isolation_findings": ["fake tap: stateless streams"]},
            "bad-tap": {
                "repo_path": str(tmp_path / "ws" / "missing"), "enabled": False, "known_invalid_config": True,
                "skip_reason": "invalid on purpose", "config_path": "x.json", "sync_command_path": "s.txt",
                "catalog_path": "c.json"}}}))
        self.config = tmp_path / "bench.json"
        data = {"workers": [1, 2], "enabled_taps": ["fake"], "timeout_seconds": 120}
        data.update(bench_config or {})
        self.config.write_text(json.dumps(data))
        self.results = tmp_path / "results"
        self.logs = tmp_path / "logs"

    def run(self, *extra, run_id=None):
        argv = ["--config", str(self.config), "--registry", str(self.registry), "--results-dir", str(self.results),
                "--logs-dir", str(self.logs)] + list(extra)
        if run_id:
            argv += ["--run-id", run_id]
        return benchmark.main(argv)

    def latest_run_dir(self):
        latest = json.loads((self.results / "latest.json").read_text())
        return self.results / latest["run_id"]

    @staticmethod
    def rows(path):
        with open(path, encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def by_workers(self, path):
        """Report rows of the fake tap keyed by worker count."""
        return {int(r["worker_count"]): r for r in self.rows(path) if r["tap_name"] == "fake-tap"}


@pytest.fixture
def api():
    server = FakeApi({
        "a": {"pages": 3}, "b": {"pages": 2}, "c": {"pages": 5}, "d": {"pages": 1}, "leaky": {"pages": 1},
        "bad": {"pages": 1, "mode": "fail500"}, "bad2": {"pages": 1, "mode": "fail500"},
        "bad3": {"pages": 1, "mode": "fail500"}, "bad4": {"pages": 1, "mode": "fail500"},
        "throttled": {"pages": 2, "mode": "429_first"},
        "fragile": {"pages": 12, "mode": "reject_concurrent"}, "slow1": {"pages": 12}, "slow2": {"pages": 4}})
    server.start()
    yield server
    server.stop()


BASIC = [_selected("a"), _selected("a_child", "a"), _selected("b"), _selected("c"), _selected("d"),
         _selected("leaky"), _selected("x", selected=False)]


def test_full_workflow_yes_artifacts_and_no_tap_changes(tmp_path, api):
    env = Env(tmp_path, api.url, BASIC)
    before = _hash_tree(str(env.repo))
    assert env.run("--workers", "1", "2", "--taps", "fake", "bad-tap", run_id="2026-01-01_00-00-00") == 0
    assert _hash_tree(str(env.repo)) == before              # existing tap files untouched
    run_dir = env.results / "2026-01-01_00-00-00"
    for name in ("parallelism_report.csv", "parallelism_report.txt", "stream_level_results.csv", "errors.csv",
                 "execution_summary.json", "config_used.json"):
        assert (run_dir / name).is_file(), name
    rows = env.rows(run_dir / "parallelism_report.csv")
    with open(run_dir / "parallelism_report.csv", encoding="utf-8") as handle:
        header = handle.readline().strip().split(",")
    assert header[:9] == ["tap_name", "can_parallel_extraction_be_done", "max_workers_successfully_tested",
                          "max_recommended_workers_without_rate_limit", "worker_count", "rate_limit_encountered",
                          "extraction_status", "total_records", "execution_duration_seconds"]
    tap_rows = {int(r["worker_count"]): r for r in rows if r["tap_name"] == "fake-tap"}
    assert set(tap_rows) == {1, 2}
    assert all(r["can_parallel_extraction_be_done"] in ("YES", "NO") for r in rows)
    assert tap_rows[2]["can_parallel_extraction_be_done"] == "YES" and tap_rows[2]["worker_count_safe"] == "YES"
    assert int(tap_rows[2]["recommended_worker_count"]) == 2 and tap_rows[1]["worker_count_safe"] == "BASELINE"
    assert tap_rows[2]["max_workers_successfully_tested"] == "2" and tap_rows[2]["evaluation_status"] == "CONCLUSIVE"
    assert tap_rows[2]["max_recommended_workers_without_rate_limit"] == "2"
    assert tap_rows[1]["rate_limit_encountered"] == "NO" and tap_rows[1]["extraction_status"] == "SUCCESS"
    assert int(tap_rows[2]["max_observed_concurrency"]) == 2 and int(tap_rows[1]["max_observed_concurrency"]) == 1
    assert tap_rows[1]["total_records"] == tap_rows[2]["total_records"] and tap_rows[1]["records_match_baseline"] == "BASELINE"
    assert tap_rows[2]["records_match_baseline"] == "MATCH" and float(tap_rows[2]["speedup"]) > 1.0
    assert tap_rows[1]["skipped_streams"] == "1" and tap_rows[1]["total_streams"] == "6"
    assert tap_rows[1]["benchmark_status"] == "COMPLETED" and tap_rows[1]["record_limit"] == "unlimited"
    bad = [r for r in rows if r["tap_name"] == "bad-tap"][0]
    assert bad["benchmark_status"] == "SKIPPED_INVALID_CONFIG" and bad["can_parallel_extraction_be_done"] == "NO"

    streams = env.rows(run_dir / "stream_level_results.csv")
    assert {r["stream_name"] for r in streams} == {"a", "a_child", "b", "c", "d", "leaky"}
    assert any(r["overlapped_with_other_stream"] == "YES" for r in streams if r["worker_count"] == "2")
    assert all(r["overlapped_with_other_stream"] == "NO" for r in streams if r["worker_count"] == "1")

    summary = json.loads((run_dir / "execution_summary.json").read_text())
    assert summary["totals"]["taps_parallel_yes"] == 1 and summary["totals"]["taps_skipped"] == 1
    assert json.loads((env.results / "latest.json").read_text())["run_id"] == "2026-01-01_00-00-00"
    merged = run_dir / "artifacts" / "fake-tap" / "workers_2" / "_output_workers_2.jsonl"
    types = [json.loads(line)["type"] for line in merged.read_text().splitlines()]
    assert types.count("RECORD") == 30 * 2 + 20 + 50 + 10 + 10        # a + a_child, b, c, d, leaky
    log = (env.logs / "fake-tap" / "2026-01-01_00-00-00" / "workers_2.log").read_text()
    assert "[TIMELINE]" in log and "[WORKER-2] START" in log and "max_observed=2" in log
    txt = (run_dir / "parallelism_report.txt").read_text()
    assert "Can Parallel Extraction Be Done? YES" in txt and "Recommended worker count                 : 2" in txt
    assert "Max workers successfully tested          : 2" in txt
    assert "SKIPPED_INVALID_CONFIG" in txt and "D. REASONS FOR NO DECISIONS" in txt

    # credentials the (fake) tap leaked to stderr must never reach logs or reports
    for root, _dirs, files in os.walk(str(tmp_path / "results")):
        for name in files:
            if name.endswith((".json", ".csv", ".txt", ".log", ".jsonl")):
                text = open(os.path.join(root, name), encoding="utf-8", errors="replace").read()
                assert SECRET_PASSWORD not in text and SECRET_TOKEN not in text, name
    assert any("<redacted>" in open(p, encoding="utf-8").read()
               for p in [str(f) for f in (run_dir / "artifacts" / "fake-tap" / "workers_1").glob("*leaky*stderr.log")])


def test_record_limits_hierarchy_enforced_identically_and_without_extra_requests(tmp_path, api):
    env = Env(tmp_path, api.url, [_selected("a"), _selected("a_child", "a"), _selected("b"), _selected("c"), _selected("d")],
              {"default_record_limit": 1000, "tap_record_limits": {"fake": 15},
               "stream_record_limits": {"fake-tap": {"c": 25}}})
    assert env.run("--workers", "1", "2", run_id="2026-01-02_00-00-00") == 0
    run_dir = env.results / "2026-01-02_00-00-00"
    streams = env.rows(run_dir / "stream_level_results.csv")
    for workers in ("1", "2"):
        by_name = {r["stream_name"]: r for r in streams if r["worker_count"] == workers}
        assert by_name["c"]["records_extracted"] == "25" and by_name["c"]["record_limit"] == "25"   # stream beats tap
        assert by_name["a"]["records_extracted"] == "15" and by_name["a"]["stream_status"] == "COMPLETED_WITH_LIMIT"
        assert by_name["b"]["records_extracted"] == "15" and by_name["b"]["record_limit"] == "15"   # tap beats default
        assert by_name["d"]["records_extracted"] == "10" and by_name["d"]["stream_status"] == "COMPLETED"
        assert int(by_name["a_child"]["records_extracted"]) <= 15
        assert by_name["a"]["api_requests_after_limit"] == "0"
    rows = env.by_workers(run_dir / "parallelism_report.csv")
    assert rows[1]["benchmark_status"] == "COMPLETED_WITH_LIMIT" and rows[1]["failed_streams"] == "0"
    assert rows[1]["limited_streams"] == rows[2]["limited_streams"] and rows[1]["total_records"] == rows[2]["total_records"]
    assert rows[1]["record_limit"].startswith("mixed")
    # c has 5 pages of 10 records; a limit of 25 needs pages 0..2 only: the tap was stopped before page 3 and 4
    assert max(api.requests["c"]) <= 2
    assert max(api.requests["a"]) <= 1 and max(api.requests["b"]) <= 1

    # CLI --record-limit overrides every config-file limit
    assert env.run("--workers", "1", "--record-limit", "5", run_id="2026-01-02_00-00-01") == 0
    cli_streams = env.rows(env.results / "2026-01-02_00-00-01" / "stream_level_results.csv")
    assert {r["stream_name"]: r["records_extracted"] for r in cli_streams if r["stream_name"] in ("a", "b", "c")} == \
        {"a": "5", "b": "5", "c": "5"}


def test_failed_stream_is_reported_and_is_not_blamed_on_parallelism(tmp_path, api):
    env = Env(tmp_path, api.url, [_selected("a"), _selected("bad"), _selected("b")])
    assert env.run(run_id="2026-01-03_00-00-00") == 0
    run_dir = env.results / "2026-01-03_00-00-00"
    rows = env.by_workers(run_dir / "parallelism_report.csv")
    assert rows[1]["benchmark_status"] == "PARTIAL" and rows[1]["failed_stream_names"] == "bad"
    assert rows[1]["successful_streams"] == "2" and rows[1]["failed_streams"] == "1"
    assert rows[2]["can_parallel_extraction_be_done"] == "YES"          # fails identically at 1 worker: not a parallel problem
    errors = env.rows(run_dir / "errors.csv")
    assert any(e["category"] == "SERVER_ERROR" and e["http_status"] == "500" for e in errors)
    assert "CONSISTENT" in (run_dir / "parallelism_report.txt").read_text()
    stream = [r for r in env.rows(run_dir / "stream_level_results.csv") if r["stream_name"] == "bad"][0]
    assert stream["stream_status"] == "FAILED" and stream["error_category"] == "SERVER_ERROR"


def test_stream_that_only_fails_under_concurrency_gives_NO(tmp_path, api):
    env = Env(tmp_path, api.url, [_selected("fragile"), _selected("slow1")])
    assert env.run(run_id="2026-01-04_00-00-00") == 0
    run_dir = env.results / "2026-01-04_00-00-00"
    rows = env.by_workers(run_dir / "parallelism_report.csv")
    assert rows[1]["benchmark_status"] == "COMPLETED"
    assert rows[2]["failed_stream_names"] == "fragile" and rows[2]["worker_count_safe"] == "NO"
    assert rows[2]["can_parallel_extraction_be_done"] == "NO" and rows[2]["recommended_worker_count"] == "0"
    # the 2-worker run is faster only because it failed a stream: no speedup may be reported for it
    assert rows[2]["speedup"] == "" and rows[2]["improvement_percentage"] == ""
    assert "not like-for-like" in rows[2]["notes"] and rows[1]["extraction_status"] == "SUCCESS"
    assert rows[2]["extraction_status"] == "PARTIAL"
    txt = (run_dir / "parallelism_report.txt").read_text()
    assert "streams_complete" in txt and "PARALLEL-ONLY" in txt and "Can Parallel Extraction Be Done? NO" in txt


def test_recovered_429_is_tracked_but_does_not_force_NO(tmp_path, api):
    env = Env(tmp_path, api.url, [_selected("throttled"), _selected("b")])
    assert env.run(run_id="2026-01-05_00-00-00") == 0
    rows = env.by_workers(env.results / "2026-01-05_00-00-00" / "parallelism_report.csv")
    assert int(rows[1]["http_429_count"]) >= 1 and rows[1]["benchmark_status"] == "COMPLETED"
    assert rows[1]["rate_limit_encountered"] == "YES" and rows[2]["rate_limit_encountered"] == "NO"
    assert rows[1]["extraction_status"] == "SUCCESS"
    # the baseline itself met the rate limit, so no worker count can be called rate-limit free
    assert rows[2]["max_recommended_workers_without_rate_limit"] == "0"
    assert rows[2]["max_workers_successfully_tested"] == "2"
    assert int(rows[1]["retry_count"]) >= 1
    assert rows[2]["can_parallel_extraction_be_done"] == "YES" and rows[2]["worker_count_safe"] == "YES"


def test_cli_overrides_config_and_results_are_never_overwritten(tmp_path, api):
    env = Env(tmp_path, api.url, [_selected("d"), _selected("b")], {"workers": [1, 2]})
    assert env.run("--workers", "1", "3", run_id="2026-01-06_00-00-00") == 0
    rows = [r for r in env.rows(env.results / "2026-01-06_00-00-00" / "parallelism_report.csv") if r["tap_name"] == "fake-tap"]
    assert sorted({int(r["worker_count"]) for r in rows}) == [1, 3]
    first_snapshot = _hash_tree(str(env.results / "2026-01-06_00-00-00"))
    assert env.run("--workers", "1", "2", run_id="2026-01-06_00-00-00") == 2          # refuses to reuse the folder
    assert _hash_tree(str(env.results / "2026-01-06_00-00-00")) == first_snapshot
    assert env.run("--workers", "1", "2", run_id="2026-01-06_00-00-01") == 0
    assert json.loads((env.results / "latest.json").read_text())["run_id"] == "2026-01-06_00-00-01"
    assert _hash_tree(str(env.results / "2026-01-06_00-00-00")) == first_snapshot       # older run untouched
    assert compare_runs.main(["--results-dir", str(env.results), "--list"]) == 0
    assert compare_runs.main(["--results-dir", str(env.results), "2026-01-06_00-00-00", "latest"]) == 0


def test_longest_first_schedule_keeps_results_in_catalog_order(tmp_path, api):
    env = Env(tmp_path, api.url, [_selected("d"), _selected("c"), _selected("b")])
    assert env.run("--workers", "1", "2", "--schedule", "longest-first", run_id="2026-01-07_00-00-00") == 0
    rows = env.by_workers(env.results / "2026-01-07_00-00-00" / "parallelism_report.csv")
    assert "longest-first" in rows[2]["notes"] and "longest-first" not in rows[1]["notes"]
    log = (env.logs / "fake-tap" / "2026-01-07_00-00-00" / "workers_2.log").read_text()
    queued = [line.split("unit=")[1].split()[0] for line in log.splitlines() if "[SCHEDULER] queued" in line]
    assert queued[0] == "c"                                   # c has the most pages, so it is handed out first
    summary = json.loads((env.results / "2026-01-07_00-00-00" / "execution_summary.json").read_text())
    assert summary["configuration"]["schedule_order"] == "longest_first"


def test_broken_environment_is_reported_as_environment_not_as_a_parallelism_verdict(tmp_path, api):
    """The real-world failure: a tap whose virtualenv cannot start it must be SKIPPED_ENVIRONMENT, never NO."""
    env = Env(tmp_path, api.url, BASIC, entry_point="module_that_is_not_installed:main")
    assert env.run("--workers", "1", "2", run_id="2026-01-08_00-00-00") == 0
    run_dir = env.results / "2026-01-08_00-00-00"
    rows = env.rows(run_dir / "parallelism_report.csv")
    row = [r for r in rows if r["tap_name"] == "fake-tap"][0]
    assert row["benchmark_status"] == "SKIPPED_ENVIRONMENT" and row["extraction_status"] == "SKIPPED"
    assert row["evaluation_status"] == "NOT_EVALUATED" and row["decision_basis"] == "NOT_EVALUATED_ENVIRONMENT"
    assert "pip install -e" in row["error_summary"] and "module_that_is_not_installed" in row["error_summary"]
    assert not (env.logs / "fake-tap").exists()                 # nothing was executed
    assert api.requests == {}                                   # and no API call was made
    assert "NOT_EVALUATED" in (run_dir / "parallelism_report.txt").read_text()


def test_environment_failure_during_the_run_discards_the_tap_results(tmp_path, api):
    env = Env(tmp_path, api.url, [_selected("envbreak"), _selected("d"), _selected("b")])
    assert env.run("--workers", "1", "2", run_id="2026-01-08_00-00-01") == 0
    run_dir = env.results / "2026-01-08_00-00-01"
    row = [r for r in env.rows(run_dir / "parallelism_report.csv") if r["tap_name"] == "fake-tap"]
    assert len(row) == 1 and row[0]["benchmark_status"] == "SKIPPED_ENVIRONMENT"
    assert "stopped being runnable" in row[0]["error_summary"] and "missing_dependency" in row[0]["error_summary"]
    assert (env.logs / "fake-tap" / "2026-01-08_00-00-01" / "workers_1.log").is_file()
    assert not (env.logs / "fake-tap" / "2026-01-08_00-00-01" / "workers_2.log").exists()   # stopped immediately
    assert env.rows(run_dir / "stream_level_results.csv") == []


def test_failed_baseline_is_inconclusive_and_parallel_runs_are_not_attempted(tmp_path, api):
    env = Env(tmp_path, api.url, [_selected("bad"), _selected("bad2"), _selected("bad3"), _selected("bad4")])
    assert env.run("--workers", "1", "2", "3", run_id="2026-01-08_00-00-02") == 0
    run_dir = env.results / "2026-01-08_00-00-02"
    rows = env.by_workers(run_dir / "parallelism_report.csv")
    assert set(rows) == {1}                                     # no pointless higher-worker runs
    assert rows[1]["extraction_status"] == "FAILURE" and rows[1]["can_parallel_extraction_be_done"] == "NO"
    assert rows[1]["evaluation_status"] == "INCONCLUSIVE" and rows[1]["decision_basis"] == "NOT_EVALUATED_BASELINE_FAILED"
    assert "were NOT run" in (run_dir / "parallelism_report.txt").read_text()
    assert "stopped after the first 3 work units" in rows[1]["notes"]
    assert "continue_on_stream_failure" not in rows[1]["notes"]     # the real reason is reported, not a wrong one
    assert sorted(p.name for p in (env.logs / "fake-tap" / "2026-01-08_00-00-02").iterdir()) == ["workers_1.log"]
    assert "INCONCLUSIVE" in (run_dir / "parallelism_report.txt").read_text()
    assert api.requests.get("bad4") is None                     # fail-fast: the 4th stream was never requested


def test_stream_sampling_is_identical_for_every_worker_count_and_flagged(tmp_path, api, capsys):
    env = Env(tmp_path, api.url, [_selected("a"), _selected("b"), _selected("c"), _selected("d")],
              {"tap_max_streams": {"fake": 3}})
    assert env.run("--dry-run") == 0
    out = capsys.readouterr().out
    assert "benchmarked=3" in out and "SAMPLED (config-tap)" in out and "planned runs: 2 worker count(s)" in out
    assert env.run("--workers", "1", "2", "--max-streams", "2", run_id="2026-01-08_00-00-03") == 0   # CLI beats the file
    run_dir = env.results / "2026-01-08_00-00-03"
    rows = env.by_workers(run_dir / "parallelism_report.csv")
    assert rows[1]["total_streams"] == rows[2]["total_streams"] == "2"
    assert {r["stream_name"] for r in env.rows(run_dir / "stream_level_results.csv")} == {"a", "b"}
    assert "SAMPLED" in (run_dir / "parallelism_report.txt").read_text()
    assert env.run("--workers", "1", "--all-streams", run_id="2026-01-08_00-00-04") == 0
    assert env.by_workers(env.results / "2026-01-08_00-00-04" / "parallelism_report.csv")[1]["total_streams"] == "4"


def test_same_tap_gives_the_same_conclusion_in_a_different_folder(tmp_path, api):
    """Reproducibility: the verdict depends on the tap's behaviour, not on where the framework/tap live."""
    outcomes = []
    for name in ("workspace_one", "deeply/nested/workspace_two"):
        folder = tmp_path / name
        folder.mkdir(parents=True)
        env = Env(folder, api.url, [_selected("a"), _selected("b"), _selected("c"), _selected("d")])
        assert env.run("--workers", "1", "2", run_id="2026-01-09_00-00-00") == 0
        rows = env.by_workers(env.results / "2026-01-09_00-00-00" / "parallelism_report.csv")
        outcomes.append([(w, r["can_parallel_extraction_be_done"], r["max_workers_successfully_tested"],
                          r["extraction_status"], r["total_records"], r["successful_streams"], r["evaluation_status"])
                         for w, r in sorted(rows.items())])
    assert outcomes[0] == outcomes[1] and outcomes[0][1][1] == "YES"


def test_invalid_configuration_is_rejected_with_exit_code_2(tmp_path, api, capsys):
    env = Env(tmp_path, api.url, [_selected("d")], {"workers": [0]})
    assert env.run() == 2
    assert "worker count" in capsys.readouterr().err
    env.config.write_text(json.dumps({"workers": [1], "enabled_taps": ["nope"]}))
    assert env.run() == 2
    assert "unknown tap" in capsys.readouterr().err
    assert not env.results.exists() or not list(env.results.iterdir())
