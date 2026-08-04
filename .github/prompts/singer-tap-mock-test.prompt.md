---
mode: agent
description: >
  Generate a catalog.json (discovery) and optionally run a mock sync for any
  Singer tap WITHOUT needing real API credentials.  Use this when you don't
  have a test account but still want to verify discovery output and sync
  plumbing are correct.
---

# Singer Tap — Mock Discovery & Sync (No Real Account)

## Objective

Given a Singer tap in this workspace, produce:

1. **`catalog.json`** — a fully-selected discovery catalog built directly
   from the tap's schemas, bypassing all API calls and client validation.
2. **`mock_sync_output.json`** (optional) — a synthetic Singer output file
   with fabricated SCHEMA, RECORD, and STATE messages for every selected
   stream, allowing end-to-end parsing tests without hitting any real API.

---

## Arguments

| Argument | Example | Description |
|---|---|---|
| `tap_name` | `tap-outreach` | Name of the tap (folder under `taps/`) |
| `tap_module` | `tap_outreach` | Python package name inside that folder |
| `streams_module` | `tap_outreach.sync.STREAM_CONFIGS` or `tap_outreach.schema.STREAMS` | Python expression that resolves to the stream registry (dict) |
| `key_properties` | `['id']` | Primary key list used for every stream (override per-stream if needed) |
| `mock_record_count` | `3` | Number of fake records to emit per stream in mock sync |

---

## Step 1 — Generate `catalog.json` (no client needed)

Run the following Python snippet from inside the tap directory.
**Do not modify any tap source files.**

```python
#!/usr/bin/env python3
"""
generate_catalog.py
Generate a fully-selected catalog.json for <tap_name> without real credentials.

Usage:
    cd taps/<tap_name>
    python generate_catalog.py
"""
import json, sys
sys.path.insert(0, '.')

# ── Import whichever helper the tap already has ──────────────────────────────
# Option A: tap has a get_schemas() function (most taps)
from <tap_module>.discover import get_schemas
from singer.catalog import Catalog, CatalogEntry, Schema

schemas, field_metadata = get_schemas()
catalog = Catalog([])
for stream_name, schema_dict in schemas.items():
    schema = Schema.from_dict(schema_dict)
    mdata = field_metadata[stream_name]
    catalog.streams.append(CatalogEntry(
        stream=stream_name,
        tap_stream_id=stream_name,
        key_properties=<key_properties>,   # e.g. ['id']
        schema=schema,
        metadata=mdata,
    ))

# Mark every stream and every field as selected
cat_dict = catalog.to_dict()
for stream in cat_dict['streams']:
    for entry in stream.get('metadata', []):
        entry['metadata']['selected'] = True

with open('catalog.json', 'w') as f:
    json.dump(cat_dict, f, indent=2)

print(f"catalog.json written — {len(cat_dict['streams'])} streams:")
for s in sorted(cat_dict['streams'], key=lambda x: x['tap_stream_id']):
    print(f"  {s['tap_stream_id']}")
```

### Adapting for taps with a STREAMS class registry

If the tap uses a `STREAMS` dict of stream classes (e.g. `tap_amazon_ads`),
replace the import block with:

```python
from <tap_module>.schema import get_schemas   # or wherever schemas live
```

---

## Step 2 — Generate `mock_sync_output.json` (optional)

This creates a valid Singer output file with fake data.
**No API calls are made.  No tap source code is changed.**

```python
#!/usr/bin/env python3
"""
generate_mock_sync.py
Emit synthetic Singer messages (SCHEMA + RECORDs + STATE) for every stream
in catalog.json so you can test parsers and downstream targets offline.

Usage:
    cd taps/<tap_name>
    python generate_mock_sync.py
"""
import json, copy, random, string
from datetime import datetime, timezone

MOCK_RECORD_COUNT = <mock_record_count>   # records per stream

def fake_value(schema_type):
    """Return a plausible fake value for a given JSON-schema type."""
    t = schema_type if isinstance(schema_type, str) else (schema_type[0] if schema_type else 'string')
    if t == 'integer':  return random.randint(1, 99999)
    if t == 'number':   return round(random.uniform(0, 1000), 2)
    if t == 'boolean':  return random.choice([True, False])
    if t == 'null':     return None
    # default → string
    return ''.join(random.choices(string.ascii_lowercase, k=8))

def fake_record(stream_name, schema, key_properties, idx):
    record = {}
    for prop, spec in schema.get('properties', {}).items():
        types = spec.get('type', 'string')
        if isinstance(types, list):
            types = [t for t in types if t != 'null']
        record[prop] = fake_value(types[0] if isinstance(types, list) and types else types)
    # Ensure primary key is a stable integer so records are unique
    for kp in key_properties:
        record[kp] = idx + 1
    # Common date fields
    now = datetime.now(timezone.utc).isoformat()
    for date_field in ('createdAt', 'updatedAt', 'eventAt', 'created_at', 'updated_at'):
        if date_field in record:
            record[date_field] = now
    return record

with open('catalog.json') as f:
    catalog = json.load(f)

messages = []
bookmarks = {}

for stream in catalog['streams']:
    tap_stream_id = stream['tap_stream_id']
    schema        = stream['schema']
    key_props     = stream.get('key_properties', ['id'])

    # SCHEMA message
    messages.append({"type": "SCHEMA", "stream": tap_stream_id,
                      "schema": schema, "key_properties": key_props})

    # RECORD messages
    for i in range(MOCK_RECORD_COUNT):
        messages.append({"type": "RECORD", "stream": tap_stream_id,
                          "record": fake_record(tap_stream_id, schema, key_props, i),
                          "time_extracted": datetime.now(timezone.utc).isoformat()})

    bookmarks[tap_stream_id] = datetime.now(timezone.utc).isoformat()

# Final STATE message
messages.append({"type": "STATE", "value": {"bookmarks": bookmarks}})

with open('mock_sync_output.json', 'w') as f:
    for msg in messages:
        f.write(json.dumps(msg) + '\n')

streams_emitted = len(catalog['streams'])
records_emitted = streams_emitted * MOCK_RECORD_COUNT
print(f"mock_sync_output.json written — {streams_emitted} streams, {records_emitted} records")
```

---

## Step 3 — Verify the output

```bash
# Count records per stream
python -c "
import json
from collections import Counter
counts = Counter()
with open('mock_sync_output.json') as f:
    for line in f:
        msg = json.loads(line)
        if msg['type'] == 'RECORD':
            counts[msg['stream']] += 1
for stream, n in sorted(counts.items()):
    print(f'  {stream}: {n}')
print(f'  TOTAL: {sum(counts.values())}')
"

# Validate catalog structure
python -c "
import json
cat = json.load(open('catalog.json'))
print(f'Streams: {len(cat[\"streams\"])}')
for s in cat['streams']:
    sel = any(e['metadata'].get('selected') for e in s.get('metadata', []) if e['breadcrumb'] == [])
    print(f'  {s[\"tap_stream_id\"]:35s}  selected={sel}')
"
```

---

## Key Constraints

- **No source code changes** — all steps run external Python scripts only
- **No real credentials required** — `config.json` is never read
- **Idempotent** — re-running overwrites `catalog.json` / `mock_sync_output.json`
- Works for any Singer tap that exposes a `get_schemas()` function or equivalent

---

## When to use this prompt

- You don't have a sandbox / test account for the API
- You want to verify the catalog schema structure after adding new streams
- You want to test a downstream Singer target locally using dummy records
- CI environments where secrets are unavailable (schema-only tests)
