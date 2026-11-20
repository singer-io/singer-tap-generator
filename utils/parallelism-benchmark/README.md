# Singer Tap Parallelism Benchmark

Find out, **for any Singer tap**, whether its streams can be extracted in parallel, how much faster that is,
and how many workers you can use before rate limits or errors appear.

It only **measures**. It never changes a tap (no tap code, config, catalog, command or output file is written).

**Contents:** [Quick start](#quick-start) · [Set the number of workers](#how-to-set-the-number-of-workers) · [How it works](#how-it-works) · [Setup](#setup) · [Configure](#configure) ·
[Run](#run) · [Read the results](#read-the-results) · [Troubleshooting](#troubleshooting) · [Add a tap](#add-a-new-tap) · [Limitations](#limitations)

---

## Quick start

```bash
cd parallelism-benchmark

python benchmark.py --dry-run                              # 1. check everything, run nothing
python benchmark.py --taps tap-jira --workers 1 2 3 4      # 2. benchmark one tap with 1, 2, 3 and 4 workers
python benchmark.py                                        # 3. benchmark every enabled tap (see configs/benchmark_config.json)
```

Then open `results/latest.json` → it points to the newest run folder. Inside, read **`parallelism_report.txt`** (for people)
or **`parallelism_report.csv`** (for spreadsheets).

---

## How to set the number of workers

**Example: test `tap-chargify` with 1, 2, 4, 8 and 12 workers.**

### Way 1 – on the command line (simplest, nothing to edit)

```bash
python benchmark.py --taps tap-chargify --workers 1 2 4 8 12
```

That is all. The framework runs the tap once per worker count and compares every run with the 1-worker run.

### Way 2 – in the configuration file

Open `configs/benchmark_config.json` and change `workers` (and, if you want only Chargify, `enabled_taps`):

```json
{
  "workers": [1, 2, 4, 8, 12],
  "enabled_taps": ["tap-chargify"]
}
```

Then run it with no options: `python benchmark.py`

> To keep the shipped file unchanged, copy it to `configs/my_test.json`, edit the copy and run
> `python benchmark.py --config configs/my_test.json`.

### Rules to remember

| Rule | Detail |
|---|---|
| **1 is always included** | It is the baseline everything is compared with. `--workers 8` runs `1` and `8`. |
| **Order and duplicates do not matter** | `--workers 12 4 1 2 8 4` is the same as `--workers 1 2 4 8 12`. |
| **Command line beats the file** | The file says `[1, 2]` and you type `--workers 1 3 4` → `1 3 4` is used. |
| **Any whole number ≥ 1** | `0` or text is rejected with a clear message before anything runs. |
| **You cannot use more workers than the tap has streams** | Chargify has **11** streams, so `12` workers can only run 11 at once. The report says so (`effective_workers = 11`) and treats 12 as the same test as 11. |
| **Each worker is a separate Python process** | 12 workers = 12 processes at the same time. Make sure your machine and the API can take it. |

### What you get for Chargify (real run, 300 records per stream)

| Workers asked | Workers that really ran at once | Time | Speedup |
|---|---|---|---|
| 1 | 1 | 143 s | – |
| 2 | 2 | 69 s | 2.06× |
| **4** | **4** | **39 s** | **3.68×** ← recommended |
| 8 | 8 | 43 s | 3.33× |
| 12 | 11 (only 11 streams) | 43 s | 3.33× |

**How to read it:** going from 4 to 8 or 12 workers did **not** help. One stream, `transactions`, takes about 41 s on its own (the next ones, `statements` and `invoices`, take about 21 s), and the sequential total is about 143 s,
so the best possible speedup is roughly 143 ÷ 41 ≈ 3.5×. With 4 workers you are already there; extra workers just wait. No rate limit (HTTP 429) appeared at any count, so the report says
`max workers successfully tested = 11`, `max workers without rate limit = 11`, and **recommended = 4** (the smallest count that reaches the best speed).

### Good habits
- **Start small** (`--workers 1 2 4`), then add bigger counts only if the speedup is still growing.
- **Check the cost first:** `python benchmark.py --taps tap-chargify --workers 1 2 4 8 12 --dry-run` prints the plan (and warns when you ask for more workers than there are streams).
- **Keep it quick:** add `--record-limit 300` (stops each stream after 300 records) – Chargify with 1, 2, 4, 8, 12 workers takes about 6 minutes this way.
- If rate limits appear at a high count, the report shows the highest count **without** rate limits in `max_recommended_workers_without_rate_limit`.

---

## How it works

```
 selected streams of the tap's existing catalog
        |   a stream + its child streams = one "work unit"
        v
 queue of work units ──►  worker 1  runs  unit A  (its own tap process)
                     ──►  worker 2  runs  unit B  (its own tap process)
                     ──►  worker N  runs  unit C  (its own tap process)
```

- A **worker** runs one stream at a time. `--workers 1 2 3 4` runs the whole tap four times, with 1, 2, 3 and 4 workers.
- **1 worker is the baseline.** Every other run is compared with it (same streams, same record limits, same config).
- Each work unit runs the tap's **own command** in a **separate process**, so logins, HTTP sessions, state and output files are never shared.
  (Threads inside a tap are not safe: the taps keep config/client/state in shared objects.) It is **not** "the whole sync run N times": each stream is extracted once per run.
- Start and end times of every stream prove that streams really overlapped (`max_observed_concurrency`).
- A small launcher inside the tap's own Python records every HTTP status, retry and rate-limit header, and enforces record limits.

---

## Setup

**You need:** Python for the framework (tested on 3.12.9; standard library only, written to avoid newer syntax, but other versions are untested) and, for each tap, its own virtual environment at
`<tap-repo>/venv` with the tap installed (`pip install -e .`). Windows is tested; Linux/macOS code paths exist but are untested.

**Folder layout** – the framework sits **next to** the tap repositories:

```text
workspace/
├── tap-jira/                  (each has tmp/configs, tmp/commands, tmp/catalogs, venv/)
├── tap-oracle-fusion/
├── tap-sap-success-factors/
├── tap-activecampaign/
├── tap-chargify/
├── tap-salesforce/            (excluded: its configuration is invalid)
└── parallelism-benchmark/     <-- this folder
    ├── benchmark.py           run this
    ├── configs/benchmark_config.json   what to run (workers, taps, limits)
    ├── configs/tap_registry.json       where each tap lives
    ├── results/<date_time>/            one folder per run (never overwritten)
    ├── logs/<tap>/<run>/               raw logs
    └── tests/
```

Taps are found through `configs/tap_registry.json` (a relative `repo_path` is resolved from the folder that contains `parallelism-benchmark/`).

**Optional – test tooling only:** `python -m venv .venv`, activate it, `pip install -r requirements.txt`, then `python -m pytest tests -q` (~2½ min, no external services).

### What each tap needs (files are only read, never changed)

| File in the tap repo | Purpose |
|---|---|
| `tmp/configs/config.json` | the tap's own credentials/settings |
| `tmp/commands/sync_run_command.txt` | the sync command; the framework reads the program name and the `--config` / `--catalog` flags |
| `tmp/commands/discovery_run_command.txt` | only used when the tap has no catalog yet |
| `tmp/catalogs/catalog.json` | the existing catalog; streams with `"selected": true` are benchmarked |
| `venv/` | the tap's virtual environment |

Notes: **Oracle Fusion** – the existing catalog is the truth (14 of 43 streams selected; the rest are never run). **Chargify** – has no catalog, so it is discovered at run time
into `results/…/artifacts/` and all streams are selected. `tmp/configs/config_jira.json` is a Jira *ticketing* token, not a tap config – it is never passed to a tap and is only used to scrub secrets from logs.

---

## Configure

Settings come from **`configs/benchmark_config.json`**; **command-line options override the file**.

```json
{
  "workers": [1, 2, 3, 4],
  "enabled_taps": ["tap-jira", "tap-oracle-fusion", "tap-sap-success-factor", "tap-activecampaign", "tap-chargify"],
  "timeout_seconds": 1800,
  "tap_record_limits":    { "tap-oracle-fusion": 200, "tap-activecampaign": 500, "tap-chargify": 300 },
  "stream_record_limits": { "tap-jira": { "issues": 1000 }, "tap-activecampaign": { "contacts": 500 } },
  "tap_max_streams":      { "tap-oracle-fusion": 8 },
  "schedule_order": "longest_first",
  "check_environment": true
}
```

| Setting | What it does |
|---|---|
| `workers` | worker counts to test, e.g. `[1, 2, 4, 8, 12]` (`1` is always added; see [Set the number of workers](#how-to-set-the-number-of-workers)) |
| `enabled_taps` | taps to run (aliases such as `tap-sap-success-factor` work) |
| `default_record_limit`, `tap_record_limits`, `stream_record_limits` | stop each stream after N records (see below) |
| `max_streams`, `tap_max_streams` | benchmark only the first N work units of a tap (see below) |
| `timeout_seconds`, `stream_timeout_seconds` | time limit per run / per work unit (default 1800 s) |
| `schedule_order` | `longest_first` (default) hands the slowest streams to workers first, so one slow stream can't start last and distort the result; `catalog` uses catalog order |
| `check_environment` | check that each tap can start before benchmarking it (default `true`) |
| `baseline_fail_fast` | stop the sequential run after this many streams if none succeeded (default 3) |
| `continue_on_stream_failure`, `stop_on_unstable_run` | keep going after a failed stream / stop trying higher worker counts after trouble |
| `max_429_ratio` | a worker count is unsafe if more than this share of its requests are HTTP 429 (default 10%) |
| `log_level`, `results_dir`, `logs_dir` | logging and output locations |

Unknown keys are rejected, so a typo can't silently do nothing. Where each value came from (default / file / CLI) is stored in `execution_summary.json`.

### Record limits – keep slow taps short
`--record-limit 500` stops every stream after 500 records. A stream stopped this way is `COMPLETED_WITH_LIMIT` – **not a failure** – and the same limits apply to every worker count, so runs stay comparable.
The tap is stopped in the very write that emits the last allowed record, so **no further API page is requested** (`api_requests_after_limit` should be 0).

Which limit wins (highest first): `--stream-record-limit TAP:STREAM=N` → `--tap-record-limit TAP=N` → `--record-limit N` → config `stream_record_limits` → config `tap_record_limits` → config `default_record_limit` → unlimited.

### Stream sampling – for taps that are expensive *per stream*
A record limit does not help when each stream costs a fixed time. **Oracle Fusion** is the example: every stream takes ~29 s (a server-side export job) whether it returns 2 or 14,000 records. For such taps benchmark only the first N streams:

```bash
python benchmark.py --taps tap-oracle-fusion --max-streams 6          # first 6 work units, for every worker count
python benchmark.py --all-streams                                     # ignore the sample settings in the file
```
Order: `--tap-max-streams TAP=N` → `--max-streams N` / `--all-streams` → config `tap_max_streams` → config `max_streams` → all.
Sampled runs are labelled **SAMPLED** in the report. Use at least as many work units as your largest worker count. The dry run prints the planned number of tap processes so you can see the cost first.

---

## Run

```bash
python benchmark.py                                          # everything enabled in the config file
python benchmark.py --workers 1 2 3 4                        # choose worker counts (all enabled taps)
python benchmark.py --taps tap-chargify --workers 1 2 4 8 12 # any list of counts, for one tap
python benchmark.py --taps tap-activecampaign --workers 1 2 3
python benchmark.py --taps tap-chargify --record-limit 300
python benchmark.py --taps tap-oracle-fusion --workers 1 2 --record-limit 100 --max-streams 4
python benchmark.py --dry-run                                # checks + plan, executes nothing
python benchmark.py --list-taps
python compare_runs.py --list                                # list finished runs
python compare_runs.py RUN_A latest --tap tap-jira           # compare two runs (ids or "latest")
```

Other options: `--timeout`, `--stream-timeout`, `--stop-on-failure`, `--stop-on-unstable`, `--schedule catalog`, `--native-baseline`,
`--skip-env-check`, `--results-dir`, `--logs-dir`, `--log-level`, `--run-id`, `--config`, `--registry`. Run `python benchmark.py --help` for all of them.

Exit codes: `0` finished · `2` invalid configuration (nothing is created) · `130` interrupted (partial reports are still written).
A run folder is never reused or overwritten.

> **Do not install or upgrade a tap's virtualenv while a benchmark is running** – see [Troubleshooting](#troubleshooting).

---

## Read the results

```text
results/
├── latest.json                      points to the newest successful run
└── 2026-11-19_14-51-18/             one folder per run
    ├── parallelism_report.txt       human-readable report (start here)
    ├── parallelism_report.csv       one row per tap and worker count
    ├── stream_level_results.csv     one row per stream and worker count
    ├── errors.csv                   every HTTP error, throttle signal and failed stream
    ├── execution_summary.json       machine-readable summary and decisions
    └── artifacts/                   merged Singer output, per-stream stderr and HTTP events
logs/<tap>/<run>/workers_<N>.log     scheduling, start/end times, errors, retries, concurrency timeline
```

### The three questions the CSV answers (first columns)

| Column | Answers | Level |
|---|---|---|
| `can_parallel_extraction_be_done` | **Does it work in parallel?** `YES` = at least one parallel run passed every check | tap |
| `max_workers_successfully_tested` | **How many workers worked?** most workers that really ran at once in a run that passed every check (never more than the tap has streams) | tap |
| `max_recommended_workers_without_rate_limit` | **How many workers without rate limits?** highest count with no rate-limit signal at it or below | tap |
| `worker_count` | workers you asked for in this run (`effective_workers` = how many could really run at once: never more than the tap's number of streams) | row |
| `rate_limit_encountered` | `YES` / `NO` / `UNKNOWN` (HTTP not measurable) / `N/A` | row |
| `extraction_status` | `SUCCESS` / `PARTIAL` / `FAILURE` / `SKIPPED` | row |
| `total_records`, `execution_duration_seconds` | records and wall-clock time of this run | row |

*Tap-level* values are the same on every row of a tap. After these come `recommended_worker_count` (best safe count; `0` = none, `1` = works but no gain),
`evaluation_status`, `worker_count_safe`, `speedup`, `improvement_percentage`, `sequential_duration_seconds`, record/stream counts, HTTP 429/4xx/5xx/retry/timeout counts, `error_summary` and more.
A blank cell means "not measurable" – nothing is invented.

### Can I trust a `NO`? → look at `evaluation_status`

| `evaluation_status` | Meaning |
|---|---|
| `CONCLUSIVE` | the result was measured; a `NO` really means parallel extraction failed the safety checks |
| `INCONCLUSIVE` | the sequential run failed (nothing to compare), or the only failures were network/auth/environment errors that concurrency did not cause → **fix and re-run** |
| `NOT_EVALUATED` | the tap was skipped or could not start → not a verdict at all |

### How the numbers are calculated
- `improvement % = (sequential − this run) / sequential × 100`, `speedup = sequential / this run`, `throughput = records / seconds`. Extraction time only. Speedup is left blank if the run did **different work** than the baseline (e.g. it failed streams) – a run that is faster because it failed isn't an improvement.
- A parallel worker count is **safe** when all of these hold against the 1-worker run:

| Check | Passes when |
|---|---|
| `streams_complete` | every stream that worked sequentially also worked |
| `concurrency_verified` | streams really overlapped (max concurrency ≥ min(workers, work units) and never above the configured workers) |
| `output_integrity` | merged Singer output is valid and its record count matches |
| `data_consistency` | per-stream record counts are not lower than the baseline (2% tolerance) |
| `no_added_timeouts` | no more timeouts than the baseline |
| `rate_limiting_recoverable` | no stream failed from throttling and 429s are below `max_429_ratio`. **An occasional 429 that the tap's own retries recovered does not make a tap NO.** |

- `recommended_worker_count`: among safe counts that never met a rate limit, the **smallest** one reaching ≥ 95% of the best speedup (not simply the fastest). Example: 1 and 2 workers clean, 3 throttled but recovered, 4 failed → `YES`, max OK = 3, max without rate limit = 2, recommended = 2.
- **Rate limit** means: HTTP 429, a `Retry-After` header on an error, an exhausted quota header, rate-limit wording in the tap's log, or a stream that failed because of throttling.
- **Timings come from single runs** and vary with API load. The report warns when a run's summed stream time differs more than 10% from the baseline – repeat before relying on exact numbers.

### Stream statuses
`COMPLETED` · `COMPLETED_WITH_LIMIT` (stopped on purpose at the record limit) · `FAILED` · `TIMED_OUT` · `SKIPPED`.

---

## Troubleshooting

| You see | What to do |
|---|---|
| `SKIPPED_ENVIRONMENT` – "tap cannot be started with its own virtualenv" | The tap's Python can't load the tap (`ModuleNotFoundError`, command not found, no `venv`). Run the `pip install -e` command printed in the message **inside the tap's venv**, check with `python benchmark.py --dry-run` (`environment: OK`), re-run. |
| `SKIPPED_ENVIRONMENT` – "stopped being runnable during the benchmark" | The venv changed mid-run. Results of that tap were discarded on purpose. Don't touch venvs while a benchmark runs; re-run. |
| `SKIPPED_INVALID_CONFIG` | Read the reason: missing config/command file, missing required key, empty catalog. Fix the **tap's** files, then `--dry-run`. |
| `NO` but `INCONCLUSIVE` / `NOT_EVALUATED` | Not a verdict. See `decision_summary` and the "REASONS FOR NO DECISIONS" part of the TXT report, fix, re-run. |
| Two reports for the same tap disagree | Compare `evaluation_status`, `benchmark_status`, the record limit / sample and `errors.csv` first. Typical causes: a broken environment, a failed baseline, different limits, API load. |
| A stream `FAILED` | See `errors.csv` (category, HTTP status, message) and the unit's `_unit_*_stderr.log`. If it fails at **every** worker count it is a tap/data problem, not a parallelism problem (the report says `CONSISTENT`). |
| `AUTH` errors | Check the credentials in the tap's `config.json`. |
| HTTP 429 / rate limiting | See `http_429_count`, `rate_limit_min_remaining`, `errors.csv`. Use fewer workers: each worker process has its own client-side limiter, so N workers send up to N× the request rate. |
| `TIMED_OUT` | Raise `--stream-timeout` / `--timeout`, lower the record limit, or sample with `--max-streams`. |
| No catalog / no selected streams | Run the tap's discovery command and select streams (`"selected": true`), or set `catalog_strategy: "discover_if_missing"` in the registry. |
| Record limit "not working" | `--dry-run` prints each stream's limit and where it came from. `--record-limit` overrides the file; finer CLI flags beat it. Check `limited_streams`, `stream_status`, `api_requests_after_limit`. Oracle's server job still runs before rows arrive. |
| `records_match_baseline` = `DIFF…` | Live data changed between runs, or a stream failed partly. Differences within 2% are ignored. |
| "results folder already exists" | Choose another `--run-id`; folders are never overwritten. |
| Missing Python packages | The framework needs none. For tests: `pip install -r requirements.txt`. A tap's `ModuleNotFoundError` needs `pip install -e .` in the tap's venv. |

**Case study – why one Jira report said YES and another NO:** in the second folder the tap's venv was still being installed while the benchmark ran. The 1- and 2-worker runs failed on every stream with `No module named 'requests'`,
the 3- and 4-worker runs succeeded. The old logic turned the failed baseline into a hard `NO`. Now the environment is checked first, environment errors stop the run and discard the tap's results, and a failed baseline is `INCONCLUSIVE`.

---

## Add a new tap

1. Put the repository next to the others and create its `venv` (`pip install -e .`).
2. Make sure it has `tmp/configs/config.json`, `tmp/commands/sync_run_command.txt` and (if it exists) a catalog with `"selected": true` streams.
3. Add an entry to `configs/tap_registry.json`:
   ```json
   "tap-new": {
     "repo_path": "tap-new",
     "enabled": true,
     "adapter": "process_isolation",
     "config_path": "tmp/configs/config.json",
     "sync_command_path": "tmp/commands/sync_run_command.txt",
     "discovery_command_path": "tmp/commands/discovery_run_command.txt",
     "catalog_path": "tmp/catalogs/catalog.json",
     "catalog_strategy": "preserve_existing",
     "required_config_keys": ["start_date"],
     "rate_limit_info": "UNKNOWN",
     "isolation_findings": ["what you learned from reading the tap's sync loop, client and rate limiter"]
   }
   ```
   Use `"catalog_strategy": "discover_if_missing"` when there is no catalog (as for Chargify).
4. Add it to `enabled_taps` in `benchmark_config.json` (or use `--taps tap-new`).
5. Check: `python benchmark.py --dry-run --taps tap-new`. Run: `python benchmark.py --taps tap-new --workers 1 2 3 --record-limit 200`.

Read the tap's code first: how it loops over streams, what is shared (client, session, state), whether a parent stream emits child streams, what its own rate limiter does – and write that into `isolation_findings`.
Optional per-tap registry settings (no code changes needed):

| Setting | Use |
|---|---|
| `config_overrides` | benchmark-only config changes, applied to a **temporary copy** of the config (e.g. Oracle `bicc_force_full_sync`) |
| `independent_streams` | catalog child streams the tap does *not* emit through the parent (e.g. ActiveCampaign `email_activities`) |
| `streams_per_task` | batch this many unlimited streams per process to save start-up time (SAP: 10) |
| `rate_limit_info` | documented limit; leave `UNKNOWN` unless known |
| `enabled`, `known_invalid_config`, `skip_reason` | exclude a tap (Salesforce) |
| `python_path`, `extra_env`, `stream_include`, `stream_exclude` | advanced |

Salesforce stays excluded until its configuration is fixed; then set `enabled: true`, `known_invalid_config: false`, `catalog_strategy: "discover_if_missing"`.

---

## Limitations

- **One run per configuration.** Results vary with API/network load; the report flags suspicious differences but does not repeat runs.
- **Process-per-stream overhead** (Python start, login) is paid by every run including the baseline, so speedups are fair relative to each other but differ from a hypothetical in-process implementation.
- **Rate limiters are per process.** The benchmark shows what happens with N independent workers, not with one shared limiter.
- **Record limits** truncate extraction (durations are then not full-sync times). A stream with exactly N records also counts as limited. Job-based extracts (Oracle BICC) still run their server job.
- A work unit that fails marks all its streams failed (a parent and its children, or a batch).
- Retries are counted from the `backoff` library only; only taps that use `requests` are fully instrumented.
- A `YES` is evidence on the tested data, not a guarantee.

**Ideas for later:** adaptive worker search, repeated runs with confidence intervals, trend analysis across runs, automatic stream-dependency detection, a shared-limiter adapter.

## Security
Passwords, tokens, private keys and authorization headers found in the tap configs (and `config_jira.json`) are removed from every log and report; sensitive URL query values are redacted; request headers and bodies are never stored.
The only credential copy the framework may create is the temporary working config (when `config_overrides` is used), deleted after the tap's runs. Keep `results/` and `logs/` out of version control (see `.gitignore`).
