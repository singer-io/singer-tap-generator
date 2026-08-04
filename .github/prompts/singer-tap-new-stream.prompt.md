---
mode: agent
description: >
  Scaffold a brand-new stream end-to-end in a Singer tap: creates the JSON schema
  file, adds sync logic (incremental or full-table), registers the stream in the
  catalog/discovery layer, and generates a unit test skeleton — all following the
  existing tap's conventions.
inputs:
  - id: tapDirectory
    description: "Full local path to the tap  e.g. C:\\Users\\you\\workspace\\taps\\tap-klaviyo"
    type: promptString
  - id: streamName
    description: "Lowercase snake_case name for the new stream  e.g. email_templates"
    type: promptString
  - id: apiEndpoint
    description: "Full API endpoint URL for this stream  e.g. https://api.example.com/v1/email_templates"
    type: promptString
  - id: replicationMethod
    description: "FULL_TABLE or INCREMENTAL"
    type: promptString
    default: "FULL_TABLE"
  - id: replicationKey
    description: "Field name used as replication key for INCREMENTAL streams (blank for FULL_TABLE)"
    type: promptString
    default: ""
  - id: keyProperties
    description: "Comma-separated primary key field name(s)  e.g. id"
    type: promptString
    default: "id"
  - id: sampleApiResponse
    description: "Paste a sample JSON API response (single record or array). Leave blank to generate a minimal stub schema."
    type: promptString
    default: ""
---

# Singer Tap — New Stream Scaffolder

You are an expert in Singer tap development.
Your job is to **add a new stream to an existing tap** end-to-end:
1. Infer or create the JSON schema
2. Register the stream in discovery
3. Add sync logic
4. Write a unit test skeleton

Follow every step in order. Never break existing streams. Fix errors autonomously.

**Inputs:**
- Tap directory       : `${input:tapDirectory}`
- Stream name         : `${input:streamName}`
- API endpoint        : `${input:apiEndpoint}`
- Replication method  : `${input:replicationMethod}`
- Replication key     : `${input:replicationKey}`
- Key properties      : `${input:keyProperties}`
- Sample API response : `${input:sampleApiResponse}`

---

## Step 1 — Analyse the Existing Tap

Read and understand the tap's current structure. Focus on:

1. `TAP_MODULE/__init__.py` — how streams are registered, constants `FULL_STREAMS` / `ENDPOINTS`, `do_sync()` routing
2. `TAP_MODULE/streams.py` or `TAP_MODULE/utils.py` — existing `Stream` class / stream definitions, `get_full_pulls()` / `get_incremental_pull()` patterns
3. `TAP_MODULE/schemas/` — one or two existing schema files to match formatting conventions
4. `tests/unittests/` — one existing unit test file to match patterns

Record:
- `TAP_NAME`  : derived from the directory name
- `TAP_MODULE`: underscored version of `TAP_NAME`
- Schema formatting style (how `type` arrays are written, whether `description` fields are used)
- How existing streams are registered (e.g. `FULL_STREAMS` list, `ENDPOINTS` dict, `STREAM_PARAMS_MAP`)

---

## Step 2 — Generate the JSON Schema

### 2a — Derive schema from sample response (if provided)

If `${input:sampleApiResponse}` is not blank, parse it:
```python
import json

sample_raw = '''${input:sampleApiResponse}'''
sample = json.loads(sample_raw)

# Unwrap if paginated response: {"data": [...]} or {"results": [...]}
if isinstance(sample, dict):
    for key in ("data", "results", "records", "items"):
        if key in sample and isinstance(sample[key], (list, dict)):
            sample = sample[key]
            break

# Take first record if list
if isinstance(sample, list) and sample:
    record = sample[0]
elif isinstance(sample, dict):
    record = sample
else:
    record = {}

def infer_type(value):
    """Infer Singer-convention JSON Schema type from a Python value."""
    if value is None:
        return {"type": ["null", "string"]}
    if isinstance(value, bool):
        return {"type": ["null", "boolean"]}
    if isinstance(value, int):
        return {"type": ["null", "integer"]}
    if isinstance(value, float):
        return {"type": ["null", "number"]}
    if isinstance(value, list):
        return {"type": ["null", "array"], "items": {}}
    if isinstance(value, dict):
        return {"type": ["null", "object"], "properties": {
            k: infer_type(v) for k, v in value.items()
        }}
    # string — check for date-time heuristic
    import re
    if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value):
        return {"type": ["null", "string"], "format": "date-time"}
    return {"type": ["null", "string"]}

# Flatten attributes sub-key if present (common Singer pattern)
if "attributes" in record and isinstance(record["attributes"], dict):
    merged = {**record, **record.pop("attributes")}
    record = merged

properties = {field: infer_type(val) for field, val in record.items()}

schema = {
    "type": ["null", "object"],
    "additionalProperties": False,
    "properties": properties
}

print(json.dumps(schema, indent=2))
```

### 2b — Minimal stub schema (no sample response)

If no sample is provided, create a stub:
```json
{
  "type": ["null", "object"],
  "additionalProperties": false,
  "properties": {
    "id": {
      "type": ["null", "string"]
    },
    "created_at": {
      "type": ["null", "string"],
      "format": "date-time"
    },
    "updated_at": {
      "type": ["null", "string"],
      "format": "date-time"
    }
  }
}
```

Add the `${input:replicationKey}` field if `${input:replicationMethod}` is `INCREMENTAL` and it is not already in the properties.

### 2c — Write the schema file

Write the generated schema to:
```
${input:tapDirectory}/TAP_MODULE/schemas/${input:streamName}.json
```

Confirm the file was written and is valid JSON:
```bash
python -c "import json; json.load(open(r'${input:tapDirectory}\TAP_MODULE\schemas\${input:streamName}.json')); print('valid JSON')"
```

---

## Step 3 — Register the Stream in Discovery

Open `TAP_MODULE/__init__.py` and add the new stream following the **exact same pattern** as the nearest similar existing stream (FULL_TABLE or INCREMENTAL).

### 3a — Add endpoint

Add to the `ENDPOINTS` dict (if one exists):
```python
'${input:streamName}': '${input:apiEndpoint}',
```

### 3b — Add to stream params map (if FULL_TABLE)

If `${input:replicationMethod}` is `FULL_TABLE`, add a params entry to `STREAM_PARAMS_MAP` (or equivalent) following existing conventions:
```python
'${input:streamName}': [
    {}   # or the required query params from the API docs
],
```

### 3c — Register the Stream object

If using an explicit `FULL_STREAMS` list, add a new `Stream(...)` object:
```python
NEW_STREAM_NAME = Stream(
    '${input:streamName}',
    '${input:streamName}',
    [<key_properties>],       # from ${input:keyProperties}
    '${input:replicationMethod}',
    replication_keys=[<rep_key>] if INCREMENTAL else None
)
```
Then append it to `FULL_STREAMS` (if FULL_TABLE) or handle via metrics/events pattern (if INCREMENTAL).

### 3d — Add sync routing in `do_sync()`

Check `do_sync()` — if the routing is automatic (e.g. based on `FULL_STREAMS` membership), no change needed. If it's explicit, add a branch for the new stream.

---

## Step 4 — Add Sync Logic

### 4a — For FULL_TABLE streams

If the tap uses `get_full_pulls()` or equivalent, the routing should be automatic via `STREAM_PARAMS_MAP`. Verify by tracing `do_sync()` → `get_full_pulls(stream, ENDPOINTS['${input:streamName}'], headers)`.

If NOT automatic, add explicit call in `do_sync()`.

### 4b — For INCREMENTAL streams

Add the stream to `EVENT_MAPPINGS` (if metric-based) or add explicit routing in `do_sync()` calling `get_incremental_pull(stream, ENDPOINTS['${input:streamName}'], state, headers, start_date)`.

---

## Step 5 — Write Unit Tests

Create or append to `tests/unittests/test_${input:streamName}.py`:

```python
"""
Unit tests for the ${input:streamName} stream in TAP_MODULE.
Covers: schema shape, sync routing, and Singer output correctness.
"""
import json
import unittest
from unittest import mock

import TAP_MODULE
from TAP_MODULE.utils import get_full_pulls  # or get_incremental_pull


class MockParseArgs:
    def __init__(self, config=None):
        self.config = config or {}


class Test${StreamClass}Schema(unittest.TestCase):

    def test_schema_file_is_valid_json(self):
        """Verify the schema file for ${input:streamName} is valid JSON."""
        import os
        schema_path = os.path.join(
            os.path.dirname(TAP_MODULE.__file__),
            "schemas", "${input:streamName}.json"
        )
        with open(schema_path) as f:
            schema = json.load(f)
        self.assertIn("properties", schema)

    def test_key_properties_exist_in_schema(self):
        """Verify all key_properties are present in the schema."""
        from TAP_MODULE import discover
        import os
        schema_path = os.path.join(
            os.path.dirname(TAP_MODULE.__file__),
            "schemas", "${input:streamName}.json"
        )
        with open(schema_path) as f:
            schema = json.load(f)
        key_props = [k.strip() for k in "${input:keyProperties}".split(",")]
        for kp in key_props:
            self.assertIn(kp, schema.get("properties", {}),
                          f"key_property '{kp}' missing from ${input:streamName} schema")

    def test_all_fields_are_nullable(self):
        """Verify every field has 'null' in its type array (Singer convention)."""
        import os
        schema_path = os.path.join(
            os.path.dirname(TAP_MODULE.__file__),
            "schemas", "${input:streamName}.json"
        )
        with open(schema_path) as f:
            schema = json.load(f)
        for field, definition in schema.get("properties", {}).items():
            ftype = definition.get("type", [])
            if isinstance(ftype, list):
                self.assertIn("null", ftype, f"Field '{field}' is not nullable")


@mock.patch("singer.utils.parse_args")
@mock.patch("requests.Session.request")
class Test${StreamClass}Sync(unittest.TestCase):

    def _mock_empty_page(self, mock_req, mock_parse_args):
        mock_parse_args.return_value = MockParseArgs()
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": [], "links": {"next": None}}
        mock_req.return_value = resp

    @mock.patch("singer.write_record")
    @mock.patch("singer.write_schema")
    def test_do_sync_writes_schema_for_stream(self, mock_ws, mock_wr, mock_req, mock_parse_args):
        """Verify do_sync emits write_schema for the ${input:streamName} stream when selected."""
        self._mock_empty_page(mock_req, mock_parse_args)
        catalog = {
            "streams": [{
                "stream": "${input:streamName}",
                "schema": {},
                "key_properties": ["id"],
                "metadata": [{"breadcrumb": [], "metadata": {"selected": True}}],
            }]
        }
        TAP_MODULE.do_sync({}, {}, catalog, {})
        schemas_written = [call[0][0] for call in mock_ws.call_args_list]
        self.assertIn("${input:streamName}", schemas_written)


if __name__ == "__main__":
    unittest.main()
```

**Before writing the file:** replace `TAP_MODULE` with the actual module name, and `${StreamClass}` with a PascalCase version of `${input:streamName}` (e.g. `email_templates` → `EmailTemplates`).

---

## Step 6 — Verify & Run Tests

Install if needed:
```bash
cd "${input:tapDirectory}"
pip install -e . -q
```

Run only the new stream's tests:
```bash
cd "${input:tapDirectory}"
python -m pytest tests/unittests/test_${input:streamName}.py -v
```

On failure: read the full error, fix the failing test, and re-run. Repeat until all pass.

Then run the full test suite to confirm no regressions:
```bash
python -m pytest tests/unittests/ -v --tb=short
```

---

## Step 7 — Final Summary

```
✅ New stream scaffolded: ${input:streamName}

Files created / modified:
  ✅ TAP_MODULE/schemas/${input:streamName}.json   (schema file)
  ✅ TAP_MODULE/__init__.py                         (stream registered, endpoint added)
  ✅ tests/unittests/test_${input:streamName}.py    (<N> unit tests)

Stream config:
  Replication  : ${input:replicationMethod}
  Key props    : ${input:keyProperties}
  Rep. key     : ${input:replicationKey}  (N/A for FULL_TABLE)
  Endpoint     : ${input:apiEndpoint}

All <N> tests passing.

Next steps:
  1. Expand schema with real API response fields (paste sample into this prompt)
  2. Add query params to STREAM_PARAMS_MAP if the endpoint needs filtering
  3. Run discovery test: singer-tap-discovery-sync-test  hasTestAccount=no
  4. Test with real credentials when available
  5. Commit: git add . && git commit -m "feat: add ${input:streamName} stream"
```

---

## Important Rules

- Never remove or modify existing streams — only add new ones.
- The new stream must follow the exact same naming, casing, and structural conventions as existing streams in the tap.
- If the tap doesn't have a `Stream` class, check `streams.py` for catalog dict creation patterns and follow those instead.
- Always run the full test suite after scaffolding to catch regressions.
