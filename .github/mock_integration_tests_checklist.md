## 12. Mock Integration Test Verification Checklist

Use this checklist whenever generating or reviewing mock integration tests for taps that lack live test credentials. Tests must validate **actual tap behavior**, not merely achieve a passing status.

### Purpose

Mock integration tests replace live sandbox/credential-dependent integration tests. They must simulate the full sync pipeline using `unittest.mock` and realistic fixture data, verifying the same correctness properties as real integration tests.

---

### 12.1 Mock Data Quality

- [ ] Mock API responses reflect the **actual API response structure** (correct root keys, pagination envelope, field names).
- [ ] Fixture records contain **all schema-defined fields** where possible; use `null` for optional fields rather than omitting them.
- [ ] Date/time fields in fixtures use **RFC 3339 format** (e.g., `"2024-01-15T12:00:00Z"`), never bare dates or epoch ints.
- [ ] Fixture data contains **at least two records per stream** to expose ordering, deduplication, and bookmark edge cases.
- [ ] For **incremental streams**, fixtures include records both before and after the bookmark boundary to verify filtering.
- [ ] For **paginated streams**, fixtures span at least two pages to verify pagination logic end-to-end.

Bad example (trivial / structurally wrong fixture):
```python
mock_response = {"items": [{"id": 1}]}   # Wrong root key; single record hides pagination bugs
```

Good example (realistic fixture):
```python
mock_response_page1 = {
    "segments": [
        {"id": 1, "name": "VIP", "updated_at": "2024-02-01T00:00:00Z"},
        {"id": 2, "name": "Trial", "updated_at": "2024-02-05T00:00:00Z"},
    ],
    "next": "cursor-page2",
}
mock_response_page2 = {
    "segments": [
        {"id": 3, "name": "Churn", "updated_at": "2024-02-10T00:00:00Z"},
    ],
}
```

---

### 12.2 Discovery Test

Every mock discovery test **must** assert all of the following:

- [ ] The **count** of discovered streams exactly equals the count of expected streams (not just a subset check).
- [ ] The **exact set** of stream names discovered equals the expected set — no extra, no missing.
- [ ] Every stream name matches the naming convention: **lowercase alphanumerics and underscores only** (`[a-z_]+`).
- [ ] Each stream has **exactly one** top-level breadcrumb (`breadcrumb == []`) in its metadata.
- [ ] There are **no duplicate metadata entries** for any field within a stream.
- [ ] `metadata` for each stream declares the correct **primary key(s)** (`table-key-properties`).
- [ ] `metadata` declares the correct **replication method** (`forced-replication-method`).
- [ ] `metadata` declares the correct **replication key(s)** for incremental streams (`valid-replication-keys`).
- [ ] Schema properties include all fields defined in the corresponding `schemas/*.json` file.

```python
# Stream count and name set equality (from tap-tester test_number_of_streams + test_streams_discovered)
self.assertEqual(len(catalog.streams), len(EXPECTED_STREAMS))
self.assertEqual({s.tap_stream_id for s in catalog.streams}, EXPECTED_STREAMS)

# Naming convention (from tap-tester test_stream_naming)
import re
for stream_name in {s.tap_stream_id for s in catalog.streams}:
    self.assertRegex(stream_name, r"^[a-z_]+$")

# Single top-level breadcrumb + no duplicates (from tap-tester test_one_top_level_breadcrumb + test_no_duplicate_metadata)
top_level = [m for m in stream_entry.metadata if m["breadcrumb"] == []]
self.assertEqual(len(top_level), 1)
all_fields = [m["breadcrumb"][1] for m in stream_entry.metadata if m["breadcrumb"] != []]
self.assertEqual(len(all_fields), len(set(all_fields)))

# Primary key, replication method and keys (from tap-tester test_primary_keys + test_replication_metadata)
stream_meta = {tuple(m["breadcrumb"]): m["metadata"] for m in stream_entry.metadata}
self.assertEqual(set(stream_meta[()]["table-key-properties"]), {"id"})
self.assertEqual(stream_meta[()]["forced-replication-method"], "INCREMENTAL")
self.assertIn("updated_at", stream_meta[()]["valid-replication-keys"])
```

---

### 12.3 All Fields Test

- [ ] **No unexpected streams** are replicated — the set of synced stream names equals the expected set exactly (from tap-tester `test_no_unexpected_streams_replicated`).
- [ ] Every stream under test emits **at least one record** (from tap-tester `test_all_streams_sync_records`).
- [ ] The set of fields replicated **exactly equals** `selected_fields - MISSING_FIELDS - KEYS_WITH_NO_DATA + EXTRA_FIELDS` — use set equality, not a subset check (from tap-tester `test_all_fields_for_streams_are_replicated`).
- [ ] Fields with `"format": "date-time"` contain a valid RFC 3339 string, not `null` or a bare date.
- [ ] Document any fields in `MISSING_FIELDS` that the API never returns, so the assertion does not silently pass.

```python
# Exact stream set (from tap-tester test_no_unexpected_streams_replicated)
self.assertSetEqual(set(synced_records.keys()), TEST_STREAMS)

# At least one record per stream (from tap-tester test_all_streams_sync_records)
self.assertGreater(record_count_by_stream.get(stream, 0), 0)

# Exact field equality (from tap-tester test_all_fields_for_streams_are_replicated)
expected_keys = selected_fields.get(stream, set()) - MISSING_FIELDS.get(stream, set())
fields_replicated = actual_fields.get(stream, set())
self.assertSetEqual(fields_replicated, expected_keys)
```

---

### 12.4 Automatic Fields Test (Minimum Selection)

- [ ] When **no** optional fields are selected (only automatic fields enabled), the tap still emits records.
- [ ] The fields replicated are **exactly** the automatic fields — no more, no less (from tap-tester `test_only_automatic_fields_replicated`, which uses set equality not superset).
- [ ] Every emitted record has a **unique primary key** — assert `len(pk_set) == len(pk_list)` (from tap-tester `test_records_primary_key_is_unique`).
- [ ] No `KeyError` or `None` is raised when accessing primary/replication key fields on the record.

```python
# Only automatic fields replicated — set equality, not superset (from tap-tester test_only_automatic_fields_replicated)
expected_automatic_fields = primary_keys | replication_keys  # inclusion=automatic fields only
self.assertSetEqual(fields_replicated, expected_automatic_fields)

# Unique primary keys across all records (from tap-tester test_records_primary_key_is_unique)
pk_tuples = [tuple(msg["data"][pk] for pk in primary_keys) for msg in messages if msg["action"] == "upsert"]
self.assertCountEqual(set(pk_tuples), pk_tuples)
```

---

### 12.5 Bookmark / State Test

- [ ] After a first sync, `state["bookmarks"][stream_id]` exists and contains the replication key.
- [ ] The bookmark value equals the **maximum replication key value** seen across all synced records (from tap-tester `test_first_sync_bookmark` and `test_second_sync_bookmark`).
- [ ] The bookmark is a **parseable string** in the expected `bookmark_format` — assert `datetime.strptime(bookmark, bookmark_format)` succeeds (from tap-tester `test_bookmark_format`).
- [ ] **FULL_TABLE streams must NOT have a bookmark** in state after sync — assert `bookmark_value is None` (from tap-tester `test_bookmark_format`).
- [ ] `state["currently_syncing"]` is `None` after both sync 1 and sync 2 complete successfully (from tap-tester `test_syncs_were_successful`).
- [ ] **Only streams under test** have bookmark entries in state — no unexpected stream keys pollute state (from tap-tester `test_syncs_were_successful`).
- [ ] Bookmark from sync 2 is **greater than or equal to** bookmark from sync 1 (from tap-tester `test_sync_2_bookmark_greater_or_equal_to_sync_1`).
- [ ] A second sync seeded with the first sync's bookmark returns **fewer or equal** records (from tap-tester `test_first_vs_second_records`).
- [ ] Records in the second sync have a replication key value **≥ the seeded bookmark minus the lookback window**, capped at `start_date` (from tap-tester `test_second_sync_records_respect_bookmark`).
- [ ] The bookmark state structure conforms to the expected format:
  ```json
  {"bookmarks": {"stream_name": {"replication_key": "2024-01-01T00:00:00Z"}}}
  ```

```python
# After sync 1 — bookmark equals max replication key (from tap-tester test_first_sync_bookmark)
bookmark = state["bookmarks"]["segments"]["updated_at"]
self.assertEqual(bookmark, max(r["updated_at"] for r in synced_records_1))

# Bookmark is parseable in expected format (from tap-tester test_bookmark_format)
self.assertIsInstance(datetime.strptime(bookmark, "%Y-%m-%dT%H:%M:%S.%fZ"), datetime)

# FULL_TABLE streams have no bookmark (from tap-tester test_bookmark_format)
self.assertIsNone(state["bookmarks"].get("activities"))

# No unexpected stream bookmarks in state (from tap-tester test_syncs_were_successful)
self.assertSetEqual(set(state["bookmarks"].keys()), EXPECTED_INCREMENTAL_STREAMS)

# Sync 2 bookmark >= sync 1 bookmark (from tap-tester test_sync_2_bookmark_greater_or_equal_to_sync_1)
self.assertGreaterEqual(bookmark_2, bookmark_1)

# Sync 2 record count <= sync 1 record count
self.assertLessEqual(len(synced_records_2), len(synced_records_1))
```

---

### 12.6 Start Date Test

- [ ] For streams where `RESPECTS_START_DATE = True`, syncing with an **earlier** `start_date` returns **more or equal** records than syncing with a **later** `start_date`.
- [ ] For streams where `RESPECTS_START_DATE = False`, record counts are **equal** between both syncs — not just independent.
- [ ] Both syncs return **at least one record** for each stream under test (from tap-tester `test_both_syncs_got_data`).
- [ ] The **minimum replication key value** in each sync is >= the respective `start_date` — verifies the API honors the date filter.
- [ ] All records returned in the later-start-date sync are also present in the earlier-start-date sync (subset check via primary keys).
- [ ] The `start_date` config value is forwarded to the API request parameters (assert on mock call args).

```python
# RESPECTS_START_DATE = True (from tap-tester test_replicated_records)
self.assertGreaterEqual(len(records_early_start), len(records_late_start))
pks_late  = {r["id"] for r in records_late_start}
pks_early = {r["id"] for r in records_early_start}
self.assertTrue(pks_late.issubset(pks_early))

# RESPECTS_START_DATE = False — counts must be equal
self.assertEqual(len(records_early_start), len(records_late_start))

# Minimum replication value >= start_date (both syncs)
min_rep_value = min(self.parse_date(r[replication_key]) for r in records)
self.assertGreaterEqual(min_rep_value, self.parse_date(start_date))
```

---

### 12.7 Pagination Test

- [ ] The tap issues a **second API request** when the first response includes a pagination cursor/token.
- [ ] The cursor/token from page N is passed correctly as the `start`/`cursor`/`page` parameter on page N+1.
- [ ] Records from **all pages** are emitted (total record count equals sum across all mocked pages).
- [ ] The tap stops paginating when the response contains no next cursor (no extra API call is made).
- [ ] An **empty page** (records list is `[]`) with a live cursor does not cause an infinite loop.
- [ ] Total record count is **strictly greater than the API page limit** (`expected_page_size`) — this is what `test_record_count_greater_than_page_limit` verifies (from tap-tester).
- [ ] The count of **unique primary key tuples equals the total record count** — no duplicates across pages (from tap-tester `test_no_duplicate_records`).

```python
# Two-page scenario
mock_client.make_request.side_effect = [page1_response, page2_response]
records = list(stream.get_records())
self.assertEqual(len(records), len(page1_records) + len(page2_records))
self.assertEqual(mock_client.make_request.call_count, 2)

# Verify cursor forwarding
_, call_kwargs = mock_client.make_request.call_args_list[1]
self.assertEqual(call_kwargs.get("params", {}).get("start"), page1_response["next"])

# No duplicate records across pages (from tap-tester test_no_duplicate_records)
pk_set = {r["id"] for r in records}
self.assertEqual(len(pk_set), len(records))
```

---

### 12.8 Interrupted Sync Test

- [ ] When `state["currently_syncing"]` is set to a stream mid-way through the catalog, the tap **resumes** at that stream.
- [ ] Streams that already appear in `state["bookmarks"]` (before `currently_syncing`) are **skipped** on resume.
- [ ] The stream listed as `currently_syncing` is re-synced from its bookmark, not from scratch.
- [ ] After a full interrupted-then-resumed sync, `currently_syncing` is **cleared** from the final state.
- [ ] Assert the **order** of streams synced matches the expected catalog order starting from the interrupted stream.
- [ ] Every stream in the resuming sync emits **at least one record** (from tap-tester `test_all_streams_sync_records`).
- [ ] The resuming sync's final state **exactly equals** the first (uninterrupted) sync's final state — proves resume is consistent (from tap-tester `test_syncs_were_successful`).
- [ ] Records in the resuming sync for the interrupted stream have a replication key value **≥ bookmark minus lookback window** (from tap-tester `test_bookmarked_streams_start_date`).

```python
interrupted_state = {
    "currently_syncing": "segments",
    "bookmarks": {
        "segments":               {"updated_at": "2024-01-01T00:00:00Z"},
        "transactional_messages": {"updated_at": "2024-01-01T00:00:00Z"},
    },
}
# transactional_messages was already synced; assert it is NOT re-synced
self.assertEqual(mock_tm_sync.call_count, 0)
# segments IS the interrupted stream; assert it IS synced
self.assertEqual(mock_segments_sync.call_count, 1)

# resuming state must match what a clean sync would have produced (from tap-tester test_syncs_were_successful)
self.assertDictEqual(resuming_sync_state, first_sync_state)

# currently_syncing must be cleared after resume (from tap-tester test_syncs_were_successful)
self.assertIsNone(resuming_sync_state.get("currently_syncing"))
```

---

### 12.9 Error Handling & Retry Test

- [ ] HTTP **4xx** errors (400, 401, 403, 404, 409, 422) raise the correct custom exception **without** retrying.
- [ ] HTTP **429** and **5xx** errors (500–503) are **retried** up to the configured maximum (default: 5 attempts).
- [ ] After exhausting retries, the correct exception is raised and propagated.
- [ ] `time.sleep` (or the backoff delay) is called between retries — assert call count equals `retries - 1`.
- [ ] The error message includes the HTTP status code and a descriptive message.

```python
with patch.object(client._session, "request", return_value=MockResponse(429)):
    with patch("time.sleep") as mock_sleep:
        with self.assertRaises(customerioRateLimitError):
            client._Client__make_request("GET", url)
        self.assertEqual(mock_session_request.call_count, 5)   # 5 attempts
        self.assertGreater(mock_sleep.call_count, 0)           # back-off applied
```

---

### 12.10 General Mock Test Anti-Patterns to Avoid

| Anti-pattern | Why it is wrong | Correct approach |
|---|---|---|
| Asserting only that a mock **was called** | Doesn't verify correctness of arguments or output | Assert call args **and** the return value or side effect |
| Using `MagicMock()` return values that auto-create attributes | Hides `AttributeError` bugs at runtime | Use `spec=ClassName` or explicit `return_value` dicts |
| Single-record fixtures for pagination tests | Pagination loop may never execute a second iteration | Always provide at least two pages of fixture data |
| Hardcoding `assert True` or trivially true assertions | Test passes regardless of implementation | Write assertions that would **fail** if the implementation is broken |
| Mocking at too high a level (e.g., patching `sync` entirely) | Tests nothing about the stream's internal logic | Mock only the HTTP layer (`client.make_request`) and assert on stream output |
| Not resetting mocks between test methods | Shared state causes order-dependent failures | Use `setUp` / `tearDown` or `patch` as context managers |

---

### 12.11 File Placement & Naming

- [ ] Mock integration tests live under `tests/unittests/` and follow the naming convention `test_mock_<test_category>.py` (e.g., `test_mock_discovery.py`, `test_mock_bookmark.py`).
- [ ] Each file imports only from `unittest`, `unittest.mock`, and the tap's own modules — no live network calls.
- [ ] A `MockResponse` helper class (with `status_code`, `json()`, `raise_for_status()`) is defined once in a shared `tests/unittests/helper.py` and re-used across test files.
- [ ] Fixture data is defined as module-level constants or in a `fixtures/` subdirectory, not inline inside test methods.
