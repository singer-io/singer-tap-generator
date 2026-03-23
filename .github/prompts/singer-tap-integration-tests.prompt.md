---
mode: agent
description: >
  Create or update the full integration test suite for a Singer tap using the
  tap-tester framework. Scaffolds tests/base.py (stream metadata, credentials,
  properties) and all canonical test files (discovery, bookmark, start_date,
  pagination, all_fields, automatic_fields, interrupted_sync). When testMode=mock,
  generates the same test files but calling tap functions directly via
  unittest.mock.patch — no live API account or tap-tester dependency required.
  Use testMode=live for live tap-tester tests, testMode=mock for mock-based tests.
inputs:
  - id: tapDirectory
    description: "Full local path to the tap  e.g. C:\\Users\\you\\workspace\\taps\\tap-drip"
    type: promptString
  - id: branchStrategy
    description: "Enter 'new' to create a new git branch, or 'existing' to use an existing branch"
    type: promptString
    default: "new"
  - id: branchName
    description: "Branch name — if strategy=new provide new name e.g. add-integration-tests; if strategy=existing provide the branch name to checkout"
    type: promptString
  - id: testMode
    description: "'live' = use real test account via tap-tester | 'mock' = generate mock-based tests using unittest.mock.patch on tap internals — no live API or credentials needed"
    type: promptString
    default: "live"
---

# Singer Tap — Integration Test Generator

You are an expert Singer tap developer. Your job is to create or update the full
integration test suite for a Singer tap using the **tap-tester** framework
(the `tap_tester.base_suite_tests` modern style).

All inputs:
- Tap directory  : `${input:tapDirectory}`
- Branch strategy: `${input:branchStrategy}`
- Branch name    : `${input:branchName}`
- Test mode      : `${input:testMode}`

---

## Step 1 — Understand the Tap's Source Code

Read the following files (they always exist in a Singer tap):

```
TAP_DIR/setup.py                        ← pip package name, tap module name
TAP_DIR/tap_<name>/streams.py  OR  catalog.py  OR  __init__.py
TAP_DIR/tap_<name>/schemas/             ← one .json per stream (stream names)
```

From the source code, collect for **every stream**:

| Field | Where to find it |
|---|---|
| `stream_name` | schema file names or `STREAMS` dict keys |
| `primary_keys` | `key_properties` in the catalog / stream definition |
| `replication_method` | `forced-replication-method` metadata or `replication_method` attribute |
| `replication_keys` | `valid-replication-keys` / `replication_key` attribute (empty set for FULL_TABLE) |
| `api_limit` | page size / `results_per_page` constant, default to `100` if not found |
| `obeys_start_date` | whether the stream filters by `start_date` — default to `False` if uncertain |
| `parent_stream` | if stream is a child, note the parent name; else `None` |

Build an internal table like:

```
stream_name          | primary_keys               | rep_method  | rep_keys       | api_limit | obeys_start_date | parent
─────────────────────────────────────────────────────────────────────────────────────────────
accounts             | {id}                       | FULL_TABLE  | set()          | 1         | False            | None
subscribers          | {id, account_id}           | INCREMENTAL | {updated_at}   | 100       | True             | accounts
```

Also note:
- **TAP_MODULE** = Python package name (e.g. `tap_drip`)
- **TAP_NAME** = pip/connection name from `setup.py` `name=` field (e.g. `tap-drip`)
- **TAP_ENV_PREFIX** = `TAP_` + TAP_NAME uppercased with hyphens replaced by underscores
  e.g. `tap-drip` → `TAP_DRIP_` | `tap-amazon-ads` → `TAP_AMAZON_ADS_`
- **CREDENTIAL_KEYS** = config keys that are secrets (API keys, tokens, passwords) —
  read them from the tap source: look for `config.get("...")` or `config["..."]` calls
  in `client.py`, `__init__.py`, or wherever the HTTP client is initialised.
  Exclude non-secret keys (`start_date`, `account_id` passed as a property, etc.).
- **ENV_VAR_NAME** for each credential key = `TAP_ENV_PREFIX` + key uppercased
  e.g. `api_token` → `TAP_DRIP_API_TOKEN` | `client_secret` → `TAP_AMAZON_ADS_CLIENT_SECRET`
- **CONNECTION_TYPE** = Stitch connection type slug — derived automatically:
  `"platform." + TAP_NAME.replace("tap-", "", 1).replace("-", "_")`
  Examples: `tap-drip` → `"platform.drip"` | `tap-amazon-ads` → `"platform.amazon_ads"`
- **HAS_INCREMENTAL_STREAMS** = True if any stream uses INCREMENTAL replication
- **HAS_PARENT_STREAMS** = True if any stream has a parent

---

## Step 2 — Git: Create or Checkout Branch

Derive the **parent branch** (run in terminal from TAP_DIR):

```bash
git -C "${input:tapDirectory}" symbolic-ref --short HEAD
```

If `${input:branchStrategy}` is **`new`**:

```bash
git -C "${input:tapDirectory}" checkout -b "${input:branchName}"
```

If `${input:branchStrategy}` is **`existing`**:

```bash
git -C "${input:tapDirectory}" checkout "${input:branchName}"
```

Verify the branch is active:

```bash
git -C "${input:tapDirectory}" branch --show-current
```

---

## Step 3 — Ensure `tests/` Directory Exists

```bash
mkdir -p "${input:tapDirectory}/tests"
```

Check whether these files already exist (do NOT overwrite without checking):
- `tests/base.py`
- `tests/test_discovery.py`
- `tests/test_bookmark.py`
- `tests/test_start_date.py`
- `tests/test_pagination.py`
- `tests/test_all_fields.py`
- `tests/test_automatic_fields.py`

If `${input:testMode}` is `mock`, also add to the checklist:
- `tests/test_interrupted_sync.py`

If a file **already exists**, read it and **merge** the new metadata — do not
delete any existing content (e.g. custom `test_*` methods or stream exclusions
the developer added). Only add missing streams or update stale metadata entries
when you are certain they are wrong.

If a file **does not exist**, create it fresh using the templates below.

---

## Step 4 — Generate `tests/base.py`

Use the stream table from Step 1.

### Template

```python
import os

from tap_tester.base_suite_tests.base_case import BaseCase


class <TapPascalCase>BaseTest(BaseCase):
    """Setup expectations for test sub classes.

    Metadata describing streams. A bunch of shared methods that are used
    in tap-tester tests. Shared tap-specific methods (as needed).
    """
    start_date = "2019-01-01T00:00:00Z"
    # Set PARENT_TAP_STREAM_ID only if the tap has child streams
    # PARENT_TAP_STREAM_ID = "parent-tap-stream-id"

    @staticmethod
    def tap_name():
        """The name of the tap."""
        return "<TAP_NAME>"

    @staticmethod
    def get_type():
        """The Stitch connection type slug."""
        # Derived from setup.py name: "platform." + name.replace("tap-","",1).replace("-","_")
        return "<CONNECTION_TYPE>"  # e.g. "platform.drip", "platform.amazon_ads"

    def get_properties(self, original: bool = True):
        """Configuration properties required for the tap."""
        return_value = {
            "start_date": self.start_date,
            # ADD any other required tap config keys here, e.g.:
            # "account_id": os.getenv("TAP_<NAME>_ACCOUNT_ID"),
        }
        if original:
            return return_value

        return_value["start_date"] = self.start_date
        return return_value

    @staticmethod
    def get_credentials():
        """Authentication information for the test account.
        Values are read from environment variables — never hardcode credentials.
        """
        return {
            # CREDENTIAL_KEY: os.getenv("ENV_VAR_NAME"),
            <CREDENTIAL_DICT>
        }

    @classmethod
    def expected_metadata(cls):
        """The expected streams and metadata about the streams."""
        return {
            <STREAM_METADATA_DICT>
        }
```

### Rules for filling the template

**`get_credentials()`**:
For each credential key found in Step 1, derive the env var name automatically:
`TAP_ENV_PREFIX + key.upper()`

Example for `tap-drip` with config key `api_token`:
```python
return {
    "api_token": os.getenv("TAP_DRIP_API_TOKEN"),
}
```

Example for `tap-amazon-ads` with keys `client_id`, `client_secret`, `refresh_token`:
```python
return {
    "client_id":     os.getenv("TAP_AMAZON_ADS_CLIENT_ID"),
    "client_secret": os.getenv("TAP_AMAZON_ADS_CLIENT_SECRET"),
    "refresh_token": os.getenv("TAP_AMAZON_ADS_REFRESH_TOKEN"),
}
```

**`expected_metadata()`**:
For every stream in the table from Step 1:

```python
"<stream_name>": {
    cls.PRIMARY_KEYS: {<primary_key_set>},
    cls.REPLICATION_METHOD: cls.FULL_TABLE,   # or cls.INCREMENTAL
    cls.REPLICATION_KEYS: set(),              # or {"updated_at"} for INCREMENTAL
    cls.OBEYS_START_DATE: False,              # True if stream respects start_date
    cls.API_LIMIT: <page_size>,
    # Only add the next line for child streams:
    # cls.PARENT_TAP_STREAM_ID: "<parent_stream_name>",
},
```

If `HAS_PARENT_STREAMS` is True, uncomment `PARENT_TAP_STREAM_ID = "parent-tap-stream-id"`
at the class level AND add the `cls.PARENT_TAP_STREAM_ID` key to each child stream entry.

---

## Step 5 — Generate `tests/test_discovery.py`

### Modern style (preferred — use this when tap uses BaseCase)

```python
"""Test tap discovery mode and metadata."""
from base import <TapPascalCase>BaseTest
from tap_tester.base_suite_tests.discovery_test import DiscoveryTest
# Only import menagerie if you override test_parent_stream
# from tap_tester import menagerie


class <TapPascalCase>DiscoveryTest(DiscoveryTest, <TapPascalCase>BaseTest):
    """Test tap discovery mode and metadata conforms to standards."""

    @staticmethod
    def name():
        return "tap_tester_<tap_underscored>_discovery_test"

    def streams_to_test(self):
        return self.expected_stream_names()
```

If **`HAS_PARENT_STREAMS` is True**, add this method after `streams_to_test()`:

```python
    def test_parent_stream(self):
        """Verify each stream's metadata includes the correct parent-tap-stream-id."""
        orphan_streams = {<streams_without_a_parent>}
        for stream in self.streams_to_test():
            with self.subTest(stream=stream):
                expected_parent = self.expected_parent_tap_stream(stream)

                catalog = [c for c in self.found_catalogs if c["stream_name"] == stream][0]
                metadata = menagerie.get_annotated_schema(
                    self.conn_id, catalog["stream_id"])["metadata"]
                stream_properties = [m for m in metadata if m.get("breadcrumb") == []]
                actual_parent = stream_properties[0].get("metadata", {}).get(
                    self.PARENT_TAP_STREAM_ID, None)

                stream_metadata = stream_properties[0]["metadata"]
                if stream not in orphan_streams:
                    self.assertIn(self.PARENT_TAP_STREAM_ID, stream_metadata)
                    self.assertTrue(isinstance(actual_parent, str))

                self.assertEqual(expected_parent, actual_parent,
                                 msg=f"Expected parent {expected_parent}, got {actual_parent}")
```

---

## Step 6 — Generate `tests/test_bookmark.py`

### If `HAS_INCREMENTAL_STREAMS` is **False** (all FULL_TABLE)

Emit a skipped stub:

```python
import unittest
from base import <TapPascalCase>BaseTest


class <TapPascalCase>BookMarkTest(<TapPascalCase>BaseTest):
    """Test tap sets a bookmark and respects it for the next sync of a stream.

    NOTE: Skipped because all streams use FULL_TABLE replication.
    Bookmark tests only apply to INCREMENTAL streams.
    """

    @staticmethod
    def name():
        return "tap_tester_<tap_underscored>_bookmark_test"

    @unittest.skip("All streams use FULL_TABLE replication — bookmark test not applicable")
    def test_run(self):
        pass
```

### If `HAS_INCREMENTAL_STREAMS` is **True**

Use the mix-in style:

```python
from base import <TapPascalCase>BaseTest
from tap_tester.base_suite_tests.bookmark_test import BookmarkTest


class <TapPascalCase>BookMarkTest(BookmarkTest, <TapPascalCase>BaseTest):
    """Test tap sets a bookmark and respects it for the next sync of a stream."""
    bookmark_format = "%Y-%m-%dT%H:%M:%S.%fZ"
    initial_bookmarks = {
        "bookmarks": {
            # "<stream_name>": {"<replication_key>": "2020-01-01T00:00:00Z"},
            <INITIAL_BOOKMARKS>
        }
    }

    @staticmethod
    def name():
        return "tap_tester_<tap_underscored>_bookmark_test"

    def streams_to_test(self):
        # Exclude streams that are FULL_TABLE or have insufficient test data
        streams_to_exclude = {
            <FULL_TABLE_STREAM_NAMES>
        }
        return self.expected_stream_names().difference(streams_to_exclude)
```

Fill `initial_bookmarks` with one entry per INCREMENTAL stream:
- key   = stream name
- value = `{replication_key: "2020-01-01T00:00:00Z"}`

Fill `streams_to_exclude` with all FULL_TABLE stream names.

---

## Step 7 — Generate `tests/test_start_date.py`

### If `HAS_INCREMENTAL_STREAMS` is **False**

Emit a skipped stub:

```python
from base import <TapPascalCase>BaseTest


class <TapPascalCase>StartDateTest(<TapPascalCase>BaseTest):
    """Note: Start date test not applicable — no INCREMENTAL streams."""

    @staticmethod
    def name():
        return "tap_tester_<tap_underscored>_start_date_test"

    def streams_to_test(self):
        return self.expected_stream_names()

    @property
    def start_date_1(self):
        return "2015-03-25T00:00:00Z"

    @property
    def start_date_2(self):
        return "2017-01-25T00:00:00Z"
```

### If `HAS_INCREMENTAL_STREAMS` is **True**

```python
from base import <TapPascalCase>BaseTest
from tap_tester.base_suite_tests.start_date_test import StartDateTest


class <TapPascalCase>StartDateTest(StartDateTest, <TapPascalCase>BaseTest):
    """Instantiate start date according to the desired data set and run the test."""

    @staticmethod
    def name():
        return "tap_tester_<tap_underscored>_start_date_test"

    def streams_to_test(self):
        # Exclude FULL_TABLE streams and any streams without sufficient test data
        streams_to_exclude = {
            <FULL_TABLE_STREAM_NAMES>
        }
        return self.expected_stream_names().difference(streams_to_exclude)

    @property
    def start_date_1(self):
        return "2015-03-25T00:00:00Z"

    @property
    def start_date_2(self):
        return "2017-01-25T00:00:00Z"
```

---

## Step 8 — Generate `tests/test_pagination.py`

Always generated — assertion that record count exceeds `API_LIMIT` (from `base.py`) is
handled automatically by `PaginationTest`.

```python
from tap_tester.base_suite_tests.pagination_test import PaginationTest
from base import <TapPascalCase>BaseTest


class <TapPascalCase>PaginationTest(PaginationTest, <TapPascalCase>BaseTest):
    """
    Ensure tap can replicate multiple pages of data for streams that use pagination.
    """

    @staticmethod
    def name():
        return "tap_tester_<tap_underscored>_pagination_test"

    def streams_to_test(self):
        # Exclude streams that don't have enough test data to exceed one page
        streams_to_exclude = set()
        return self.expected_stream_names().difference(streams_to_exclude)
```

**Exclusion guidance:** Any stream whose total record count in the test environment is
likely ≤ `API_LIMIT` should be added to `streams_to_exclude`.  Leave a comment explaining
why (e.g. `# sandbox has only 1 record`).

If the tap requires a reduced `page_size` property to force pagination at test time,
override `get_properties()` and add it there. Document it with a comment.

---

## Step 9 — Generate `tests/test_all_fields.py`

Always generated. Use the mix-in style:

```python
from base import <TapPascalCase>BaseTest
from tap_tester.base_suite_tests.all_fields_test import AllFieldsTest

# Declare known fields that exist in the schema but are NOT returned by the
# test-environment API (e.g. features not enabled in sandbox accounts).
# Leave empty if none are known.
KNOWN_MISSING_FIELDS = {
    # "<stream_name>": {"<field_name>", ...},
}


class <TapPascalCase>AllFields(AllFieldsTest, <TapPascalCase>BaseTest):
    """Ensure running the tap with all streams and fields selected results in
    the replication of all fields."""

    MISSING_FIELDS = KNOWN_MISSING_FIELDS

    @staticmethod
    def name():
        return "tap_tester_<tap_underscored>_all_fields_test"

    def streams_to_test(self):
        # Exclude streams with no test data or no API access in the test environment
        streams_to_exclude = set()
        return self.expected_stream_names().difference(streams_to_exclude)
```

**`KNOWN_MISSING_FIELDS` guidance:** Populate lazily — start with an empty dict and only
add entries after a first test run reveals fields present in the schema but absent from
actual records. Always leave an inline comment explaining why each field is missing.

---

## Step 10 — Generate `tests/test_automatic_fields.py`

Always generated (applies to both FULL_TABLE and INCREMENTAL streams):

```python
"""Test that with no fields selected, automatic fields are still replicated."""
from base import <TapPascalCase>BaseTest
from tap_tester.base_suite_tests.automatic_fields_test import MinimumSelectionTest


class <TapPascalCase>AutomaticFields(MinimumSelectionTest, <TapPascalCase>BaseTest):
    """Test that with no fields selected for a stream, automatic fields are still replicated."""

    @staticmethod
    def name():
        return "tap_tester_<tap_underscored>_automatic_fields_test"

    def streams_to_test(self):
        # Exclude streams with known missing test data
        streams_to_exclude = set()
        return self.expected_stream_names().difference(streams_to_exclude)
```

---

## Step 11 — Validate Generated Files

For each generated file, re-read it and verify:

1. No syntax errors — do a basic check by looking for unmatched braces/brackets
2. `tap_name()` matches the pip package name from `setup.py`
3. `get_type()` matches the derived CONNECTION_TYPE from Step 1
4. Every stream from Step 1's table is present in `expected_metadata()`
5. FULL_TABLE streams have `REPLICATION_KEYS: set()`
6. INCREMENTAL streams have at least one key in `REPLICATION_KEYS`
7. `initial_bookmarks` in `test_bookmark.py` only contains INCREMENTAL streams

If any check fails, fix the file before proceeding.

---

## Step 12 — Check for Env Var Validation in `base.py`

Using the credential env var names derived in Step 1, add a `setUp` guard so the
test fails fast with a clear message if credentials are missing:

```python
def setUp(self):
    missing = [v for v in ["TAP_<NAME>_<KEY>", ...]  # derived env var names
               if not os.getenv(v)]
    if missing:
        raise Exception(f"Missing required environment variables: {missing}")
```

Place this method in `base.py` right after `get_type()`.

> **Note:** When `${input:testMode}` is `mock`, Steps 4–12 (live tests) are skipped entirely.
> Mock tests run in CI without credentials; live tests (Steps 4–12) are generated only when `testMode=live`.

---

## Step 13 — Mock Testing Path *(only if `testMode` = `mock`)*

Skip this entire step if `${input:testMode}` is `live`.

### Key principles (derived from tap-listrak, tap-lever, tap-taboola)

| Aspect | Mock approach |
|---|---|
| HTTP interception | `@patch("tap_module.request")` or `@patch("tap_module.client.get")` on the **tap’s own internal request function** |
| Mock data | Inline list/dict class attributes in `base.py` **or** auto-generated from the tap’s own schema JSON files |
| Test runner | Plain `unittest` — no `tap_tester`, no `responses` library, no Singer/Stitch infrastructure |
| Config | Dummy hardcoded values — no env vars |
| Base class | Plain Python mixin (not a `TestCase`) — test classes inherit from BOTH the mixin AND `unittest.TestCase` |
| Test files | Same canonical names (`test_discovery.py`, `test_bookmark.py`, etc.) but implementations call tap functions directly |

---

### 13a — Update `tests/base.py` for mock mode

When `testMode` is `mock`, replace the `base.py` with the
mock style. The mock `base.py` is a **plain mixin class** — no `BaseCase` import,
no `tap_tester` dependency.

```python
import json
import os


class <TapPascalCase>BaseTest:
    """Base test case for <TAP_NAME> integration tests with mocked data.
    Not a TestCase itself — mix with unittest.TestCase in each test class."""

    PRIMARY_KEYS = "primary_keys"
    REPLICATION_METHOD = "replication_method"
    REPLICATION_KEYS = "replication_keys"
    OBEYS_START_DATE = "obeys_start_date"
    API_LIMIT = "api_limit"
    # PARENT = "parent"  # uncomment if tap has child streams

    default_start_date = "2020-01-01T00:00:00Z"

    @classmethod
    def expected_metadata(cls):
        """The expected streams and metadata about the streams."""
        return {
            <STREAM_METADATA_DICT>  # same dict as the live base.py
        }

    def setUp(self):
        """Set up test fixtures."""
        self.config = self.get_mock_config()
        self.state = {}

    def tearDown(self):
        """Clean up after tests."""
        pass

    @staticmethod
    def get_mock_config():
        """Return mock configuration with dummy values — no real credentials."""
        return {
            # TODO: fill with all required config keys using fake test values
            # e.g. "api_key": "mock_test_api_key",
            "start_date": "2020-01-01T00:00:00Z",
        }

    @staticmethod
    def get_mock_state():
        """Return initial mock state."""
        return {}

    # ── Schema-driven mock data generation ─────────────────────────────────
    # These helpers read the tap’s own JSON schema files and auto-generate
    # valid mock records — no hand-written fixtures required.

    @staticmethod
    def _schema_path(stream_name):
        base_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        return os.path.join(base_dir, "tap_<TAP_MODULE>", "schemas",
                            f"{stream_name}.json")

    @classmethod
    def _load_schema(cls, stream_name):
        with open(cls._schema_path(stream_name), "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _schema_type(schema):
        """Return concrete type, resolving null unions."""
        t = schema.get("type", "object")
        if isinstance(t, list):
            non_null = [x for x in t if x != "null"]
            return non_null[0] if non_null else "null"
        return t

    @staticmethod
    def _generate_value(schema, date_value="2024-01-01T00:00:00Z"):
        """Recursively generate one valid mock value for a JSON-schema fragment."""
        if "enum" in schema and schema["enum"]:
            return schema["enum"][0]

        schema_type = <TapPascalCase>BaseTest._schema_type(schema)
        if schema_type == "object":
            properties = schema.get("properties", {})
            return {
                key: <TapPascalCase>BaseTest._generate_value(val, date_value)
                for key, val in properties.items()
            }
        if schema_type == "array":
            return [<TapPascalCase>BaseTest._generate_value(
                schema.get("items", {"type": "string"}), date_value)]
        if schema_type == "string":
            fmt = schema.get("format")
            if fmt == "date-time":
                return date_value
            if fmt == "email":
                return "mock@example.com"
            return "mock"
        return {"integer": 1, "number": 1.0, "boolean": True}.get(schema_type)

    @classmethod
    def _generate_stream_record(cls, stream_name, date_value="2024-01-01T00:00:00Z"):
        """Generate one schema-valid mock record for the given stream."""
        return cls._generate_value(cls._load_schema(stream_name),
                                   date_value=date_value)
```

**When to use inline mock data instead of `_generate_stream_record()`:**
If a stream has complex business logic that relies on specific field values
(e.g. date filtering, type coercion, sentinel values like `end_date=None`),
add inline `MOCK_<STREAM>` class attributes in `base.py` with realistic values
and use them directly in the test, similar to `TaboolaBaseTest.MOCK_CAMPAIGNS`.

Also add a helper `_mock_request()` factory method when the tap uses a single
internal request function for all streams:

```python
@classmethod
def _mock_request(cls, stream_data=None):
    """Create a side_effect function for patching tap_<module>.request."""
    if stream_data is None:
        stream_data = cls.MOCK_<STREAM>  # <-- replace with actual mock data

    def mock_fn(url, *args, **kwargs):
        # Route the mocked URL to the right fixture based on URL contents
        if "<stream_endpoint>" in url:  # <-- replace per stream
            return MockResponse({"<envelope_key>": stream_data})
        return MockResponse({})

    return mock_fn
```

And add `MockResponse` near the top of `base.py`:

```python
class MockResponse:
    """Minimal requests.Response stand-in."""
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass
```

---

### 13b — Mock-mode `test_discovery.py`

Call the tap’s own `discover` / `do_discover` function directly. Mock only
the `Context` or config object — no HTTP call needed for discovery.

```python
"""Integration tests for <TAP_NAME> stream discovery with mocked data."""
import unittest
from unittest.mock import MagicMock
from singer import metadata

try:
    from .base import <TapPascalCase>BaseTest
except ImportError:
    from base import <TapPascalCase>BaseTest

import tap_<TAP_MODULE>
# TODO: import the specific discover/do_discover function and any Context class
# e.g. from tap_<TAP_MODULE> import discover
# e.g. from tap_<TAP_MODULE>.context import Context


class <TapPascalCase>DiscoveryTest(<TapPascalCase>BaseTest, unittest.TestCase):

    def _get_catalog(self):
        """Run discover with a mocked context/config."""
        # Option A — tap has a Context object:
        # ctx = MagicMock(spec=Context)
        # ctx.config = self.get_mock_config()
        # return discover(ctx)
        #
        # Option B — tap’s do_discover() reads config from a module-level variable:
        # tap_<TAP_MODULE>.CONFIG = self.get_mock_config()
        # return tap_<TAP_MODULE>.do_discover()
        pass  # TODO: implement for this tap

    def test_discovery_returns_all_expected_streams(self):
        """Verify discover returns catalog entries for all expected streams."""
        catalog = self._get_catalog()
        expected = self.expected_metadata()
        # TODO: adjust attribute access to match the catalog object type
        discovered_ids = {s.tap_stream_id for s in catalog.streams}
        self.assertEqual(discovered_ids, set(expected.keys()))

    def test_discovery_primary_keys(self):
        """Verify primary keys match expected for every stream."""
        catalog = self._get_catalog()
        expected = self.expected_metadata()
        for stream in catalog.streams:
            with self.subTest(stream=stream.tap_stream_id):
                self.assertEqual(
                    set(stream.key_properties),
                    expected[stream.tap_stream_id][self.PRIMARY_KEYS],
                )

    def test_discovery_schema_has_properties(self):
        """Verify every discovered stream has a schema with at least one property."""
        catalog = self._get_catalog()
        for stream in catalog.streams:
            with self.subTest(stream=stream.tap_stream_id):
                schema_dict = stream.schema.to_dict()
                self.assertIn("properties", schema_dict)
                self.assertGreater(len(schema_dict["properties"]), 0)

    def test_discovery_replication_method(self):
        """Verify every stream’s forced-replication-method matches expectations."""
        catalog = self._get_catalog()
        expected = self.expected_metadata()
        for stream in catalog.streams:
            with self.subTest(stream=stream.tap_stream_id):
                mdata = metadata.to_map(stream.metadata)
                actual = (metadata.get(mdata, (), "forced-replication-method")
                          or metadata.get(mdata, (), "replication-method"))
                self.assertEqual(
                    actual,
                    expected[stream.tap_stream_id][self.REPLICATION_METHOD],
                )
```

---

### 13c — Mock-mode `test_bookmark.py`

Patch the tap’s internal HTTP function and assert that bookmarks are set correctly:

```python
"""Integration tests for <TAP_NAME> bookmarking with mocked data."""
import unittest
from unittest.mock import patch, MagicMock

try:
    from .base import <TapPascalCase>BaseTest
except ImportError:
    from base import <TapPascalCase>BaseTest

import tap_<TAP_MODULE>


class <TapPascalCase>BookmarkTest(<TapPascalCase>BaseTest, unittest.TestCase):
    """Verify bookmark behaviour for INCREMENTAL streams."""

    # TODO: patch the correct internal request/HTTP function path
    @patch("tap_<TAP_MODULE>.<http_function>")
    def test_bookmark_is_set_after_sync(self, mock_request):
        """After syncing an INCREMENTAL stream, a bookmark must be written to state."""
        mock_request.side_effect = self._mock_request()
        state = {}
        # TODO: call the tap’s sync function for an INCREMENTAL stream
        # e.g. tap_<TAP_MODULE>.sync_<stream>(self.config, state)
        # Assert state was updated with a bookmark
        self.assertIn("bookmarks", state)

    @patch("tap_<TAP_MODULE>.<http_function>")
    def test_bookmark_filters_records_on_second_sync(self, mock_request):
        """Second sync using a bookmark should return only newer records."""
        # Set a bookmark that is between the two mock record dates
        mid_date = "2021-06-15T00:00:00Z"
        state = {"bookmarks": {"<stream_name>": {"<replication_key>": mid_date}}}

        mock_request.side_effect = self._mock_request()
        records_written = []

        with patch("tap_<TAP_MODULE>.singer.write_record",
                   side_effect=lambda s, r: records_written.append((s, r))):
            # TODO: call tap sync with state
            pass

        # Only records with replication_key > mid_date should appear
        for stream, record in records_written:
            if stream == "<stream_name>":
                self.assertGreater(record["<replication_key>"], mid_date)
```

---

### 13d — Mock-mode `test_pagination.py`

Supply a `side_effect` list to simulate multiple pages:

```python
"""Integration tests for <TAP_NAME> pagination with mocked data."""
import unittest
from unittest.mock import patch

try:
    from .base import <TapPascalCase>BaseTest
except ImportError:
    from base import <TapPascalCase>BaseTest

import tap_<TAP_MODULE>


class <TapPascalCase>PaginationTest(<TapPascalCase>BaseTest, unittest.TestCase):
    """Verify page-based pagination for streams that paginate."""

    @patch("tap_<TAP_MODULE>.<http_function>")
    def test_<stream>_fetches_multiple_pages(self, mock_request):
        """Tap should keep fetching pages until an empty response is received."""
        page1 = [self._generate_stream_record("<stream_name>") for _ in range(100)]
        page2 = [self._generate_stream_record("<stream_name>")]
        empty  = []  # signals end of pages

        mock_request.side_effect = [page1, page2, empty]

        records_written = []
        with patch("tap_<TAP_MODULE>.singer.write_record",
                   side_effect=lambda s, r: records_written.append(r)):
            # TODO: call the stream’s sync function
            pass

        # Should have fetched exactly two non-empty pages
        self.assertEqual(mock_request.call_count, 3)  # page1, page2, empty
        self.assertEqual(len(records_written), 101)

    @patch("tap_<TAP_MODULE>.<http_function>")
    def test_<stream>_single_page_stops_correctly(self, mock_request):
        """Single-page response: tap should stop after one empty page sentinel."""
        page1 = [self._generate_stream_record("<stream_name>")]
        mock_request.side_effect = [page1, []]  # one page then empty

        records_written = []
        with patch("tap_<TAP_MODULE>.singer.write_record",
                   side_effect=lambda s, r: records_written.append(r)):
            pass  # TODO: call sync

        self.assertEqual(len(records_written), 1)
```

---

### 13e — Mock-mode `test_interrupted_sync.py`

Verify that when a sync is interrupted mid-stream and restarted with the saved
state, it resumes from the correct bookmark without duplicating records:

```python
"""Integration tests for <TAP_NAME> interrupted sync resumption with mocked data."""
import unittest
from unittest.mock import patch

try:
    from .base import <TapPascalCase>BaseTest
except ImportError:
    from base import <TapPascalCase>BaseTest

import tap_<TAP_MODULE>


class <TapPascalCase>InterruptedSyncTest(<TapPascalCase>BaseTest, unittest.TestCase):
    """Verify that sync resumes correctly after an interruption."""

    @patch("tap_<TAP_MODULE>.<http_function>")
    def test_interrupted_sync_resumes_from_bookmark(self, mock_request):
        """
        Simulate an interrupted sync by setting a mid-point bookmark,
        then verify the resumed sync only emits records after that bookmark.
        """
        # State left by the interrupted first sync
        interrupted_state = {
            "bookmarks": {
                "<stream_name>": {"<replication_key>": "2021-06-15T00:00:00Z"}
            }
        }

        mock_request.side_effect = self._mock_request()
        records_after_resume = []
        with patch("tap_<TAP_MODULE>.singer.write_record",
                   side_effect=lambda s, r: records_after_resume.append((s, r))):
            # TODO: call sync with interrupted_state
            pass

        # Only records after the interrupted bookmark should be synced
        for stream, record in records_after_resume:
            if stream == "<stream_name>":
                self.assertGreater(
                    record["<replication_key>"],
                    "2021-06-15T00:00:00Z",
                    msg="Resumed sync should not replay already-synced records",
                )

    @patch("tap_<TAP_MODULE>.<http_function>")
    def test_full_table_stream_always_fully_replicated(self, mock_request):
        """
        FULL_TABLE streams must be fully replicated even when the sync was
        previously interrupted — they have no bookmark to resume from.
        """
        # Even with a stale state, FULL_TABLE streams ignore it
        stale_state = {"bookmarks": {}}
        mock_request.side_effect = self._mock_request()
        records_written = []
        with patch("tap_<TAP_MODULE>.singer.write_record",
                   side_effect=lambda s, r: records_written.append((s, r))):
            pass  # TODO: call sync with stale_state

        full_table_records = [
            r for s, r in records_written if s == "<full_table_stream>"
        ]
        self.assertEqual(
            len(full_table_records),
            len(self.MOCK_<STREAM>),  # or use _generate_stream_record count
            msg="FULL_TABLE stream must replicate all records regardless of state",
        )
```

---

### 13f — Mock-mode `test_all_fields.py`, `test_automatic_fields.py`, `test_start_date.py`

These follow the same `@patch` pattern. For each:

**`test_all_fields.py`** — select all fields, run sync with mock, verify every field
from the schema appears in the emitted records:
```python
@patch("tap_<TAP_MODULE>.<http_function>")
def test_all_schema_fields_replicated(self, mock_request):
    mock_request.side_effect = self._mock_request()
    stream_name = "<stream_name>"
    schema = self._load_schema(stream_name)
    expected_fields = set(schema["properties"].keys())
    records_written = []
    with patch("tap_<TAP_MODULE>.singer.write_record",
               side_effect=lambda s, r: records_written.append((s, r))):
        pass  # TODO: call sync
    actual_fields = set().union(*[set(r.keys()) for s, r in records_written
                                  if s == stream_name])
    self.assertTrue(expected_fields.issubset(actual_fields))
```

**`test_automatic_fields.py`** — same but verify only PKs + replication keys appear
when no fields are explicitly selected.

**`test_start_date.py`** — run sync twice with different `start_date` values; verify
the second (later) start date returns fewer or equal records:
```python
@patch("tap_<TAP_MODULE>.<http_function>")
def test_start_date_filters_older_records(self, mock_request):
    mock_request.side_effect = self._mock_request()
    records_early, records_late = [], []
    # Sync 1: early start_date
    config_1 = {**self.get_mock_config(), "start_date": "2020-01-01T00:00:00Z"}
    with patch("tap_<TAP_MODULE>.singer.write_record",
               side_effect=lambda s, r: records_early.append(r)):
        pass  # TODO: call sync with config_1
    # Sync 2: later start_date
    mock_request.side_effect = self._mock_request()
    config_2 = {**self.get_mock_config(), "start_date": "2021-07-01T00:00:00Z"}
    with patch("tap_<TAP_MODULE>.singer.write_record",
               side_effect=lambda s, r: records_late.append(r)):
        pass  # TODO: call sync with config_2
    self.assertLessEqual(len(records_late), len(records_early))
```

---

### 13g — How to find the correct patch target

Before writing any `@patch`, read the tap source to find the **exact dotted path**
of the HTTP function:

1. Open `tap_<module>/client.py` or `tap_<module>/streams.py`
2. Find the function that calls `requests.get()` / `requests.post()` or the
   tap’s own session/client object
3. The patch target is `"tap_<module>.<function_name>"` or
   `"tap_<module>.client.<method_name>"`

Examples from real taps:
- `@patch("tap_taboola.request")` — top-level `request()` function in `tap_taboola`
- `@patch("tap_listrak.streams.request")` — imported in the streams module
- `@patch("tap_lever.http.Client.get")` — method on a client class

Always patch at the **point of use** (where it is imported), not at the
original definition.

---

## Final Report

```
╔══════════════════════════════════════════════════════════════════════╗
║         Singer Tap — Integration Test Generation Report              ║
╚══════════════════════════════════════════════════════════════════════╝

Tap            : <TAP_NAME>
Connection type: <CONNECTION_TYPE>  (derived: platform.<name>)
Tests dir      : <TAP_DIR>/tests/
Branch         : <BRANCH_NAME>

────────────────────────────────────────────────────────┐
 STREAMS DISCOVERED
────────────────────────────────────────────────────────┘
 Stream                  | Rep. Method  | Rep. Keys        | Parent
 ────────────────────────────────────────────────────────
 <stream rows>

 Total streams     : <N>
 INCREMENTAL       : <N>
 FULL_TABLE        : <N>
 With parent       : <N>

────────────────────────────────────────────────────────┐
 FILES GENERATED / UPDATED  (testMode = live)
────────────────────────────────────────────────────────┘
 tests/base.py                  ✅ created / ✅ updated / ⏭️  unchanged
 tests/test_discovery.py        ✅ created / ...
 tests/test_bookmark.py         ✅ created / ⏭️  skipped (no INCREMENTAL streams)
 tests/test_start_date.py       ✅ created / ...
 tests/test_pagination.py       ✅ created / ...
 tests/test_all_fields.py       ✅ created / ...
 tests/test_automatic_fields.py ✅ created / ...

 FILES GENERATED / UPDATED  (testMode = mock)
────────────────────────────────────────────────────────┘
 tests/base.py                  ✅ rewritten as plain mixin (no tap-tester)
 tests/test_discovery.py        ✅ calls tap's discover() directly
 tests/test_bookmark.py         ✅ uses @patch on tap's HTTP function
 tests/test_pagination.py       ✅ side_effect page list
 tests/test_all_fields.py       ✅ schema-driven field assertions
 tests/test_automatic_fields.py ✅ auto-field assertions
 tests/test_start_date.py       ✅ two-config sync comparison
 tests/test_interrupted_sync.py ✅ bookmark resume assertions

────────────────────────────────────────────────────────┐
 NEXT STEPS
────────────────────────────────────────────────────────┘
 1. Set the required credential env vars (derived from tap source config keys):
    TAP_<NAME>_<KEY>   e.g. TAP_DRIP_API_TOKEN

 2. Install tap-tester (if not already):
    pip install tap-tester

 3. Run a single test to verify the scaffold works:
    cd <TAP_DIR>/tests
    python -m pytest test_discovery.py -v --tb=short

 4. Fill in any TODO comments left in the test files.

────────────────────────────────────────────────────────
 OVERALL: ✅ INTEGRATION TESTS SCAFFOLDED
          Changes left unstaged — use the commit/release prompts to proceed.
────────────────────────────────────────────────────────
```

---

## Important Rules

- **Never hardcode credentials** — always use `os.getenv()`. Env var names are derived automatically: `TAP_` + tap name (upper, hyphens→underscores) + `_` + config key (upper). E.g. `tap-drip` + `api_token` → `TAP_DRIP_API_TOKEN`.
- **Prefer mix-in test classes** (`DiscoveryTest`, `BookmarkTest`, etc.) over writing full manual test implementations — they cover the standard assertions automatically.
- **Do not overwrite existing test files** without first reading them — merge new stream entries into existing `expected_metadata()` dicts instead.
- **Skip bookmark and start_date tests** for FULL_TABLE-only taps; emit the skipped stub so the test runner doesn't error.
- **Set `API_LIMIT`** to the actual page size found in the tap source; use `100` as a safe default if it cannot be determined.
- **`OBEYS_START_DATE`** should be `True` only when you can verify from source code that the stream filters records by `start_date`; default to `False`.
- Always add a `setUp` method with env var validation so test failures are immediately actionable.
- **Mock `base.py` uses dummy credential values** — `get_mock_config()` must never call `os.getenv()`; use string literals like `"mock_test_token"`.
- **Prefer `_generate_stream_record()` over hand-written mock data** — it reads the tap’s own schema JSON files ensuring mocks stay in sync when schemas change. Only write inline mock data (`MOCK_<STREAM>`) when the test must assert specific field values (type coercions, sentinel handling, date filtering).
- **`@patch` at the point of use** — patch `"tap_module.request"` not `"requests.get"`. Read the tap source to find the exact dotted path (see Step 13g).
- **Mock tests are plain `unittest`** — no `pip install tap-tester`, no Singer/Stitch infrastructure required. Run with `python -m pytest tests/ -v`.
- **`test_interrupted_sync.py` is mock-only** — it verifies bookmark resumption logic that is impossible to test reliably against a live API.