---
mode: agent
description: >
  Deep-audit every JSON schema file in a Singer tap. Checks Singer conventions
  (nullable fields, key_properties, replication keys, $ref resolution, metadata)
  and reports issues with suggested fixes. Produces an annotated audit report.
inputs:
  - id: tapDirectory
    description: "Full local path to the tap  e.g. C:\\Users\\you\\workspace\\taps\\tap-klaviyo"
    type: promptString
  - id: streamsToAudit
    description: "Comma-separated stream names to audit (blank = all streams)"
    type: promptString
    default: ""
  - id: fixIssues
    description: "yes = auto-fix safe issues (nullable fields, missing key_properties list) | no = report only"
    type: promptString
    default: "no"
---

# Singer Tap — Schema Audit

You are an expert in Singer tap development and JSON Schema conventions.
Your job is to **audit every schema file** in the tap and report (and optionally fix) issues.

Follow every step in order. Never delete schema files. Fix errors autonomously.

**Inputs:**
- Tap directory   : `${input:tapDirectory}`
- Streams to audit: `${input:streamsToAudit}` *(blank = all)*
- Auto-fix issues : `${input:fixIssues}`

---

## Step 1 — Discover Tap Module & Schema Files

Derive `TAP_MODULE` from the directory:
```bash
Get-ChildItem "${input:tapDirectory}" -Directory | Where-Object Name -like "tap_*" | Select-Object -First 1 -ExpandProperty Name
```

List all schema files:
```bash
Get-ChildItem "${input:tapDirectory}\TAP_MODULE\schemas" -Filter "*.json" -Recurse | Select-Object FullName, Name
```

If no `schemas/` folder is found, check for alternative locations:
```bash
Get-ChildItem "${input:tapDirectory}" -Recurse -Filter "*.json" | Where-Object { $_.FullName -like "*schema*" }
```

Build `SCHEMA_FILES` list. If `${input:streamsToAudit}` is NOT blank, filter to only those streams.

---

## Step 2 — Load Source Code for Context

Read these files to understand the tap's replication methods and key properties:
- `${input:tapDirectory}/TAP_MODULE/__init__.py`
- `${input:tapDirectory}/TAP_MODULE/streams.py` *(if it exists)*

Record for each stream:
- `key_properties` declared in source
- `replication_method` (`FULL_TABLE` or `INCREMENTAL`)
- `replication_keys` (for `INCREMENTAL`)

---

## Step 3 — Run Schema Audit

Run this Python audit script. **Replace `TAP_MODULE` and directory paths with real values** before executing:

```python
import json, os, sys

SCHEMAS_DIR = r"${input:tapDirectory}\TAP_MODULE\schemas"
SHARED_DIR  = os.path.join(SCHEMAS_DIR, "shared")
FILTER = [s.strip() for s in "${input:streamsToAudit}".split(",") if s.strip()]

# ── Load shared $ref schemas ──────────────────────────────────────────────────
shared_refs = {}
if os.path.isdir(SHARED_DIR):
    for fname in os.listdir(SHARED_DIR):
        if fname.endswith(".json"):
            with open(os.path.join(SHARED_DIR, fname)) as f:
                shared_refs[f"shared/{fname}"] = json.load(f)

def resolve_refs(schema, refs):
    """Recursively resolve $ref references."""
    if "$ref" in schema:
        ref_key = schema["$ref"]
        return resolve_refs(refs.get(ref_key, {}), refs)
    if "properties" in schema:
        schema["properties"] = {
            k: resolve_refs(v, refs) for k, v in schema["properties"].items()
        }
    for key in ("items", "additionalProperties"):
        if key in schema:
            schema[key] = resolve_refs(schema[key], refs)
    return schema

# ── Audit helpers ─────────────────────────────────────────────────────────────
def check_property(name, pdef, path=""):
    issues = []
    full_path = f"{path}.{name}" if path else name
    ptype = pdef.get("type", [])
    if isinstance(ptype, str):
        ptype = [ptype]

    # Rule 1: All fields must be nullable
    if ptype and "null" not in ptype:
        issues.append((full_path, "NOT_NULLABLE", f"type={ptype} — add 'null' to type array"))

    # Rule 2: date-time fields must have format
    if "string" in ptype and pdef.get("format") in (None, "") and ("_at" in name or "date" in name or "time" in name):
        issues.append((full_path, "MISSING_FORMAT", f"field name suggests date/time but no format:date-time set"))

    # Rule 3: Nested object properties should also be audited
    if "properties" in pdef:
        for sub_name, sub_def in pdef["properties"].items():
            issues.extend(check_property(sub_name, sub_def, path=full_path))

    return issues

results = {}
for fname in sorted(os.listdir(SCHEMAS_DIR)):
    if not fname.endswith(".json"):
        continue
    stream_name = fname[:-5]
    if FILTER and stream_name not in FILTER:
        continue

    with open(os.path.join(SCHEMAS_DIR, fname)) as f:
        raw_schema = json.load(f)
    schema = resolve_refs(json.loads(json.dumps(raw_schema)), shared_refs)

    issues = []
    props = schema.get("properties", {})

    # Rule 4: Schema must have properties
    if not props:
        issues.append(("(root)", "NO_PROPERTIES", "schema has no properties defined"))

    # Rule 5: Each property checked
    for field_name, field_def in props.items():
        issues.extend(check_property(field_name, field_def))

    results[stream_name] = {
        "field_count": len(props),
        "issues": issues,
        "raw_schema": raw_schema,
    }

# ── Print report ──────────────────────────────────────────────────────────────
total_issues = 0
issue_streams = []
for stream, data in sorted(results.items()):
    i_count = len(data["issues"])
    total_issues += i_count
    status = "✅" if i_count == 0 else f"⚠️  {i_count} issue(s)"
    print(f"\n{'─'*60}")
    print(f"Stream : {stream}  ({data['field_count']} fields)  {status}")
    if data["issues"]:
        issue_streams.append(stream)
        for (path, code, msg) in data["issues"]:
            print(f"  [{code}]  {path}: {msg}")

print(f"\n{'═'*60}")
print(f"Streams audited  : {len(results)}")
print(f"Total issues     : {total_issues}")
print(f"Streams with issues: {issue_streams or 'None'}")
```

Save the results for reference.

---

## Step 4 — Cross-Check Metadata Against Source Code

For each stream found in Step 2 source analysis, verify:

| Check | Rule |
|---|---|
| `key_properties` in schema | Every field listed as a `key_property` must exist in `schema.properties` |
| `replication_key` in schema | For `INCREMENTAL` streams, the `replication_key` field must exist in `schema.properties` |
| `tap_stream_id` uniqueness | No two streams share the same `tap_stream_id` |

Report any mismatches:
```
[MISSING_KEY_PROPERTY]  stream=<name>  field=<field>  — declared as key_property but not in schema
[MISSING_REP_KEY]       stream=<name>  field=<field>  — declared as replication_key but not in schema
```

---

## Step 5 — Auto-Fix (only if `${input:fixIssues}` = `yes`)

**SAFE fixes only** (apply without human review):

| Issue code | Fix applied |
|---|---|
| `NOT_NULLABLE` | Wrap type: if `"type": "string"` → `"type": ["string", "null"]`; if `"type": ["string"]` → `"type": ["string", "null"]` |
| `MISSING_FORMAT` | Add `"format": "date-time"` to date/time-named string fields |

**Never auto-fix:**
- `NO_PROPERTIES` — requires human review to add fields
- `MISSING_KEY_PROPERTY` / `MISSING_REP_KEY` — requires source code change

For each fix applied:
1. Read the original schema file
2. Apply the minimal change
3. Write the updated file back
4. Log: `FIXED  [<stream>] <field> — <what was changed>`

After all fixes, re-run the audit script to confirm issues are resolved.

---

## Step 6 — Final Audit Report

```
╔══════════════════════════════════════════════════════════════════╗
║             Singer Tap Schema Audit Report                       ║
╚══════════════════════════════════════════════════════════════════╝

Tap       : TAP_NAME
Directory : ${input:tapDirectory}
Scope     : ${input:streamsToAudit} (or ALL)
Auto-fix  : ${input:fixIssues}

────────────────────────────────────────────────────────────────────
SCHEMA FILE AUDIT
────────────────────────────────────────────────────────────────────
  Streams audited   : <N>
  Fields checked    : <N total across all streams>
  Issues found      : <N>
  Issues fixed      : <N>  (if fixIssues=yes)
  Remaining issues  : <N>

  Issue breakdown:
    NOT_NULLABLE    : <N> fields
    MISSING_FORMAT  : <N> fields
    NO_PROPERTIES   : <N> schemas
    MISSING_KEY_PROP: <N> streams

────────────────────────────────────────────────────────────────────
PER-STREAM SUMMARY
────────────────────────────────────────────────────────────────────
  ✅ <stream_name>  (<N> fields, 0 issues)
  ⚠️  <stream_name>  (<N> fields, <N> issues)
  ...

────────────────────────────────────────────────────────────────────
NEXT STEPS
────────────────────────────────────────────────────────────────────
  1. Fix any remaining NOT_NULLABLE issues manually or re-run with fixIssues=yes
  2. Review MISSING_KEY_PROPERTY and MISSING_REP_KEY in source code
  3. Run discovery test to confirm schemas load cleanly in Singer pipeline
     → singer-tap-discovery-sync-test  (hasTestAccount=no)
  4. Run unit tests to ensure metadata tests still pass
     → singer-tap-unittests
```

---

## Important Rules

- Never delete schema files, even if they are empty or problematic.
- Never change `key_properties` or `replication_method` — these are source-code decisions.
- If a fix would change the meaning of a field (e.g. remove a required field), skip the fix and add a `MANUAL_REVIEW_NEEDED` note instead.
- Always back up schema files before auto-fixing: copy to `<file>.bak` first.
