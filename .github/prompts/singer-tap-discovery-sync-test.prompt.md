---
mode: agent
description: Test a Singer tap end-to-end — runs discovery and data sync, using real credentials if a test account is available, or auto-generated mock data if not. Produces a full summary report.
inputs:
  - id: tapNameOrUrl
    description: "Tap name or full Git URL  e.g. tap-zendesk  OR  https://github.com/singer-io/tap-zendesk"
    type: promptString
  - id: tapDirectory
    description: "Full local path where the tap exists or should be cloned  e.g. C:\\Users\\user_name\\Documents\\workspace\\taps\\tap-zendesk"
    type: promptString
  - id: hasTestAccount
    description: "Do you have a test account with real credentials? Enter 'yes' or 'no'"
    type: promptString
    default: "no"
  - id: configFilePath
    description: "Path to config.json with credentials. Leave blank to use the default (config.json inside the tap directory). Only used when hasTestAccount=yes"
    type: promptString
    default: ""
  - id: streamsToTest
    description: "Comma-separated list of stream names to test (e.g. boards,users,items). Leave blank to test ALL streams"
    type: promptString
    default: ""
  - id: startDate
    description: "Start date for incremental streams in ISO 8601 format e.g. 2024-01-01T00:00:00Z"
    type: promptString
    default: "2024-01-01T00:00:00Z"
---

# Singer Tap — Discovery & Data Sync Tester

You are an expert in Singer tap development and testing. Your job is to **run and validate** the complete Singer tap pipeline:
1. **Discovery** — run `--discover` and verify the catalog is valid
2. **Data sync** — run sync with a catalog and verify Singer messages are output correctly

You will use **real credentials** if a test account is available, or **auto-generated mock data** if not.

Follow every step below in strict order. Fix any errors autonomously — do not ask the user to fix things.

**Inputs:**
- Tap name/URL: `${input:tapNameOrUrl}`
- Tap directory: `${input:tapDirectory}`
- Has test account: `${input:hasTestAccount}`
- Config file path override: `${input:configFilePath}`
- Streams to test: `${input:streamsToTest}` *(blank = all)*
- Start date (fallback): `${input:startDate}`

---

## Step 1 — Prepare the Repository

### 1a — Resolve the tap name

Derive a short `TAP_NAME` from the input:
- If `${input:tapNameOrUrl}` contains `http`, extract the last path segment (e.g. `tap-zendesk` from `https://github.com/singer-io/tap-zendesk`)
- Otherwise use `${input:tapNameOrUrl}` directly as `TAP_NAME`

The Python module name is `TAP_MODULE` = replace hyphens with underscores in `TAP_NAME` (e.g. `tap_zendesk`).

### 1b — Check if the tap directory exists

```bash
test -d "${input:tapDirectory}" && echo "EXISTS" || echo "NOT_FOUND"
```

**If NOT_FOUND:** Derive the clone URL:
- If `${input:tapNameOrUrl}` starts with `http`, use it directly
- Otherwise use `https://github.com/singer-io/${input:tapNameOrUrl}`

Clone the tap:
```bash
git clone <resolved_url> "${input:tapDirectory}"
```

### 1c — Verify the tap module exists

```bash
ls "${input:tapDirectory}"
```

Confirm that a folder named `TAP_MODULE` (the Python package) is present. If not found, check for any folder starting with `tap_` and use that as `TAP_MODULE`.

### 1d — Install the tap

```bash
cd "${input:tapDirectory}"
pip install -e . --quiet 2>&1 | tail -5
```

If `setup.py` has `[dev]` extras:
```bash
pip install -e ".[dev]" --quiet 2>&1 | tail -5
```

Confirm the CLI entry point is available:
```bash
which TAP_NAME || pip show TAP_NAME
```

---

## Step 2 — Resolve Config File

### 2a — Determine the config path

Priority order:
1. If `${input:configFilePath}` is NOT blank → use that path as `CONFIG_PATH`
2. Else if `${input:tapDirectory}/config.json` exists → use that as `CONFIG_PATH`
3. Else if `${input:tapDirectory}/config.json.example` or `${input:tapDirectory}/sample_config.json` exists → note it as reference only (fake credentials — do NOT use for real runs)
4. Else → `CONFIG_PATH` is unresolved

```powershell
# Check for existing config files
Get-ChildItem "${input:tapDirectory}" -Filter "*.json" | Select-Object Name
```

### 2b — Validate test account decision

| hasTestAccount | CONFIG_PATH resolved? | Action |
|---|---|---|
| `yes` | Yes (from priority 1 or 2) | Proceed with real credentials → **Step 3A** |
| `yes` | No | Ask user to place `config.json` → **Step 2c** → **Step 3A** |
| `no` | — | Use mock mode → **Step 3B** |

---

### 2c — Ask user to place `config.json`  *(only when `hasTestAccount=yes` and no config file found)*

**Read `REQUIRED_CONFIG_KEYS` from the tap source** so you can tell the user exactly which keys are needed:

```python
import ast, pathlib, re

tap_dir = pathlib.Path(r"${input:tapDirectory}")
init_candidates = list(tap_dir.glob("tap_*/__init__.py"))
src = init_candidates[0].read_text() if init_candidates else ""
match = re.search(r"REQUIRED_CONFIG_KEYS\s*=\s*(\[.*?\])", src, re.DOTALL)
required_keys = ast.literal_eval(match.group(1)) if match else []
print("Required config keys:", required_keys)
```

**Display this message and wait for the user to confirm** before continuing:

```
⏸️  Paused — config.json required

hasTestAccount=yes but no config.json was found at:
  ${input:tapDirectory}\config.json

Please create that file now with the following keys:

  Required keys : <REQUIRED_CONFIG_KEYS>
  start_date    : use ISO 8601 format e.g. "2024-01-01T00:00:00Z"

Example:
  {
    "<key1>": "<value1>",
    "<key2>": "<value2>",
    "start_date": "2024-01-01T00:00:00Z"
  }

⚠️  Do NOT commit config.json — it contains credentials.
    Add it to .gitignore if not already present.

Once you have saved the file, type READY to continue.
(Type SKIP to run in mock mode instead.)
```

**Wait for the user to reply:**
- `READY` → verify `${input:tapDirectory}/config.json` now exists and is valid JSON, set `CONFIG_PATH` to that path, ensure `config.json` is in `.gitignore`, then continue to **Step 3A**
- `SKIP`  → switch to mock mode, continue to **Step 3B**
- Any other reply → re-show the message above

---

## Step 3A — Real Credentials Path (hasTestAccount = yes)

Skip this step if using mock mode. Jump to Step 3B.

### 3A-1 — Read and display config keys (NOT values)

Read `CONFIG_PATH` and display only the **key names** (not values) so the user can confirm credentials are correct:

```python
import json
with open("CONFIG_PATH") as f:
    cfg = json.load(f)
print("Config keys found:", list(cfg.keys()))
```

### 3A-2 — Run Discovery with real credentials

```bash
cd "${input:tapDirectory}"
TAP_NAME --config CONFIG_PATH --discover > /tmp/catalog_real.json 2>&1
echo "Exit code: $?"
```

Check exit code:
- **0** → Discovery succeeded, catalog written to `/tmp/catalog_real.json`
- **Non-zero** → Read stderr, diagnose the error, fix if possible (e.g. missing required config key), retry once. If it still fails, record the failure and continue to the sync step with a note.

Validate catalog structure:
```python
import json
with open("/tmp/catalog_real.json") as f:
    cat = json.load(f)
streams = cat.get("streams", [])
print(f"Streams discovered: {len(streams)}")
for s in streams:
    print(f"  - {s.get('stream') or s.get('tap_stream_id')}")
```

### 3A-3 — Select streams and prepare catalog

If `${input:streamsToTest}` is NOT blank, filter the catalog to only those streams. Otherwise select ALL streams.

Write the filtered catalog to `/tmp/catalog_selected.json`. Mark each stream as selected by setting `"selected": true` inside the stream's metadata entry at breadcrumb `[]`:

```python
import json

with open("/tmp/catalog_real.json") as f:
    cat = json.load(f)

filter_streams = [s.strip() for s in "${input:streamsToTest}".split(",") if s.strip()]

for stream in cat["streams"]:
    sname = stream.get("stream") or stream.get("tap_stream_id")
    should_select = (not filter_streams) or (sname in filter_streams)
    for entry in stream.get("metadata", []):
        if entry.get("breadcrumb") == []:
            entry["metadata"]["selected"] = should_select
            break
    else:
        # No root metadata entry — add one
        if not stream.get("metadata"):
            stream["metadata"] = []
        stream["metadata"].append({
            "breadcrumb": [],
            "metadata": {"selected": should_select}
        })

with open("/tmp/catalog_selected.json", "w") as f:
    json.dump(cat, f, indent=2)

selected = [s.get("stream") or s.get("tap_stream_id") for s in cat["streams"]
            if any(e.get("breadcrumb") == [] and e["metadata"].get("selected")
                   for e in s.get("metadata", []))]
print(f"Selected streams: {selected}")
```

### 3A-4 — Run Sync with real credentials

```bash
cd "${input:tapDirectory}"
TAP_NAME --config CONFIG_PATH --catalog /tmp/catalog_selected.json 2>/tmp/sync_stderr.log | tee /tmp/sync_output.jsonl | head -50
echo "Exit code: $?"
```

Collect Singer message statistics:
```python
import json

schema_count = 0
record_count = 0
state_count = 0
stream_record_counts = {}
errors = []

with open("/tmp/sync_output.jsonl") as f:
    for i, line in enumerate(f):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            t = msg.get("type")
            if t == "SCHEMA":
                schema_count += 1
            elif t == "RECORD":
                sname = msg.get("stream", "unknown")
                record_count += 1
                stream_record_counts[sname] = stream_record_counts.get(sname, 0) + 1
            elif t == "STATE":
                state_count += 1
            else:
                errors.append(f"Line {i+1}: unexpected message type '{t}'")
        except json.JSONDecodeError as e:
            errors.append(f"Line {i+1}: invalid JSON — {e}")

print(f"SCHEMA messages: {schema_count}")
print(f"RECORD messages: {record_count}")
print(f"STATE  messages: {state_count}")
print(f"Per-stream record counts: {stream_record_counts}")
if errors:
    print(f"Errors: {errors}")
```

Check sync stderr for Python errors:
```bash
cat /tmp/sync_stderr.log | grep -i "error\|traceback\|exception" | head -20
```

---

## Step 3B — Mock Data Path (hasTestAccount = no or config missing)

### 3B-1 — Locate schemas

```bash
ls "${input:tapDirectory}/TAP_MODULE/schemas/"
```

If no `schemas/` folder found, try alternative locations:
```bash
find "${input:tapDirectory}" -name "*.json" -path "*/schemas/*" | head -20
```

If **no schemas at all** — fail gracefully:
> "No schemas found and no test account provided. Cannot run mock tests. Please either provide a config.json with real credentials or ensure the tap has schema files in `TAP_MODULE/schemas/`."
> Skip to Step 6 and report failure.

### 3B-2 — Read all schema files

For each `.json` file in the `schemas/` folder, load the schema and collect:
- Stream name (filename without `.json`)
- All field names and their types from `properties`

If `${input:streamsToTest}` is NOT blank, only process those streams. Otherwise process ALL schema files.

### 3B-3 — Generate mock sample data

For each selected stream's schema, create 3 mock records by generating plausible values for each field type:

| JSON Schema type | Mock value strategy |
|---|---|
| `["null", "string"]` | `"sample_<field_name>"` |
| `["null", "integer"]` / `["null", "number"]` | `1` / `1.0` |
| `["null", "boolean"]` | `true` |
| `["null", "string"]` + `"format": "date-time"` | `"${input:startDate}"` |
| `["null", "array"]` | `[]` |
| `["null", "object"]` | `{}` |
| `"null"` only | `null` |

Write a Python script to generate and run a complete mock Singer tap:

```python
# /tmp/mock_tap.py  — auto-generated by this prompt
import json, sys, os

SCHEMAS_DIR = "${input:tapDirectory}/TAP_MODULE/schemas"
FILTER_STREAMS = [s.strip() for s in "${input:streamsToTest}".split(",") if s.strip()]

def mock_value(prop_def):
    types = prop_def.get("type", [])
    if isinstance(types, str):
        types = [types]
    fmt = prop_def.get("format", "")
    if "date-time" in fmt:
        return "${input:startDate}"
    if "boolean" in types:
        return True
    if "integer" in types:
        return 1
    if "number" in types:
        return 1.0
    if "array" in types:
        return []
    if "object" in types:
        return {}
    if "string" in types:
        return "sample_value"
    return None

def make_record(schema):
    props = schema.get("properties", {})
    return {k: mock_value(v) for k, v in props.items()}

schema_files = sorted(os.listdir(SCHEMAS_DIR))
for fname in schema_files:
    if not fname.endswith(".json"):
        continue
    stream_name = fname[:-5]
    if FILTER_STREAMS and stream_name not in FILTER_STREAMS:
        continue
    with open(os.path.join(SCHEMAS_DIR, fname)) as f:
        schema = json.load(f)
    # SCHEMA message
    sys.stdout.write(json.dumps({"type": "SCHEMA", "stream": stream_name, "schema": schema, "key_properties": []}) + "\n")
    # 3 RECORD messages
    for i in range(3):
        rec = make_record(schema)
        sys.stdout.write(json.dumps({"type": "RECORD", "stream": stream_name, "record": rec}) + "\n")
    # STATE message after each stream
    sys.stdout.write(json.dumps({"type": "STATE", "value": {stream_name: "${input:startDate}"}}) + "\n")
```

**Before running:** replace `TAP_MODULE` in the script with the actual module name detected in Step 1c.

Run the mock tap and capture output:
```bash
python /tmp/mock_tap.py > /tmp/sync_output.jsonl 2>/tmp/sync_stderr.log
echo "Exit code: $?"
head -20 /tmp/sync_output.jsonl
```

### 3B-4 — Build mock catalog from schemas

```python
import json, os

SCHEMAS_DIR = "${input:tapDirectory}/TAP_MODULE/schemas"
FILTER_STREAMS = [s.strip() for s in "${input:streamsToTest}".split(",") if s.strip()]

streams = []
for fname in sorted(os.listdir(SCHEMAS_DIR)):
    if not fname.endswith(".json"):
        continue
    stream_name = fname[:-5]
    if FILTER_STREAMS and stream_name not in FILTER_STREAMS:
        continue
    with open(os.path.join(SCHEMAS_DIR, fname)) as f:
        schema = json.load(f)
    streams.append({
        "stream": stream_name,
        "tap_stream_id": stream_name,
        "schema": schema,
        "key_properties": [],
        "metadata": [{"breadcrumb": [], "metadata": {"selected": True}}]
    })

catalog = {"streams": streams}
with open("/tmp/catalog_mock.json", "w") as f:
    json.dump(catalog, f, indent=2)

print(f"Mock catalog built with {len(streams)} streams:")
for s in streams:
    print(f"  - {s['stream']}")
```

### 3B-5 — Validate mock Singer output

```python
import json

schema_count = 0
record_count = 0
state_count = 0
stream_record_counts = {}
schema_errors = []
record_errors = []

with open("/tmp/sync_output.jsonl") as f:
    for i, line in enumerate(f):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            t = msg.get("type")
            if t == "SCHEMA":
                schema_count += 1
                if "stream" not in msg:
                    schema_errors.append(f"Line {i+1}: SCHEMA missing 'stream' key")
                if "schema" not in msg:
                    schema_errors.append(f"Line {i+1}: SCHEMA missing 'schema' key")
            elif t == "RECORD":
                sname = msg.get("stream", "unknown")
                if "record" not in msg:
                    record_errors.append(f"Line {i+1}: RECORD missing 'record' key")
                record_count += 1
                stream_record_counts[sname] = stream_record_counts.get(sname, 0) + 1
            elif t == "STATE":
                state_count += 1
        except json.JSONDecodeError as e:
            schema_errors.append(f"Line {i+1}: invalid JSON — {e}")

print(f"SCHEMA messages : {schema_count}")
print(f"RECORD messages : {record_count}")
print(f"STATE  messages : {state_count}")
print(f"Per-stream record counts: {stream_record_counts}")
print(f"Schema errors   : {schema_errors or 'None'}")
print(f"Record errors   : {record_errors or 'None'}")
```

---

## Step 4 — Discovery Validation (Schema Integrity Checks)

Run these checks regardless of whether real or mock mode was used.

Catalog file to validate: `/tmp/catalog_real.json` (real mode) or `/tmp/catalog_mock.json` (mock mode).

```python
import json

with open("/tmp/catalog_XXXX.json") as f:   # replace XXXX with real or mock
    cat = json.load(f)

issues = []
for entry in cat.get("streams", []):
    sname = entry.get("stream") or entry.get("tap_stream_id", "UNKNOWN")
    schema = entry.get("schema", {})
    props  = schema.get("properties", {})

    # Check 1: Schema has properties
    if not props:
        issues.append(f"[{sname}] schema has no properties")

    # Check 2: All field types are nullable (Singer convention)
    for field, fdef in props.items():
        ftype = fdef.get("type", [])
        if isinstance(ftype, list) and "null" not in ftype:
            issues.append(f"[{sname}] field '{field}' is not nullable (missing 'null' in type)")

    # Check 3: tap_stream_id matches stream name
    if entry.get("stream") and entry.get("tap_stream_id"):
        if entry["stream"] != entry["tap_stream_id"]:
            issues.append(f"[{sname}] stream != tap_stream_id ({entry['stream']} vs {entry['tap_stream_id']})")

    # Check 4: key_properties exist
    kp = entry.get("key_properties")
    if kp is None:
        issues.append(f"[{sname}] missing key_properties")

    # Check 5: All key_properties are defined in schema
    for kf in (kp or []):
        if kf not in props:
            issues.append(f"[{sname}] key_property '{kf}' not found in schema properties")

print(f"Streams checked: {len(cat.get('streams', []))}")
if issues:
    print(f"Issues found ({len(issues)}):")
    for iss in issues:
        print(f"  WARNING: {iss}")
else:
    print("All schema integrity checks passed.")
```

---

## Step 5 — Sync Output Validation (Singer Protocol Checks)

```python
import json

schema_streams  = set()
record_streams  = set()
orphan_records  = []   # records with no preceding SCHEMA for that stream
message_order   = []   # to detect RECORD before SCHEMA violations

with open("/tmp/sync_output.jsonl") as f:
    for i, line in enumerate(f):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            t   = msg.get("type")
            sname = msg.get("stream", "")
            if t == "SCHEMA":
                schema_streams.add(sname)
            elif t == "RECORD":
                record_streams.add(sname)
                if sname not in schema_streams:
                    orphan_records.append(f"Line {i+1}: RECORD for '{sname}' before its SCHEMA")
        except Exception:
            pass

missing_schemas = record_streams - schema_streams
extra_schemas   = schema_streams - record_streams   # schemas with zero records (not an error)

print("=== Singer Protocol Validation ===")
print(f"  Streams with SCHEMA  : {sorted(schema_streams)}")
print(f"  Streams with RECORDs : {sorted(record_streams)}")
print(f"  Streams with 0 records (schema only): {sorted(extra_schemas)}")
if orphan_records:
    print(f"  PROTOCOL VIOLATIONS (RECORD before SCHEMA):")
    for v in orphan_records:
        print(f"    {v}")
else:
    print("  Message ordering     : OK (all RECORDs preceded by SCHEMA)")
```

---

## Step 6 — Final Summary Report

Print the complete test summary. Fill in all values from the steps above.

```
╔══════════════════════════════════════════════════════════════════╗
║        Singer Tap Discovery & Sync Test Report                   ║
╚══════════════════════════════════════════════════════════════════╝

Tap           : TAP_NAME
Directory     : ${input:tapDirectory}
Test Mode     : REAL CREDENTIALS  /  MOCK DATA  (pick one)
Config File   : CONFIG_PATH  (or "N/A — mock mode")
Streams Scope : ${input:streamsToTest}  (or "ALL")
Run Date      : <current datetime>

────────────────────────────────────────────────────────────────────
DISCOVERY
────────────────────────────────────────────────────────────────────
  Status          : ✅ PASSED  /  ❌ FAILED  /  ⚠️ PASSED WITH WARNINGS
  Streams found   : <N>
  Streams listed  : <comma-separated names>
  Schema issues   : <count or "None">
  Detail          : <any warnings or errors found>

────────────────────────────────────────────────────────────────────
DATA SYNC
────────────────────────────────────────────────────────────────────
  Status          : ✅ PASSED  /  ❌ FAILED  /  ⚠️ PASSED WITH WARNINGS
  SCHEMA messages : <N>
  RECORD messages : <N total>
  STATE  messages : <N>
  Per-stream records:
    - <stream_1>  : <N> records
    - <stream_2>  : <N> records
    ...
  Protocol issues : <count or "None">

────────────────────────────────────────────────────────────────────
OVERALL RESULT
────────────────────────────────────────────────────────────────────
  ✅ ALL CHECKS PASSED
  — or —
  ❌ <N> issue(s) found (see details above)

────────────────────────────────────────────────────────────────────
NEXT STEPS
────────────────────────────────────────────────────────────────────
  [If mock mode]:
    1. Obtain real test account credentials and create config.json
    2. Re-run this prompt with hasTestAccount=yes to validate against live data
    3. Check that replication keys / bookmarks work correctly with real data

  [If real credentials mode]:
    1. Review per-stream record counts — 0 records may indicate a filter issue
    2. Confirm bookmarking works: re-run with --state to check incremental sync
    3. Run unit tests: python -m pytest tests/unittests/ -v
    4. Commit catalog.json and any fixes to a feature branch
```

---

## Important Rules

1. **Never print raw credential values** — only print config key names
2. **Never modify the original tap source code** to make tests pass — fix the test/runner only
3. **Fix errors autonomously** — read error output, diagnose, fix, retry once; report failure if still broken
4. **Replace all placeholder tokens** (`TAP_NAME`, `TAP_MODULE`, `CONFIG_PATH`, `XXXX`) with actual values before running any command or script
5. **Always validate JSON** — every catalog and sync output must be valid JSON or JSONL; report malformed lines as errors
6. **Do not skip steps** — even if discovery fails, attempt the sync step with available data (catalog from schemas or mock catalog) and report both results independently
