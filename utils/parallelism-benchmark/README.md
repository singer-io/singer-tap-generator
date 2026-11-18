# Singer Tap Parallelism Benchmark

A standalone, reusable harness that answers three questions for any Singer tap **without modifying the tap**:

1. **Can this tap safely extract several streams in parallel?** (`YES` / `NO`)
2. **How much faster is parallel extraction?** (speedup, improvement %, throughput)
3. **What is the highest worker count that is still safe** before rate limiting, throttling, errors or data
   differences outweigh the gain? (`recommended_worker_count`)

Contents: [1 Introduction](#1-introduction) · [2 Prerequisites](#2-prerequisites) · [3 Workspace layout](#3-workspace-directory-structure) ·
[4 Tap configuration](#4-tap-configuration-setup) · [5 Framework configuration](#5-framework-configuration) ·
[6 Installation](#6-installation) · [7 How to run](#7-how-to-run) · [8 Understanding the results](#8-understanding-the-results) ·
[9 Troubleshooting](#9-troubleshooting) · [10 Adding a tap](#10-adding-a-new-tap) · [11 Limitations](#11-limitations-and-future-improvements)

---

## 1. Introduction

### What it does
For every enabled tap the framework

1. validates the tap's existing configuration/catalog (offline, no API calls),
2. splits the **selected streams** of the existing catalog into independent *work units*,
3. runs those units with 1, 2, 3, … **workers** (a worker is a slot that runs one stream at a time),
4. records when every stream started and ended, how many records it produced, every HTTP status, retry, throttle signal and error,
5. compares each parallel run with the 1-worker run, applies safety checks, and writes CSV / TXT / JSON reports.

### Why benchmarking is needed
Streams of a tap are usually run one after another. Running them concurrently can be much faster, but it can also hit
API rate limits, share state that is not thread-safe, or corrupt output. The benchmark turns "should we parallelise this tap?"
into evidence: measured speedup, observed concurrency, error/throttle counts, and a per-worker-count safety verdict.

### How sequential and parallel runs are compared
* The **sequential baseline** is the same mechanism with one worker (not a different code path), so both sides pay the same
  per-process start-up cost. `--native-baseline` additionally records the tap's own unmodified single-process sync for reference.
* Every worker count extracts the **same selected streams** with the **same record limits**, from `start_date` (no state file is
  passed), against the same config. Record counts per stream are compared with the baseline (`records_match_baseline`).
* `improvement % = (sequential − current) / sequential × 100`, `speedup = sequential / current`,
  `throughput = records / wall-clock seconds`. Wall-clock covers extraction only (merging and reporting are excluded).

### How stream-level parallelism is achieved (and why the taps are not patched)
Inspecting the taps showed that in-process threading is unsafe (e.g. `tap_jira.context.Context` is a class-level singleton; every tap
shares one client/session/state dict and writes to one stdout). Instead the harness uses **process isolation**:

```
 catalog (selected streams, selection preserved)
        |  plan: a stream + its child streams = one work unit (stream dependencies kept together)
        v
 queue of work units --> worker 1 -> tap process (unit A)  -> own stdout / stderr / HTTP-event files
                     --> worker 2 -> tap process (unit B)  -> own stdout / stderr / HTTP-event files
                     --> worker N -> tap process (unit C)  -> ...
        |
        v
 controlled merge (schedule order, units never interleaved) -> one valid Singer JSONL + one combined STATE
```
* Each unit runs the tap's **own console entry point** (the command is parsed from its `sync_run_command.txt`; only the catalog
  path is re-targeted) with a trimmed catalog. This is **not** "the whole sync run N times": each stream is extracted exactly once per run.
* Authentication, HTTP session, API client, pagination state, rate-limit counters and output file are per process, so one stream cannot
  interfere with another. *Client-side rate limiters are per process too* – N workers can send N× the single-process request rate, which is
  exactly what the 429/throttle counters measure.
* A small launcher (`worker_support/worker_runner.py`, run by the tap's own Python) instruments the process *at runtime*: it wraps
  `requests.Session.send` (status, duration, rate-limit headers, redacted URL, short error body), hooks the `backoff` logger (retries) and
  enforces record limits. Nothing on disk in a tap repository is touched.

### What the final YES/NO means
`can_parallel_extraction_be_done = YES` means: in the benchmark, **at least one parallel worker count passed every safety check** (section 8.4):
real overlap observed, every stream that worked sequentially also worked in parallel, merged output is consistent, no data loss versus
sequential, no extra timeouts and rate limiting (if any) was recovered by the tap's own retries. `NO` means no tested parallel worker count
passed (or the tap was skipped / not evaluable) – the reasons are listed explicitly. `YES` does not mean parallelism is *worthwhile*;
check `recommended_worker_count` (`1` = viable but no measurable gain) and `speedup`.

### What it does not do
It does **not** implement permanent parallelism inside any tap, change any tap file, config, catalog, command or output, or tune the taps.
It only measures. Salesforce stays excluded while its configuration is invalid.

---

## 2. Prerequisites

| Item | Requirement (verified from the repositories) |
|---|---|
| Python for the framework | Developed and tested on **Python 3.12.9**. Standard library only; no code uses syntax newer than 3.8, but only 3.12 was tested. |
| Python for each tap | Each tap must have its own virtualenv at `<repo>/venv` (`venv\Scripts\python.exe` on Windows, `venv/bin/python` on Linux/macOS) with the tap installed. All current venvs are Python 3.12.9. The framework runs the tap with *that* interpreter. |
| Tap dependencies (from each `setup.py`) | **tap-jira**: singer-python 6.8.0, requests 2.34.2, dateparser · **tap-oracle-fusion**: singer-python 6.8.0, requests 2.34.2, backoff 2.2.1 · **tap-sap-success-factors**: singer-python 6.8.0, requests 2.34.2, backoff 2.2.1, python-dateutil 2.9.0.post0, lxml 6.1.3, signxml 5.1.0 · **tap-activecampaign**: singer-python 6.8.0, requests 2.34.2, backoff 2.2.1, pyhumps 3.8.0 · tap-salesforce: singer-python 6.8.0, requests 2.33.0, xmltodict 1.0.2 |
| Operating system | Developed on Windows 10/11 (PowerShell). Process trees are stopped with `taskkill /T` on Windows and a process group (`SIGKILL`) on Linux/macOS (the POSIX path is implemented but was not exercised in this environment). |
| Singer SDK | `singer-python==6.8.0` in every tap (catalog `metadata` with `selected`, `parent-tap-stream-id`; `singer.write_record`). Other SDK generations are untested. |
| Network | Outbound HTTPS to each tap's API (credentials come from the tap's own config file). |
| Test tooling (optional) | `pytest`, `requests`, `pylint` – see `requirements.txt`. |

A tap's virtualenv is created the way the repository documents it, e.g. `python -m venv venv` then `pip install -e .` from the tap repository.

---

## 3. Workspace directory structure

The framework folder sits **next to** the tap repositories (they are discovered through the registry, relative to the framework's parent folder):

```text
workspace/                                  <- "workspace root" = parent of parallelism-benchmark/
├── tap-jira/                 tmp/{configs,commands,catalogs,outputs,scripts,states}/  venv/
├── tap-oracle-fusion/        tmp/...                                                  venv/
├── tap-sap-success-factors/  tmp/...                                                  venv/
├── tap-activecampaign/       tmp/...                                                  venv/
├── tap-salesforce/           tmp/...   (excluded: invalid configuration)
└── parallelism-benchmark/
    ├── benchmark.py  config.py  tap_registry.py  catalog_utils.py  executor.py  metrics.py
    ├── decision.py  error_analyzer.py  report_generator.py  output_merger.py  results_store.py
    ├── adapters.py  compare_runs.py  requirements.txt  README.md
    ├── worker_support/worker_runner.py        # launcher run inside each tap's venv
    ├── configs/benchmark_config.json          # run settings (workers, taps, limits, timeouts, ...)
    ├── configs/tap_registry.json              # one entry per tap (paths, adapter, findings, ...)
    ├── results/<YYYY-MM-DD_HH-MM-SS>/         # one folder per run (never overwritten) + results/latest.json
    ├── logs/<tap>/<run_id>/workers_<N>.log    # raw per-tap, per-worker-count logs
    └── tests/                                 # unit + integration tests (fake tap + local HTTP API)
```

**How taps are found:** `configs/tap_registry.json` lists every tap. A relative `repo_path` (default: the tap name) is resolved against the workspace
root; an absolute path works anywhere. To move a tap, change its `repo_path`. Use another registry with `--registry path\to\registry.json`.
Check what the framework sees with `python benchmark.py --list-taps` and `python benchmark.py --dry-run`.

---

## 4. Tap configuration setup

The framework distinguishes three kinds of files – **only the registry entry ties them together, and none of the tap files are ever modified**:

| Kind | Where | Purpose |
|---|---|---|
| **Tap-specific configuration** | `<repo>/tmp/configs/config.json` | The tap's own credentials/settings (what the tap reads via `--config`). Read by the framework only to validate required keys and to collect secret values for log scrubbing. |
| **Tap-specific command files** | `<repo>/tmp/commands/sync_run_command.txt`, `discovery_run_command.txt` | The commands the team already uses. The sync command is parsed to learn the executable name and the `--config` / `--catalog` (or `--properties`) flags; the redirect (`> output.json`) is dropped. |
| **Framework central configuration** | `parallelism-benchmark/configs/benchmark_config.json` (+ `tap_registry.json`) | Which taps, worker counts, record limits, timeouts, thresholds. |

Existing files per tap in this workspace (all paths are relative to the tap repository; verify with `--dry-run`):

| Tap | Config | Discovery cmd | Sync cmd | Existing catalog | Notes |
|---|---|---|---|---|---|
| tap-jira | `tmp/configs/config.json` | `tmp/commands/discovery_run_command.txt` | `tmp/commands/sync_run_command.txt` (uses `--properties`) | `tmp/catalogs/catalog.json` (15 streams, all selected) | basic-auth keys (`username`, `password`, `base_url`, …) |
| tap-oracle-fusion | `tmp/configs/config.json` | same names | same names (uses `--catalog`) | `tmp/catalogs/catalog.json` (43 streams, **14 selected** – streams that error in discovery were deselected on purpose) | **the existing catalog is the source of truth**; deselected streams are reported as `skipped_streams` and never run |
| tap-sap-success-factors | `tmp/configs/config.json` | same names | same names | `tmp/catalogs/catalog.json` (416 streams, all selected) | OAuth keys (`client_id`, `private_key`, …) |
| tap-activecampaign | `tmp/configs/config.json` (`api_url`, `api_token`, `start_date`, `user_agent`) | same names | same names | `tmp/catalogs/catalog.json` (59 streams, all selected) | 5 req/s limiter inside the tap |
| tap-salesforce | `tmp/configs/config.json` | same names | same names | `tmp/catalogs/catalog.json` is **empty** | excluded (`SKIPPED_INVALID_CONFIG`) |

Other files you will see (`config_jira.json`, `integration_tests_*_command.txt`, `state_run_command.txt`, `memory_profiler_command.txt`, `scripts/*`):
* `config_jira.json` holds the Jira (ticketing) token used by team tooling, **not a tap config**. The framework never passes it to a tap; it only reads the
  values so they can be removed from logs.
* The `integration_tests_*` files are not used. (They contain plain-text credentials – do not keep secrets there.)
* `tmp/scripts/add_selected.py` is reused only when a tap is set to `catalog_strategy: "discover_if_missing"`.

### Tap-specific differences handled by the registry (no code changes)
| Setting | Meaning | Example |
|---|---|---|
| `adapter` | execution adapter (`process_isolation`) | all taps |
| `config_overrides` | benchmark-only changes applied to a **temporary copy** of the config (deleted afterwards) | Oracle: `bicc_force_full_sync: true` so reruns do a full extract instead of reusing a server-side BICC job |
| `independent_streams` | catalog child streams that the tap does **not** emit via the parent, scheduled on their own | ActiveCampaign: `email_activities` |
| `streams_per_task` | batch this many *unlimited* streams per process to amortise start-up/authentication (streams with a record limit always get their own process) | SAP: 10 |
| `entry_point` | `module:function` to call instead of a console script | tests only |
| `catalog_strategy` | `preserve_existing` (default) or `discover_if_missing` | Salesforce after it is fixed |
| `rate_limit_info` | documented limit (leave `UNKNOWN` unless documented – detected headers are reported anyway) | ActiveCampaign: 5 req/s |
| `isolation_findings` | evidence from code inspection, printed in the report | all taps |
| `known_invalid_config`, `skip_reason`, `enabled` | exclusion | Salesforce |
| `extra_env`, `python_path`, `parent_metadata_keys`, `stream_include`, `stream_exclude`, `required_config_keys` | misc. |  |

### Verifying that a tap's files are valid
```bash
python benchmark.py --dry-run                         # all taps: config JSON, required keys, catalog, venv python, selected/excluded streams, work units, resolved record limits
python benchmark.py --dry-run --taps tap-activecampaign
```
A tap that fails these offline checks is reported with the exact reason (`config file missing`, `config missing required keys: …`, `existing catalog is empty`, `no virtualenv python found`).
To verify credentials, run the tap's own discovery command (`tmp/commands/discovery_run_command.txt`) – the benchmark does not discover when a catalog exists.
Never overwrite the existing `catalog.json` or `output.json`: generate new files elsewhere; the framework writes only inside `results/` and `logs/`.

---

## 5. Framework configuration

`configs/benchmark_config.json` (complete working example – this is the shipped file):

```json
{
  "workers": [1, 2, 3, 4],
  "enabled_taps": ["tap-jira", "tap-oracle-fusion", "tap-sap-success-factor", "tap-activecampaign"],
  "timeout_seconds": 1800,
  "stream_timeout_seconds": 1800,
  "continue_on_stream_failure": true,
  "stop_on_unstable_run": false,
  "merge_output": true,
  "keep_unit_outputs": false,
  "native_baseline": false,
  "log_level": "INFO",
  "default_record_limit": null,
  "tap_record_limits":   { "tap-oracle-fusion": 500, "tap-activecampaign": 500 },
  "stream_record_limits": { "tap-jira": { "issues": 1000 }, "tap-activecampaign": { "contacts": 500 } },
  "max_429_ratio": 0.10,
  "record_tolerance_pct": 2.0,
  "recommendation_min_speedup": 1.2,
  "schedule_order": "catalog"
}
```
Optional keys: `registry_path`, `results_dir`, `logs_dir`. `record_limit` is accepted as an alias of `default_record_limit`. Unknown keys are rejected with a clear error
(typos never silently do nothing). Keys starting with `_` are comments. Tap names may use aliases (`tap-sap-success-factor` = `tap-sap-success-factors`).

| Setting | Meaning |
|---|---|
| `workers` | worker counts to test. `1` (the baseline) is always added. |
| `enabled_taps` | taps to benchmark. Registered taps that are disabled/invalid still get a `SKIPPED…` row. |
| `default_record_limit` / `tap_record_limits` / `stream_record_limits` | max records **per stream** (section 5.1). `null`/absent = unlimited. |
| `timeout_seconds` | wall-clock budget of one run (one tap × one worker count). Streams not started when it expires are `SKIPPED`; running ones are stopped. |
| `stream_timeout_seconds` | limit for one work unit; exceeding it gives `TIMED_OUT` for its streams. |
| `continue_on_stream_failure` | `true`: other streams keep running after a failure; `false`: stop scheduling new streams. |
| `stop_on_unstable_run` | do not try higher worker counts after a run with extra failures or any 429. |
| `merge_output` / `keep_unit_outputs` | merge per-stream outputs into `_output_workers_N.jsonl` / keep the per-stream files. |
| `native_baseline` | also run the tap's own single-process sync as a reference row (skipped when record limits apply). |
| `max_429_ratio` | a worker count is unsafe if HTTP 429 responses exceed this share of its requests (default 10%). |
| `record_tolerance_pct` | allowed record-count difference versus the baseline for streams that completed in both runs (default 2%, minimum 2 records). |
| `recommendation_min_speedup` | speedup below this is flagged as "not a worthwhile gain" (default 1.2×). |
| `schedule_order` | `catalog` (default): work units are handed to workers in catalog order, the same order the baseline uses. `longest_first` (CLI `--schedule longest-first`): parallel runs hand out the units that took longest in the sequential baseline first, which removes the "long stream starts last" tail (the baseline itself stays in catalog order). Use it when one stream dominates, e.g. ActiveCampaign `email_activities`. |
| `log_level`, `results_dir`, `logs_dir` | framework logging level and output locations. |

**Retry behaviour:** the framework deliberately does **not** retry failed streams (failures must stay visible). Retries are the tap's own
(`backoff` and similar): they are observed and reported as `retry_count`, `http_429_count`, `throttling_count`, and decide whether rate limiting counts as *recovered*.

### 5.1 Record limits
Resolution order for a stream (highest first; the source of each value is shown by `--dry-run` and recorded in `execution_summary.json`):

1. CLI `--stream-record-limit TAP:STREAM=N`
2. CLI `--tap-record-limit TAP=N`
3. CLI `--record-limit N` (a uniform limit that **overrides every limit in the config file**; `--no-record-limit` removes them)
4. config `stream_record_limits[tap][stream]`
5. config `tap_record_limits[tap]`
6. config `default_record_limit`
7. unlimited

How a limit is enforced (no tap change): a runtime wrapper on the tap's stdout counts `RECORD` messages **inside the tap process** and stops the process in the very write
that emits the limit-th record, so no further API page is requested; the parent also counts on the pipe, drops records beyond a child stream's own limit and is the source of truth for the reports.
For a unit with a parent and children, reaching the *parent's* limit stops the unit (children are produced by the parent). Stream status becomes
`COMPLETED_WITH_LIMIT` – **a limited stream is not a failure**. A stream with *exactly* `limit` records is also reported as limited (the tap is stopped before it can confirm it had no more).
`api_requests_after_limit` (stream CSV) counts requests started after the limit was reached – it should be `0`.
Limits are applied identically to every worker count, so all runs extract comparable datasets. **Caveat:** for job-based extracts (Oracle BICC) the server-side extraction
runs before any row is returned, so the limit shortens only the post-download work, not the extraction job.

### 5.2 Precedence between CLI and file
Defaults < `benchmark_config.json` < command line. Every value's origin is stored under `configuration.value_sources` in `execution_summary.json`.
Example: file says `"workers": [1, 2]`, command `--workers 1 3 4` ⇒ `[1, 3, 4]`.

---

## 6. Installation

```bash
cd parallelism-benchmark

python -m venv .venv
# Windows (PowerShell):   .venv\Scripts\Activate.ps1
# Windows (cmd):          .venv\Scripts\activate.bat
# Linux / macOS:          source .venv/bin/activate

pip install -r requirements.txt     # only needed to run the tests / lint; the framework itself needs no packages
```
Nothing else is required: each tap already has its own `venv` with the tap installed (`pip install -e .` inside the tap repository, as in its own setup notes).
Run the self-tests once: `python -m pytest tests -q` (about 1½ minutes; uses a fake tap and a local HTTP server, no external service).

---

## 7. How to run

```bash
# configured taps, worker counts and record limits from configs/benchmark_config.json
python benchmark.py

# explicit worker counts (override the config file)
python benchmark.py --workers 1 2 3 4
python benchmark.py --workers 1 2

# one tap
python benchmark.py --taps tap-activecampaign --workers 1 2 3
python benchmark.py --taps tap-jira --workers 1 2

# Oracle Fusion with a small record limit (overrides every config-file limit)
python benchmark.py --taps tap-oracle-fusion --workers 1 2 --record-limit 100

# override the limit of everything / of one tap / of one stream
python benchmark.py --record-limit 500
python benchmark.py --tap-record-limit tap-activecampaign=200 --stream-record-limit tap-activecampaign:contacts=50
python benchmark.py --no-record-limit --taps tap-jira

# smoke test / inspection
python benchmark.py --dry-run                              # plan, resolved limits and their sources, no execution
python benchmark.py --taps tap-sap-success-factors --max-streams 8 --workers 1 2
python benchmark.py --taps tap-activecampaign --schedule longest-first   # start the slowest streams first
python benchmark.py --native-baseline --workers 1 2        # adds the tap's own single-process run (when no limits apply)

# other useful options
python benchmark.py --timeout 3600 --stream-timeout 600 --stop-on-failure --stop-on-unstable
python benchmark.py --results-dir D:\bench\results --logs-dir D:\bench\logs --log-level DEBUG --run-id my-run
python compare_runs.py --list
python compare_runs.py 2026-11-18_16-30-00 latest --tap tap-jira
```
Exit codes: `0` finished, `2` invalid configuration/arguments (nothing is created), `130` interrupted (partial reports are still written, `latest.json` is not moved).
A run folder is never reused: an existing `--run-id` is refused.

---

## 8. Understanding the results

### 8.1 Where results are stored
```text
results/
├── latest.json                       # {"run_id": "...", ...}: the most recent successful run (portable; no symlinks)
├── 2026-11-18_16-30-00/              # one folder per execution; older folders are never modified
│   ├── parallelism_report.csv        # consolidated: one row per tap × worker count
│   ├── parallelism_report.txt        # human-readable report
│   ├── stream_level_results.csv      # one row per stream × worker count
│   ├── errors.csv                    # every HTTP error / throttle signal / failed stream
│   ├── execution_summary.json        # machine-readable summary (config incl. value sources, decisions, rows, checks)
│   ├── config_used.json
│   └── artifacts/<tap>/              # _catalog.json (working copy), _unit_catalogs/, workers_<N>/ (merged Singer output, per-unit stderr + HTTP events)
└── _archive_prototype_runs/          # results of the earlier prototype (kept, not part of the current layout)
logs/<tap>/<run_id>/workers_<N>.log   # scheduling, worker assignment, start/end, HTTP errors, retries, throttling, failures, timeline, concurrency summary
```
`latest.json` is updated only when the run was not interrupted and at least one tap produced results. Use `python compare_runs.py RUN_A RUN_B` (run ids, paths or `latest`) to compare two runs.

### 8.2 Consolidated CSV (`parallelism_report.csv`)
One row per tap and worker count (plus an optional `native_sequential` row, plus one row for each skipped tap). Key columns:

| Column | Meaning |
|---|---|
| `benchmark_status` | **per row**: `COMPLETED`, `COMPLETED_WITH_LIMIT`, `PARTIAL`, `FAILED`, `TIMED_OUT`, `SKIPPED`, `SKIPPED_INVALID_CONFIG` |
| `can_parallel_extraction_be_done`, `recommended_worker_count`, `decision_basis`, `decision_summary` | **tap-level**: the same value on every row of a tap (`decision_scope` says so). `recommended_worker_count`: `0` = no safe parallel configuration (NO / skipped), `1` = viable but no measurable gain, `≥2` = recommended |
| `worker_count_safe` | **per row**: `YES`/`NO` verdict of this worker count (`BASELINE` for 1, `N/A` for native/skipped); `worker_safety_notes` lists failed checks or recovered-throttling notes |
| `total_streams`, `successful_streams`, `failed_streams`, `limited_streams`, `skipped_streams` | selected, completed (incl. limited), failed/timed-out, stopped by a limit, and *excluded from the catalog* (never run) |
| `record_limit` | `500`, `unlimited` or `mixed (...)` |
| `sequential_duration_seconds`, `execution_duration_seconds`, `improvement_percentage`, `speedup`, `throughput_records_per_second` | blank when not computable (e.g. baseline or run failed completely) |
| `max_observed_concurrency`, `avg_observed_concurrency`, `concurrency_verdict`, `total_stream_execution_seconds` | proof of overlap (section 8.5) |
| `http_429_count`, `throttling_count`, `retry_count`, `http_4xx_count` (includes 429), `http_5xx_count`, `timeout_count`, `extraction_error_count` | error/rate-limit tracking |
| `failed_stream_names`, `error_summary`, `start_time`, `end_time` | details; times are RFC 3339 UTC |
| extras | `rate_limit_info`, `detected_rate_limit_headers`, `rate_limit_min_remaining`, `records_match_baseline`, `api_request_count`, `api_requests_after_limit`, `http_status_counts`, `parallel_efficiency_percentage`, `longest_unit_*`, `avg_process_startup_seconds`, `notes`, `log_file`, `output_file`, … |

Blank cell = *not measurable* (nothing is invented). `rate_limit_info` is `UNKNOWN` unless documented/detected.

### 8.3 Stream-level CSV and TXT report
`stream_level_results.csv`: tap, stream, worker count, worker id, start/end timestamps, duration, `records_extracted`, `record_limit`, `stream_status`, 429/retry/throttle counts,
`error_category`, `error_message`, `overlapped_with_other_stream` (`YES` if its time window intersects any stream of another work unit), plus `work_unit`, `metrics_scope`, `timing_basis`, `api_requests_after_limit`.
`metrics_scope=unit` means a stream's HTTP counts are those of its whole work unit (a parent and its children share one process); batched units use time-attributed counts.
Use it to see whether problems affect one stream or the whole tap.

`parallelism_report.txt`: **A** overall summary (tested / YES / NO / skipped taps, average improvement), **B/C** per tap (selected streams, record limit, baseline, results table per worker count, concurrency validation,
YES/NO, recommended workers, observations, failed-stream patterns *CONSISTENT / PARALLEL-ONLY / INTERMITTENT*, reasons, technical validation) and **D** reasons for every NO.
Detailed raw logs are in `logs/<tap>/<run_id>/` and `results/<run_id>/artifacts/<tap>/workers_<N>/_unit_*_stderr.log` (secrets scrubbed).

### 8.4 How YES/NO and the recommended worker count are calculated
For each parallel worker count (> 1) these checks are evaluated against the 1-worker run of the same tap:

| Check | Passes when |
|---|---|
| `streams_complete` | no stream fails (or is skipped/timed out) that succeeded sequentially, and some streams succeeded |
| `concurrency_verified` | ≥ 2 work units, observed max concurrency ≥ min(workers, units) and ≤ workers (from real start/end timestamps) |
| `output_integrity` | merged Singer output is consistent (every line valid JSON, record count equals what was streamed) and no new non-Singer stdout lines |
| `data_consistency` | for streams that completed in both runs, record counts are not lower than the baseline (tolerance `record_tolerance_pct`) |
| `no_added_timeouts` | no more timeouts than the baseline |
| `rate_limiting_recoverable` | no stream failed because of throttling, no retry give-ups beyond the baseline, and HTTP 429 ≤ `max_429_ratio` of requests. **Occasional 429s that the tap's retries recovered do not cause NO** – they are reported as warnings |

A worker count is **safe** if all checks pass. `can_parallel_extraction_be_done = YES` if any parallel worker count is safe, otherwise `NO` (also `NO` when the tap was skipped, only 1 worker was tested, or the baseline itself failed – see `decision_basis`).
`recommended_worker_count` = among safe counts, the **smallest** one reaching ≥ 95% of the best safe speedup (extra workers that add risk without gain are not chosen); `1` if the best safe speedup is ≤ 1.05×; `0` if none is safe.
A count with 3 workers throttled but 2 workers clean yields `YES` with `2`.

### 8.5 Concurrency proof
From each work unit's start/end timestamp a sweep-line computes the maximum and time-weighted average number of streams in flight, the sum of stream times and the wall-clock; `concurrency_verdict` is
`SEQUENTIAL_CONFIRMED`, `FULL` (max = min(workers, units)), `PARTIAL_x_OF_y`, `NO_OVERLAP_OBSERVED` or `INVALID_EXCEEDS_WORKERS`. Worker *threads* are never counted as concurrency.
The per-run log contains a `[TIMELINE]` (worker | stream | start | end) and a `[CONCURRENCY]` line.
Speedup is bounded by the longest work unit and by per-process start-up overhead (both reported). Timings come from single runs and vary with API/network load (the TXT flags runs whose summed stream time differs >10% from the baseline) – repeat before relying on exact numbers.

---

## 9. Troubleshooting

| Symptom | What to check / do |
|---|---|
| `SKIPPED_INVALID_CONFIG` | Read the reasons in the row/TXT: missing/invalid `config.json`, missing required keys, **empty or unreadable catalog**, missing command file, `known_invalid_config`. Fix the tap's own files (never the framework's), then rerun `--dry-run`. |
| `SKIPPED_ENVIRONMENT` / "no virtualenv python found" | Create the tap's `venv` and `pip install -e .` in it, or set `python_path` in the registry. |
| Missing catalog | Run the tap's discovery command (`tmp/commands/discovery_run_command.txt`), then `python tmp/scripts/add_selected.py <catalog>` as in `command_steps.txt`. Or set `catalog_strategy: "discover_if_missing"` – the framework then writes a new `_discovery_output.json` under `results/…/artifacts` and selects all streams. |
| Discovery failure | Run the discovery command by hand and read its stderr (credentials, network). The benchmark does not need discovery when a valid catalog exists. |
| "no selected streams" | Streams must have `"selected": true` in the root metadata of the existing catalog. |
| Sync failure (`FAILED`) | Look at `errors.csv` (category, status, message), the unit's `_unit_*_stderr.log` and `logs/<tap>/<run>/workers_N.log`. If the same stream fails at every worker count the pattern is `CONSISTENT` – a tap/data problem, not a parallelism problem. |
| Authentication failure | Category `AUTH` (401/403): check the tap's config credentials. All worker counts fail together. |
| Rate limiting / HTTP 429 | Columns `http_429_count`, `throttling_count`, `retry_count`, `rate_limit_min_remaining`; category `RATE_LIMIT`/`THROTTLING` in `errors.csv`. If the tap's retries recover it, the worker count can still be safe (warning). If not (`rate_limiting_recoverable` fails) use fewer workers: the tap's own limiter is per process, so N workers multiply the request rate. |
| Timeouts | `TIMED_OUT` streams / `timeout_count`. Raise `--stream-timeout` / `--timeout`, lower the record limit, or use `--max-streams` for a smoke test. |
| Missing Python dependencies | The framework needs none. For tests: `pip install -r requirements.txt`. A tap reporting `ModuleNotFoundError` needs `pip install -e .` in *its* venv. |
| Worker execution failure / interrupted run | Ctrl-C stops all tap processes and writes partial reports (exit 130). Orphaned processes after a hard kill can be ended with Task Manager (look for the tap's `python.exe`). |
| Output conflicts | `output_integrity` check, `[MERGE]` lines in the log (`consistent=False`), `merge:` notes. Each unit has its own output file; conflicts would indicate a non-Singer line on stdout (counted per unit as `non-Singer line(s)`). |
| Record limit not enforced | Run `--dry-run` and read the `[limit:source]` per stream. Remember: limits apply to *selected* streams, CLI `--record-limit` overrides all config-file limits, finer CLI flags beat it, and the native baseline is skipped when limits exist. Check `limited_streams`, `stream_status` and `api_requests_after_limit` (should be 0). For BICC (Oracle) the server-side job still runs before rows arrive. |
| Records differ from baseline (`records_match_baseline` = `DIFF…`) | Live data changed between runs, or a stream failed partially. Differences within `record_tolerance_pct` are ignored; lower counts in parallel fail `data_consistency`. |
| Results folder exists | Run ids are timestamps; pass a different `--run-id`. Folders are never overwritten. |

---

## 10. Adding a new tap

1. **Place the repository** next to the others (`workspace/tap-new/`) or note its absolute path.
2. **Provide its files** (already present for current taps): `tmp/configs/config.json`, `tmp/commands/sync_run_command.txt` (+ `discovery_run_command.txt`), a **venv** with the tap installed, and an **existing catalog** with streams marked `"selected": true` (`tmp/catalogs/catalog.json`).
3. **Register it** – add an entry to `configs/tap_registry.json`:
   ```json
   "tap-new": {
     "repo_path": "tap-new",
     "enabled": true,
     "adapter": "process_isolation",
     "config_path": "tmp/configs/config.json",
     "sync_command_path": "tmp/commands/sync_run_command.txt",
     "discovery_command_path": "tmp/commands/discovery_run_command.txt",
     "catalog_path": "tmp/catalogs/catalog.json",
     "required_config_keys": ["start_date"],
     "rate_limit_info": "UNKNOWN",
     "isolation_findings": ["what you learned from reading its sync loop / client / rate limiter"]
   }
   ```
   Add `independent_streams`, `config_overrides`, `streams_per_task`, … only if inspection shows a need (section 4).
4. **Enable it**: add the name to `enabled_taps` in `benchmark_config.json` (or use `--taps tap-new`). Optionally add `tap_record_limits` / `stream_record_limits`.
5. **Validate**: `python benchmark.py --dry-run --taps tap-new` (config keys, catalog, venv, work units, record limits).
6. **Execute**: `python benchmark.py --taps tap-new --workers 1 2 3 --record-limit 200` (start small).
7. **Interpret** the row for each worker count, `can_parallel_extraction_be_done`, `recommended_worker_count` and the TXT reasons; inspect the stream-level CSV for problematic streams.

Inspect the new tap's code first: how streams are looped, which objects are shared (client/session/state), whether child streams are emitted by a parent, what the tap's own rate limiter does. Put those facts into `isolation_findings`.
Taps need no code in this framework; only a different execution model (e.g. a proven thread-safe in-process adapter) would be added to `adapters.py`.

---

## 11. Limitations and future improvements

Known limitations (honest list):
* **One run per configuration**: results vary with API/network conditions. The report flags suspicious differences but does not repeat runs.
* **Process-per-work-unit overhead** (interpreter start, imports, authentication, reported as `avg_process_startup_seconds`) is part of every run; the 1-worker baseline pays it too, so speedups are fair relative to each other but differ from a hypothetical in-process implementation. The tap's own single-process run (`--native-baseline`) can be faster than the 1-worker pool.
* **Rate limiters are per process** – the benchmark shows what happens if N workers are used, not what a single shared limiter would do.
* **Record limits**: enforced at the stdout protocol layer (per work unit); a stream with exactly `limit` records is reported as limited; BICC/ESS server-side extraction is not shortened; units that contain a parent and children stop when the parent reaches its limit, and the whole unit is the scheduling granule.
* A work unit that fails marks all its streams failed (a batch of unlimited streams can hide which stream broke).
* Retries are counted from the `backoff` library log only; custom retry loops are invisible. Only taps using `requests` are fully instrumented.
* `COMPLETED_WITH_LIMIT` runs are truncated extractions – not a full-sync duration.
* Tested on Windows with Python 3.12 and singer-python 6.8.0; Linux/macOS paths are implemented but untested here.
* Safety is judged on the tested data/limits/time; a `YES` is evidence, not a guarantee.

Future work: adaptive worker-count search (stop climbing when 429s appear), automated optimal-concurrency detection, repeated runs with confidence intervals, historical comparison and trend analysis (building on `execution_summary.json` and `compare_runs.py`),
better rate-limit detection (per-endpoint limits, `Retry-After` semantics), support for other Singer SDK generations, automatic stream-dependency detection from the tap code, and additional execution adapters (shared-limiter, in-process threads for taps proven safe).

## Security
Passwords, tokens, private keys and authorization headers found in the tap config files (and `config_jira.json`) are removed from every log, stderr capture and report, together with generic `Bearer`/`Basic`/`password=` patterns; URL query values of sensitive parameters are redacted at capture time;
request headers/bodies are never stored. The only copy of credentials the framework may create is the temporary working config (when `config_overrides` is set), deleted after the tap's runs. Keep `results/` and `logs/` out of version control (see `.gitignore`).

## Tests
```bash
python -m pytest tests -q          # unit tests (config, limits, planning, metrics, decisions, reports) + end-to-end tests with a fake tap
python -m pylint *.py worker_support/worker_runner.py --disable=C,R --max-line-length=130
```
