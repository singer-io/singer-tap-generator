# Code Review — `validate_singer_schemas.py`

Reviewed on: 2026-05-01  
File: `utils/validate_singer_schemas.py`

---

## How to Run

### Prerequisites

```bash
# No extra dependencies required — only Python stdlib is used
python --version   # Python 3.8+
```

### Validate a directory of schema JSON files

```bash
# Basic usage — validate all .json files in a schemas/ directory
python validate_singer_schemas.py path/to/tap_name/schemas

# Example using a real tap
python validate_singer_schemas.py C:/Users/Documents/workspace/taps/tap-ebay/tap_ebay/schemas
```

### Validate a Singer catalog file

```bash
# Validate schemas embedded inside a catalog.json produced by --discover
python validate_singer_schemas.py --catalog path/to/catalog.json

# Example
python validate_singer_schemas.py --catalog C:/Users/atul.tiwari2/Documents/workspace/taps/tap-ebay/catalog.json
```

### Strict mode (for CI/CD — exits with code 1 on any issue)

```bash
python validate_singer_schemas.py --catalog catalog.json --strict
```

### Typical workflow (discover → validate)

```bash
# Step 1 — run tap discovery to produce a catalog
tap-ebay --config config.json --discover > catalog.json

# Step 2 — validate the catalog
python validate_singer_schemas.py --catalog catalog.json

# Step 3 — strict validation for CI
python validate_singer_schemas.py --catalog catalog.json --strict && echo "Schemas OK"
```

### Arguments reference

| Argument | Type | Description |
|---|---|---|
| `schema_directory` | positional (optional) | Path to a directory of `.json` schema files |
| `--catalog` | option | Path to a Singer catalog JSON file |
| `--strict` | flag | Exit with code `1` if any errors or warnings are found |

> **Note:** `schema_directory` and `--catalog` are mutually exclusive. One of them must always be provided.

---

## Section 1 — Bugs

---

### B-01 · `check_indentation` is a dead method

**Lines:** 95–100

**Problem:**  
The method body is a single `pass` with a comment saying the check is disabled. It is still called from `validate_file`, and the module docstring still lists "Inconsistent indentation" as an active check. Users see it listed as a capability but it silently does nothing.

**Fix:**  
Either implement the check or remove the method and its call in `validate_file`, and remove the docstring entry.

---

### B-02 · `check_non_nullable_fields` — broken object-type guard

**Lines:** 299–301

**Problem:**  
```python
if not is_nullable and schema_type != 'object':
```
When `schema_type` is a list like `["null", "object"]` or `["string", "object"]`, the comparison `schema_type != 'object'` is always `True` (a list never equals a string). This causes incorrect warnings to fire for object-typed fields defined with array-style types.

**Fix:**
```python
types = schema_type if isinstance(schema_type, list) else [schema_type]
if not is_nullable and 'object' not in types and 'array' not in types:
```

---

### B-03 · `check_non_nullable_fields` — array-type guard is asymmetric with the object guard

**Lines:** 302–303

**Problem:**  
The array guard correctly handles both string and list forms:
```python
if schema_type != 'array' and not (isinstance(schema_type, list) and 'array' in schema_type):
```
But the object guard on the line above only handles the single-string case (see B-02). The two guards apply inconsistent logic for the same pattern, making the code hard to reason about.

**Fix:**  
Consolidate both guards into a single `types` list check as shown in B-02.

---

### B-04 · `validate_catalog_file` picks root metadata by index `[0]` instead of by breadcrumb

**Lines:** 570–572

**Problem:**  
```python
root_md = metadata[0] if metadata and isinstance(metadata, list) else None
```
In a Singer catalog, the metadata list contains one entry per field breadcrumb. The root-level entry (`breadcrumb == []`) is **not always at index 0** — taps may emit field metadata before the root entry. This means `check_root_level_md` is almost always called with the wrong entry (a field-level metadata dict), producing false errors.

**Fix:**
```python
root_md = next(
    (m for m in metadata if isinstance(m, dict) and m.get('breadcrumb') == []),
    None
)
```

---

### B-05 · `check_root_level_md` reports unknown metadata keys as `ERROR`

**Lines:** 451–458

**Problem:**  
```python
self.add_issue(..., 'ERROR', f'Root level metadata key "{key}" is not a standard discoverable key...')
```
The Singer spec allows any custom keys in metadata. Non-standard keys such as `selected`, `database-name`, or any tap-specific custom key that is not in the hardcoded `RootLevelMetadataKeywords` list will produce false-positive ERRORs on every real catalog. This makes the validator unusable on many real taps without suppression.

**Fix:**  
Downgrade to `WARNING`:
```python
self.add_issue(..., 'WARNING', f'Unexpected metadata key "{key}" — not in known Singer metadata keys')
```

---

### B-06 · Commented-out path output in `print_report` silently discards per-field detail

**Lines:** 650–654, 662–666

**Problem:**  
```python
# if paths:
#     for path in sorted(paths):
#         print(f"  • {path}")
```
The `_group_issues` method collects per-field `path` data into lists, but the output lines are commented out. When a check fires on 50 fields across a large schema, the report shows only one message with no indication of which fields triggered it. The path collection runs but the data is always discarded.

**Fix:**  
Uncomment and enable the path output, or — if deduplication is the goal — embed the path directly in the message string and stop collecting it separately.

---

### B-07 · `check_datetime_format` does not recurse into `oneOf/anyOf/allOf`

**Lines:** ~190–205

**Problem:**  
Every other recursive check method (`check_valid_types`, `check_object_properties`, `check_non_nullable_fields`) includes a loop over composition keywords:
```python
for key in ['oneOf', 'anyOf', 'allOf']:
    if key in schema:
        ...
```
`check_datetime_format` is missing this loop entirely. Timestamp fields inside a composition schema are silently skipped.

**Fix:**  
Add the same composition recursion loop at the end of `check_datetime_format`.

---

## Section 2 — Logic / Correctness Issues

---

### L-01 · `check_root_schema_structure` raises ERROR for missing `additionalProperties` even when `properties` is defined

**Lines:** 361–365

**Problem:**  
```python
if 'additionalProperties' not in schema:
    self.add_issue(..., 'ERROR', 'Root schema missing "additionalProperties" constraint')
```
The companion check in `check_object_properties` correctly only requires `additionalProperties` when `properties` is **empty**. But `check_root_schema_structure` requires it unconditionally on the root schema — even when the root has a full `properties` block. Most real Singer tap schemas have `properties` but no `additionalProperties`, so this ERROR fires on every valid schema.

**Fix:**  
Apply the same condition as `check_object_properties`: only require `additionalProperties` when `properties` is also absent, or downgrade to WARNING.

---

### L-02 · `validate_catalog_file` uses `return False` for early exits instead of recording issues via `add_issue`

**Lines:** 464–482

**Problem:**  
```python
if not self.catalog_file.exists():
    print(f"Error: Catalog file '{self.catalog_file}' does not exist")
    return False
```
These early exits bypass `add_issue` entirely. When `print_report()` is called after such a failure it will show "✅ All schemas are valid! No issues found." — a false success message — because `self.issues` was never populated.

**Fix:**  
Route all failures through `add_issue` before returning:
```python
if not self.catalog_file.exists():
    self.add_issue(str(self.catalog_file), 'ERROR', 'Catalog file does not exist')
    return False
```

---

### L-03 · `SchemaValidator` is stateful and cannot be safely reused

**Lines:** 72–75

**Problem:**  
`self.issues` and `self.warnings` accumulate across calls. If `validate_all()` or `validate_file()` is called multiple times on the same instance (e.g. in a test suite or batch runner), issues from earlier runs bleed into later reports, inflating counts and mixing results.

**Fix:**  
Add a `reset()` method that clears both lists, and call it at the start of `validate_all()`.

---

### L-04 · `RootLevelMetadataKeywords.expected_keys()` re-constructs the frozenset on every call

**Lines:** 48–63

**Problem:**  
The `@classmethod` builds a new `frozenset({cls.SELECTED, cls.REPLICATION_METHOD, ...})` every time it is called. Since `check_root_level_md` calls it once per metadata key per stream, this creates unnecessary allocations on large catalogs.

**Fix:**  
Define the frozenset as a class-level constant:
```python
class RootLevelMetadataKeywords:
    ...
    EXPECTED_KEYS: frozenset = frozenset({'selected', 'replication-method', ...})
```

---

### L-05 · `print_report` can produce a false "all valid" message when called before `validate_all`

**Lines:** 630–637

**Problem:**  
There is no guard preventing `print_report()` from being called before any validation has run. A caller who forgets `validate_all()` gets `"✅ All schemas are valid! No issues found."` — a completely misleading output.

**Fix:**  
Add a `_validated` flag set to `True` when `validate_all()` completes; `print_report()` should assert or warn if it is called before validation.

---

## Section 3 — Design Issues

---

### D-01 · `validate_file` returns `None`; `validate_catalog_file` returns `bool` — inconsistent

**Lines:** 369, 462

**Problem:**  
Sibling methods in the same class have different return types. `validate_all` cannot uniformly check the result of `validate_file` the same way it checks `validate_catalog_file`.

**Fix:**  
Change `validate_file` signature and return value to `bool`, consistent with `validate_catalog_file`.

---

### D-02 · `argparse` is imported inside `main()` instead of at the top of the file

**Line:** 675

**Problem:**  
All other imports are at module top. `import argparse` inside a function is only warranted for optional/slow imports. `argparse` is a stdlib module and should be at the top.

---

### D-03 · `validate_file` prints a line per file unconditionally — noisy in CI

**Line:** 371

**Problem:**  
```python
print(f"Validating {file_path.name}...")
```
On a schema directory with 50 files, this floods stdout with 50 progress lines. There is no `--verbose` or `--quiet` flag to suppress this.

**Fix:**  
Add a `verbose: bool = False` parameter to `__init__` (settable via a `--verbose` CLI flag) and gate these prints behind it.

---

### D-04 · `validate_file` reads file with no encoding specified — breaks on Windows

**Line:** 377

**Problem:**  
```python
content = file_path.read_text()
```
On Windows, the default encoding is `cp1252`. A schema file with any UTF-8 characters (accented names, arrow symbols, non-ASCII descriptions) will raise a `UnicodeDecodeError`, caught by the bare `except Exception` and reported as an unhelpful "Could not read file" error.

**Fix:**
```python
content = file_path.read_text(encoding='utf-8')
```

---

## Section 4 — Minor / Style Issues

| ID | Location | Description |
|---|---|---|
| M-01 | Line ~129 | `additional_props` variable is assigned but never read (only presence via `'additionalProperties' not in schema` is checked) |
| M-02 | Line ~97 | `check_indentation` has a `content: str` parameter that is never used since the method is empty |
| M-03 | Line ~458 | Typo in error message: `"does not uses dot escape notation"` → `"does not use dot-notation"` |
| M-04 | Line ~23 | `Tuple` is imported from `typing` but only used in one place; on Python 3.9+ the built-in `tuple[bool, dict]` can be used instead |
| M-05 | Whole file | `check_object_properties`, `check_valid_types`, `check_non_nullable_fields` all repeat the same 4-level recursion pattern (properties, items-dict, items-list, oneOf/anyOf/allOf). Consider extracting a `_recurse(method, schema, path)` helper to eliminate ~30 lines of repetition |
| M-06 | Lines 1–18 | **Module-level docstring has no `Usage` or `Examples` section.** The only usage examples exist inside the `argparse` epilog in `main()` — visible only via `--help`. A developer reading the source file directly has no quick-start guidance. The docstring should include at minimum: `python validate_singer_schemas.py <schemas_dir>` and `python validate_singer_schemas.py --catalog catalog.json --strict`. |

---

## Summary

| Category | Count |
|---|---|
| 🔴 Bugs | 7 |
| 🟠 Logic / Correctness | 5 |
| 🟡 Design | 4 |
| 🔵 Minor / Style | 6 |
| **Total** | **22** |

### Priority Fixes (cause incorrect results on real catalogs)

| Priority | ID | Issue |
|---|---|---|
| 1 | B-04 | Root metadata picked by index `[0]` instead of breadcrumb `[]` — wrong entry validated on most catalogs |
| 2 | B-05 | Unknown metadata keys reported as ERROR — false positives on every real tap |
| 3 | L-01 | `additionalProperties` ERROR fires on all schemas that have `properties` — almost always a false positive |
| 4 | B-02/B-03 | Broken object/array type guard in `check_non_nullable_fields` — incorrect warnings on list-typed fields |
| 5 | B-06 | Commented-out path output — all per-field location detail is silently discarded from the report |
| 6 | L-02 | Early-exit failures bypass `add_issue` — report shows false "all valid" after catalog-not-found errors |
