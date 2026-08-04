# Singer Tap PR Review Guide

## 🚀 Quick Start

**Just paste the PR link**:

```
Review this PR: https://github.com/singer-io/tap-{name}/pull/{number}

Verify exception definitions are correct as per API documentation
Identify breaking changes (config keys, primary keys, replication keys, state format, stream renames, data types)
Validate schemas using /opt/code/singer-tap-generator/utils/validate_singer_schemas.py
Use /tmp/config.json to debug the code
```

---

## 📋 Complete Review Checklist

### 1. ⚠️ Exception Definitions Verification (CRITICAL)

**Objective**: Verify exception definitions match API documentation

**Steps**:
1. Read `tap_{name}/exceptions.py`
2. Fetch official API error documentation
3. Cross-reference `ERROR_CODE_EXCEPTION_MAPPING` with documented status codes
4. Verify exception hierarchy (base → backoff base → specific errors)
5. Validate backoff configuration matches API rate limit guidance

**Commands**:
```bash
# Find exception definitions
grep -n "ERROR_CODE_EXCEPTION_MAPPING" tap_{name}/exceptions.py

# View exception hierarchy
grep -n "class.*Error" tap_{name}/exceptions.py

# Check backoff configuration in client
grep -A10 "@backoff.on_exception" tap_{name}/client.py
```

**Validation Checklist**:
- [ ] All documented API error codes are mapped (400, 401, 403, 404, 422, 429, 5xx)
- [ ] 4xx errors (except 429) → inherit from base Error (no retry)
- [ ] 429, 5xx → inherit from BackoffError (exponential retry)
- [ ] Connection errors included (ConnectionResetError, Timeout, ChunkedEncodingError)
- [ ] `max_tries`, `factor`, `wait_gen` configured appropriately
- [ ] Error message parsing handles API-specific response formats

**Common API Error Response Formats**:
- **SparkPost**: `{"errors": [{"message": "...", "code": "...", "description": "..."}]}`
- **SendGrid**: `{"errors": [{"message": "...", "field": "..."}]}`
- **Customer.io**: `{"meta": {"error": "..."}}`
- **SAP SuccessFactors**: `{"error": {"message": {...}}}`

**API Documentation URLs**:
- SparkPost: https://developers.sparkpost.com/api/
- SendGrid: https://www.twilio.com/docs/sendgrid/api-reference/mail-send/mail-send
- Customer.io: https://customer.io/docs/api/
- SAP SuccessFactors: https://help.sap.com/docs/
- Taboola: https://developers.taboola.com/
- Branch: https://help.branch.io/developers-hub/reference
- Monday: https://developer.monday.com/api-reference/
- Listrak: https://api.listrak.com/

---

### 2. 🔴 Breaking Changes Identification (CRITICAL)

**Objective**: Identify all changes that break backward compatibility

#### 2.1 Config Properties Changes

**Commands**:
```bash
# Compare REQUIRED_CONFIG_KEYS
git show master:tap_{name}/__init__.py | grep -A5 REQUIRED_CONFIG_KEYS
git show {branch}:tap_{name}/__init__.py | grep -A5 REQUIRED_CONFIG_KEYS
```

**Check for**:
- [ ] **Added** required keys (existing configs will fail validation)
- [ ] **Removed** keys (existing configs become invalid)
- [ ] **Renamed** keys (existing configs won't map correctly)
- [ ] Changed default values for optional parameters
- [ ] New optional parameters (document defaults)

**Examples of Breaking Changes**:
- ❌ `['assertion', 'company_id']` → `['username', 'password', 'company_id']`
- ❌ `['access_token']` → `['api_key']`
- ✅ `['api_key']` → `['api_key', 'request_timeout']` (if request_timeout has default)

#### 2.2 Primary Key Changes (key_properties)

**Commands**:
```bash
# Find all key_properties definitions
grep -r "key_properties" tap_{name}/streams/ --include="*.py"

# Compare specific stream with master
git show master:tap_{name}/streams/{stream}.py | grep key_properties
git show {branch}:tap_{name}/streams/{stream}.py | grep key_properties
```

**Check for**:
- [ ] Simple key → Composite key (e.g., `["email"]` → `["group_id", "email"]`)
- [ ] Key field renamed (e.g., `["id"]` → `["user_id"]`)
- [ ] Key fields reordered (impacts some targets)
- [ ] Data type changes in key fields (integer → string)

**Impact**: ❌ Breaks incremental syncs, de-duplication, upsert logic in targets

#### 2.3 Replication Key Changes (replication_keys)

**Commands**:
```bash
# Find all replication_keys definitions
grep -r "replication_keys" tap_{name}/streams/ --include="*.py"

# Compare with master
git diff master {branch} -- tap_{name}/streams/
```

**Check for**:
- [ ] Field name changes (e.g., `end_time` → `created`)
- [ ] Data type changes (Unix timestamp → ISO string, or vice versa)
- [ ] Format changes (e.g., `2024-01-01 00:00:00` → `2024-01-01T00:00:00Z`)
- [ ] Replication method changes (INCREMENTAL ↔ FULL_TABLE)

**Examples of Breaking Changes**:
- ❌ `["end_time"]` → `["created"]` (bookmark key name changed)
- ❌ Unix timestamp (1234567890) → ISO string ("2024-01-01T00:00:00Z")
- ❌ INCREMENTAL → FULL_TABLE (loses bookmark ability)

**Impact**: ❌ Existing state files become incompatible, syncs restart from start_date

#### 2.4 State File Structure Changes

**Check for**:
- [ ] Bookmark key naming conventions changed
- [ ] Bookmark value format changed (timestamp format, cursor structure)
- [ ] Parent stream bookmark handling changed
- [ ] New state fields required

**Compare state structure**:
```bash
# Check bookmark handling code
grep -A10 "get_bookmark\|write_bookmark" tap_{name}/streams/abstracts.py
```

#### 2.5 Stream Changes

**Commands**:
```bash
# List all streams in master
git show master:tap_{name}/streams/__init__.py | grep "from tap_{name}.streams"

# List all streams in PR branch
grep "from tap_{name}.streams" tap_{name}/streams/__init__.py

# Compare stream definitions
git diff master {branch} -- tap_{name}/streams/__init__.py
```

**Check for**:
- [ ] **Removed streams** (data loss for users syncing those streams)
- [ ] **Renamed streams** (breaks catalog references, view names in warehouses)
- [ ] Stream consolidation (multiple streams → single stream)
- [ ] Stream splitting (single stream → multiple streams)

**Examples**:
- ❌ `invalids` → `invalid_emails` (renamed)
- ❌ `groups_all` removed (deprecated API)
- ✅ New stream added (backward compatible)

#### 2.6 Schema Field Changes

**Commands**:
```bash
# Count schema files
find tap_{name}/schemas -name "*.json" -type f | wc -l

# Compare specific schema
git show master:tap_{name}/schemas/{stream}.json > /tmp/old_schema.json
diff /tmp/old_schema.json tap_{name}/schemas/{stream}.json
```

**Check for**:
- [ ] Removed fields (breaks field mappings in targets)
- [ ] Data type changes (e.g., `"type": "integer"` → `"type": "string"`)
- [ ] Format changes (e.g., removed `"format": "date-time"`)
- [ ] Nested structure changes (flattened or expanded objects)
- [ ] `additionalProperties` changes (false → true or vice versa)

---

### 3. 📋 Schema Validation (REQUIRED)

**Command**:
```bash
cd /opt/code/tap-{name}
python /opt/code/singer-tap-generator/utils/validate_singer_schemas.py tap_{name}/schemas/
```

**Success Criteria**:
- ✅ **0 errors** (BLOCKING - must fix before approval)
- ⚠️ Warnings about non-nullable primary keys are EXPECTED and OK
- ⚠️ Other warnings should be evaluated case-by-case

**Critical Validations**:
- [ ] All timestamp/date fields have `"format": "date-time"`
- [ ] Fields ending in: `_time`, `_date`, `_at`, `created`, `updated`, `timestamp`
- [ ] Root level has `"additionalProperties": false` (strict schemas)
- [ ] Nested objects can have `"additionalProperties": true` if needed
- [ ] Primary key fields do NOT allow null (should NOT be in type array)
- [ ] Required fields properly marked in JSON schema

**Common Schema Issues**:
```json
// ❌ BAD - missing date-time format
"created_at": {
  "type": ["null", "string"]
}

// ✅ GOOD
"created_at": {
  "type": ["null", "string"],
  "format": "date-time"
}

// ❌ BAD - root allows additionalProperties
{
  "type": "object",
  "properties": {...},
  "additionalProperties": true  // ❌ Root level
}

// ✅ GOOD
{
  "type": "object",
  "properties": {...},
  "additionalProperties": false  // ✅ Strict at root
}
```

---

### 4. 🔁 Loop Safety Check (CRITICAL)

**Command**:
```bash
grep -rn "while True" tap_{name}/ --include="*.py"
```

**Success Criteria**:
- ✅ **ZERO instances of `while True` loops**
- All pagination loops MUST have explicit exit conditions

**Check for**:
- [ ] No `while True:` without immediate break condition
- [ ] Pagination uses: `has_more_pages`, `max_pages`, or empty result checks
- [ ] Safety limits in place (e.g., `max_pages = 10000`)
- [ ] All loops eventually terminate (no infinite loops possible)

**Good Patterns**:
```python
# ✅ GOOD - explicit condition
max_pages = 10000
current_page = 0
has_more = True

while has_more and current_page < max_pages:
    response = fetch_page()
    if not response or not response.get('data'):
        has_more = False
        break
    process(response)
    current_page += 1

# ✅ GOOD - cursor-based with safety
cursor = "initial"
max_iterations = 5000
iterations = 0

while cursor and iterations < max_iterations:
    response = fetch(cursor)
    process(response)
    cursor = response.get('next_cursor')
    iterations += 1
```

**Bad Patterns**:
```python
# ❌ BAD - while True without safety
while True:
    response = fetch_page()
    if not response:
        break  # What if response is always truthy?
    process(response)

# ❌ BAD - no iteration limit
cursor = "initial"
while cursor:  # What if cursor is never None?
    response = fetch(cursor)
    cursor = response.get('next_cursor')
```

---

### 5. 📊 Record Completeness Check (CRITICAL)

**Command**:
```bash
# Find continue statements in exception handlers
grep -A5 "except.*:" tap_{name}/streams/*.py | grep -B3 "continue"

# Check for try/except in get_records
grep -A10 "def get_records" tap_{name}/streams/*.py | grep -A5 "except"
```

**Success Criteria**:
- ✅ NO silent record skipping
- All exceptions must be logged with context

**Check for**:
- [ ] No `continue` in exception handlers without logging
- [ ] All record processing errors logged (LOGGER.error or LOGGER.warning)
- [ ] Failed records either re-raise or log with full context (record ID, stream name, error)
- [ ] Bookmark filtering is explicit and documented

**Good Patterns**:
```python
# ✅ GOOD - log and raise
for record in get_records():
    try:
        transformed = transformer.transform(record, schema, metadata)
        write_record(stream_name, transformed)
        counter.increment()
    except Exception as e:
        LOGGER.error(f"Failed to transform record {record.get('id')}: {e}")
        raise  # Re-raise to fail the sync

# ✅ GOOD - log and skip with reason
for record in get_records():
    try:
        transformed = transformer.transform(record, schema, metadata)
    except ValidationError as e:
        LOGGER.warning(f"Skipping invalid record {record.get('id')}: {e}")
        continue  # OK - logged with reason
    write_record(stream_name, transformed)

# ✅ GOOD - bookmark filtering (not an error)
for record in get_records():
    record_date = record.get('created_at')
    if record_date < bookmark_date:
        continue  # OK - explicit filtering logic
    write_record(stream_name, record)
```

**Bad Patterns**:
```python
# ❌ BAD - silent skipping
for record in get_records():
    try:
        write_record(transform(record))
    except:
        continue  # ❌ Silent skip - forbidden

# ❌ BAD - broad exception without logging
for record in get_records():
    try:
        write_record(transform(record))
    except Exception:
        pass  # ❌ Silent failure
```

---

### 6. 🧪 Test Coverage Assessment (REQUIRED)

**Integration Tests** (tap-tester framework):

```bash
# Check base test file exists
ls -lh tests/base.py

# Count integration test files
ls -1 tests/test_*.py | wc -l

# View expected streams metadata
grep -A5 "def expected_metadata" tests/base.py
```

**Required Integration Tests**:
- [ ] `tests/base.py` - Base class with `expected_metadata()` for all streams
- [ ] `tests/test_discovery.py` - Discovery mode validation
- [ ] `tests/test_bookmark.py` - Bookmark persistence and resumability
- [ ] `tests/test_all_fields.py` - All fields are discovered and synced
- [ ] `tests/test_start_date.py` - Start date filtering works correctly
- [ ] `tests/test_pagination.py` - Pagination logic handles all pages
- [ ] `tests/test_interrupted_sync.py` - Resume after interruption
- [ ] `tests/test_automatic_fields.py` - Primary/replication keys marked automatic

**Unit Tests**:

```bash
# List unit test files
ls -1 tests/unittests/*.py

# Count total test lines
wc -l tests/base.py tests/unittests/*.py
```

**Required Unit Tests**:
- [ ] `test_client.py` - HTTP client, authentication, error handling
  - Test ALL exception types (400/401/403/404/422/429/500/502/503)
  - Verify 4xx errors don't retry (call_count=1)
  - Verify 5xx/429 errors retry with backoff (call_count=max_tries)
  - Test connection errors with retry
  - Test successful retry after failures
- [ ] `test_discovery_flow.py` - Catalog generation
- [ ] `test_{sync_type}_sync.py` - Full table and incremental sync logic
- [ ] `test_pagination_flow.py` - Pagination edge cases
- [ ] Stream-specific tests as needed

**Coverage Assessment**:
- ✅ **Comprehensive**: 1000+ test lines, all streams covered, all error codes tested
- ⚠️ **Adequate**: 500+ test lines, major streams covered, critical errors tested
- ❌ **Insufficient**: <500 test lines, missing critical error tests

---

### 7. 🔢 Version Numbering (CRITICAL)

**Command**:
```bash
# Check version in setup.py
grep "version=" setup.py
```

**Semantic Versioning Rules**:

| Scenario | Old | New | Required |
|----------|-----|-----|----------|
| Initial release | N/A | 0.0.1 or 0.1.0 | - |
| Bug fixes, new features (no breaking changes) | 0.x.y | 0.x+1.0 or 1.x.0 | - |
| 1-4 breaking changes | 0.x.y or 1.x.y | 2.0.0 | CHANGELOG + migration guide |
| 5+ breaking changes | Any | 2.0.0 | CHANGELOG + comprehensive migration guide |
| Test-only changes | Any | No change | - |

**Definition of Breaking Change**:
- Config: Required key added/removed/renamed
- Primary keys: Changed for any stream
- Replication keys: Changed for any stream
- State format: Bookmark structure changed
- Stream names: Renamed or removed
- Schema fields: Removed or data type changed
- Data formats: Timestamp format changed (Unix ↔ ISO)

**Check**:
- [ ] Version number in `setup.py` matches breaking change count
- [ ] If breaking changes exist, version MUST be ≥ 2.0.0
- [ ] CHANGELOG.md updated with version and changes
- [ ] Migration guide included for major version bumps

---

### 8. 📖 Documentation Requirements

**For Breaking Changes (version 2.0.0+)**:

**CHANGELOG.md** must include:
- [ ] Version number and release date
- [ ] "BREAKING CHANGES" section listing each change
- [ ] Migration guide for each breaking change:
  ```markdown
  ### Config Key Changes
  **Old (v1.x)**:
  ```json
  {
    "assertion": "...",
    "company_id": "..."
  }
  ```

  **New (v2.0)**:
  ```json
  {
    "username": "...",
    "password": "...",
    "company_id": "..."
  }
  ```
  
  ### Stream Renames
  | Old Stream Name | New Stream Name |
  |----------------|-----------------|
  | `invalids` | `invalid_emails` |
  | `groups_all` | `suppression_groups` |
  ```

**README.md** must include:
- [ ] Updated config examples with all required keys
- [ ] Stream table with current key_properties and replication_keys
- [ ] Version compatibility notes if applicable
- [ ] API version documented (e.g., "Uses SendGrid v3 API")

---

### 9. ✨ Code Quality Checks

**Python Best Practices**:

```bash
# Check for naked except clauses
grep -n "except:" tap_{name}/ --include="*.py" -r

# Check logging for API requests
grep -n "LOGGER.info.*request\|LOGGER.debug.*request" tap_{name}/client.py

# Check for date handling
grep -n "strftime\|strptime\|datetime" tap_{name}/streams/*.py
```

**Checklist**:
- [ ] No naked `except:` clauses (always specify exception types)
- [ ] All API requests logged with URL and sanitized params
- [ ] Progress logging present (`LOGGER.info("START Syncing: {stream}")`)
- [ ] Date handling uses RFC 3339 format (`YYYY-MM-DDTHH:MM:SSZ`)
- [ ] Timezone handling explicit (UTC preferred, pytz for conversions)
- [ ] Memory efficient - uses generators/iterators, not loading full datasets
- [ ] Module structure (proper package, not single script)
- [ ] API credentials never logged or printed

**Authentication Pattern**:
```python
# ✅ GOOD - preserves existing headers
def authenticate(self, headers, params):
    headers = headers.copy()  # Don't mutate input
    headers["Authorization"] = f"Bearer {self.config['api_key']}"
    return headers, params

# Or using dict expansion
headers = {**headers, "Authorization": f"Bearer {self.api_key}"}
```

---

### 10. 🎯 Special Implementation Patterns

#### 10.1 Dynamic Schema Discovery (OData, GraphQL, etc.)

**For taps without static schemas**:
- [ ] Metadata endpoint fetching (e.g., EDMX for OData)
- [ ] Schema generation from API metadata
- [ ] Entity/field type mapping
- [ ] Composite key handling
- [ ] Navigation property (expand) support
- [ ] Graceful handling of unavailable entities

**Commands**:
```bash
# Check for schema discovery code
grep -rn "edmx\|metadata\|\$metadata" tap_{name}/ --include="*.py"

# Verify no static schemas directory conflicts
ls tap_{name}/schemas/ 2>/dev/null || echo "No static schemas (OK for dynamic discovery)"
```

#### 10.2 Cursor-Based Pagination

**Check implementation**:
- [ ] Cursor extraction from response (links array, next_page, pagination token)
- [ ] Initial cursor handling (e.g., `cursor=initial`)
- [ ] Cursor persistence for resume capability
- [ ] Page size configuration
- [ ] Safety limits (max_pages or max_iterations)

**Commands**:
```bash
# Check pagination logic
grep -A20 "def get_records" tap_{name}/streams/abstracts.py

# Check cursor handling
grep -n "cursor\|next_page\|pagination" tap_{name}/streams/abstracts.py
```

#### 10.3 Metrics/Analytics Streams

**Check implementation**:
- [ ] Date range parameters (`from`, `to`) properly set
- [ ] Timestamp field normalization (e.g., API returns `ts`, code uses `timestamp`)
- [ ] Precision/granularity parameters documented
- [ ] Composite keys include timestamp + dimension field(s)
- [ ] Metrics aggregation documented

**Example**:
```python
# ✅ GOOD - timestamp normalization
def modify_object(self, record, parent_record=None):
    if 'ts' in record:
        record['timestamp'] = record.pop('ts')
    return record
```

#### 10.4 Two-Step Fetch Pattern (List IDs → Fetch Details)

**Check implementation**:
- [ ] First API call fetches list of IDs or minimal records
- [ ] Second API call fetches full details per ID
- [ ] Proper error handling for missing records
- [ ] Rate limiting between detail fetches

**Example** (Customer.io customers stream):
```python
# Step 1: List customer IDs
customer_ids = client.get("/v1/customers")
# Step 2: Fetch each customer's attributes
for customer_id in customer_ids:
    customer = client.get(f"/v1/customers/{customer_id}")
    yield customer
```

---

### 11. 🏗️ CI/CD Configuration

**CircleCI** (`.circleci/config.yml`):
```bash
# View CI configuration
cat .circleci/config.yml
```

**Required CI Steps**:
- [ ] Python version specified (3.11+ recommended)
- [ ] Dependency installation (pip/uv)
- [ ] Pylint enforcement (target: 9.5/10)
  ```yaml
  pylint tap_{name} -d C,R,W
  ```
- [ ] Unit tests with coverage
  ```yaml
  coverage run -m pytest tests/unittests
  coverage html
  ```
- [ ] Integration tests (tap-tester)
  ```yaml
  run-test --tap=tap-{name} tests
  ```
- [ ] Daily cron job for regression detection

**Pre-commit Hooks** (`.pre-commit-config.yaml`):
- [ ] black (code formatting)
- [ ] flake8 (linting)
- [ ] bandit (security scanning)
- [ ] codespell (typo detection)
- [ ] Standard checks (trailing-whitespace, end-of-file-fixer, check-json, check-yaml)

---

### 12. 📝 Additional Files Review

**README.md**:
- [ ] Streams table lists all streams with:
  - API documentation links
  - Primary keys
  - Replication strategy (INCREMENTAL vs FULL_TABLE)
- [ ] Config example has all required keys
- [ ] Authentication section explains API key generation
- [ ] Install instructions include Python version requirement
- [ ] Usage examples show discovery and sync commands

**CHANGELOG.md**:
- [ ] Version header with current version
- [ ] Changes listed (features, bug fixes, breaking changes)
- [ ] Migration guide if breaking changes exist

**setup.py**:
- [ ] Correct version number
- [ ] Dependencies pinned to specific versions
- [ ] Package data includes schemas: `package_data = {"tap_{name}": ["schemas/*.json"]}`
- [ ] Entry point defined: `tap-{name}=tap_{name}:main`
- [ ] `extras_require` has dev dependencies (pytest, coverage, pylint)

---

## 🎯 Approval Decision Matrix

### ✅ APPROVED (Merge Ready)

**Criteria**:
- Zero breaking changes (or initial release with version 0.0.1)
- Schema validation: 0 errors
- Exception definitions verified against API docs
- Test coverage: Comprehensive or Adequate
- No `while True` loops
- No silent record skipping
- Version number appropriate
- CI/CD configured

**Actions**: None - ready to merge

---

### ⚠️ CONDITIONAL APPROVAL (Merge with Requirements)

**Criteria**:
- Breaking changes exist BUT:
  - Version bumped to 2.0.0+
  - CHANGELOG has migration guide
  - README updated
- OR minor issues that don't block merge:
  - Schema warnings only (no errors)
  - Documentation needs minor updates
  - Test coverage adequate but not comprehensive

**Actions**: 
- List required actions before merge
- Example: "Update version from 0.1.0 → 2.0.0 in setup.py"
- Example: "Add migration guide to CHANGELOG.md"

---

### ❌ REJECTED (Do Not Merge)

**Blocking Issues**:
- Schema validation errors exist (not warnings)
- `while True` loops without safety limits
- Silent record skipping (continue in except without logging)
- Exception definitions don't match API documentation
- Breaking changes exist but version not bumped to 2.0.0+
- Missing critical tests (no client error handling tests)
- Infinite loop risk identified
- Config validation issues

**Actions**:
- List all blocking issues
- Request fixes before re-review

---

## 📄 Review Output Template

```markdown
## ✅ PR Review: tap-{name} PR #{number} - {title}

---

### 📊 PR Overview

**Type**: [Initial implementation | Complete rewrite | Bug fixes | Feature additions | Test additions]
**Scope**: {brief description}
**Master baseline**: {description of master state}
**Changes**: {X} files, +{Y}/-{Z} lines
**Version**: {old_version} → {new_version}

---

### 🔐 Configuration Properties

**REQUIRED_CONFIG_KEYS**: `{keys}`

**Optional Parameters**:
- `{param_name}` ({type}, default: {value}): {description}

✅ **NO BREAKING CHANGES** | ❌ **BREAKING CHANGES**:
- {specific change 1}
- {specific change 2}

---

### ⚠️ Exception Definitions

#### Exception Hierarchy

```python
{TapName}Error (base)
├── {TapName}BackoffError (retryable base)
│   ├── {TapName}RateLimitError (429)
│   ├── {TapName}InternalServerError (500)
│   └── ...
├── {TapName}BadRequestError (400)
├── {TapName}UnauthorizedError (401)
└── ...
```

#### Backoff Configuration

```python
@backoff.on_exception(
    wait_gen=backoff.expo,
    exception=({exceptions}),
    max_tries={N},
    factor={F}
)
```

#### Verification Against {API Name} API

✅ **VERIFIED** | ❌ **INCOMPLETE** | ⚠️ **PARTIAL**

| Status | Exception | API Documentation | Verification |
|--------|-----------|-------------------|--------------|
| **400** | `{Tap}BadRequestError` | {API behavior} | ✅/❌ {notes} |
| **401** | `{Tap}UnauthorizedError` | {API behavior} | ✅/❌ {notes} |
| ... | ... | ... | ... |

**Issues Found**: {list or "None"}

---

### 📋 Schema Validation

**Command**:
```bash
cd /opt/code/tap-{name}
python /opt/code/singer-tap-generator/utils/validate_singer_schemas.py tap_{name}/schemas/
```

**Results**: ✅ **{X} errors, {Y} warnings**

```
Found {N} schema files in tap_{name}/schemas

SUMMARY: {X} errors, {Y} warnings
```

**Analysis**:
- Errors: {list or "None - all schemas valid"}
- Warnings: {summary of warnings - typically non-nullable primary keys which is expected}

✅ **PASSED** | ❌ **FAILED**

**Schema Files** ({N} total):
- {list major streams}

---

### 🔍 Breaking Changes Analysis

#### 2.1 Config Properties
✅ **NO CHANGE** | ❌ **BREAKING**
- {details}

#### 2.2 Primary Keys (key_properties)
✅ **NO CHANGE** | ❌ **BREAKING**
- {stream_name}: {old} → {new}

#### 2.3 Replication Keys
✅ **NO CHANGE** | ❌ **BREAKING**
- {stream_name}: {old} → {new}

#### 2.4 State Structure
✅ **NO CHANGE** | ❌ **BREAKING**
- {details}

#### 2.5 Stream Changes
✅ **NO CHANGE** | ❌ **BREAKING**
- Removed: {list}
- Renamed: {list}
- Added: {list}

#### 2.6 Schema Fields
✅ **NO CHANGE** | ❌ **BREAKING**
- {details}

**Total Breaking Changes**: {count}

---

### 🔁 Loop Safety

**Check**:
```bash
grep -rn "while True" tap_{name}/ --include="*.py"
```

✅ **PASSED** - No `while True` loops found | ❌ **FAILED** - {count} instances found

**Analysis**: {details if failed}

---

### 📊 Record Completeness

**Check**:
```bash
grep -A5 "except.*:" tap_{name}/streams/*.py | grep "continue"
```

✅ **PASSED** - No silent record skipping | ⚠️ **WARNING** - {details}

**Analysis**: {details if issues found}

---

### 🧪 Test Coverage

**Integration Tests**: {count} files, {lines} lines
- Files: {list test files}

**Unit Tests**: {count} files, {lines} lines
- Files: {list test files}
- Client error handling: ✅ Comprehensive | ⚠️ Partial | ❌ Missing

**Assessment**: ✅ Comprehensive | ⚠️ Adequate | ❌ Insufficient

**Coverage Highlights**:
- {specific test coverage notes}

---

### 🏗️ Code Quality

**CircleCI Configuration**: ✅ Present | ❌ Missing
- Python version: {version}
- Pylint: ✅ Enforced | ❌ Not configured
- Unit tests: ✅ Configured | ❌ Missing
- Integration tests: ✅ Configured | ❌ Missing
- Daily cron: ✅ Present | ❌ Missing

**Pre-commit Hooks**: ✅ Configured | ❌ Missing
- {list configured hooks}

---

### 📝 Recommendations

#### Required Before Merge
{list or "None - implementation is production-ready"}

#### Best Practices Observed
{list positive findings}

#### Optional Enhancements
{list optional improvements}

---

### 🎯 Final Approval

## {✅ **APPROVED** | ⚠️ **CONDITIONAL APPROVAL** | ❌ **REJECTED**}

**Rationale**:
{detailed reasoning for approval decision}

**Required Actions**: {list or "None"}

**Next Steps**:
{list recommended next steps}

---

### 📊 Summary Statistics

| Metric | Count |
|--------|-------|
| Files Changed | {N} |
| Lines Added | {N} |
| Lines Deleted | {N} |
| Streams | {N} |
| Schemas | {N} |
| Schema Validation Errors | {N} |
| Exception Classes | {N} |
| Integration Tests | {N} |
| Unit Test Files | {N} |
| Test Coverage Lines | {N} |
| Python Version | {version} |
| Breaking Changes | {N} |

---
```

---

## 🔍 Common Issues Found in Reviews

### Issue 1: Version Number Too Low for Breaking Changes
**Problem**: Version 0.1.0 with 5 breaking changes
**Solution**: Bump to 2.0.0
**Example**: tap-sendgrid PR #25

### Issue 2: Missing Date-Time Format
**Problem**: Timestamp fields without `"format": "date-time"`
**Solution**: Add format to all date/time fields in schemas
**Validation**: Schema validation script catches this

### Issue 3: While True Loops
**Problem**: `while True:` in pagination code
**Solution**: Replace with explicit condition and max_iterations safety
**Pattern**:
```python
# Replace this:
while True:
    if not has_more:
        break

# With this:
max_pages = 10000
page = 0
while has_more and page < max_pages:
    # ...
    page += 1
```

### Issue 4: Silent Record Skipping
**Problem**: `except: continue` without logging
**Solution**: Log all exceptions before skipping
**Pattern**:
```python
# Add logging:
except Exception as e:
    LOGGER.error(f"Failed to process record {record_id}: {e}")
    # Then decide: raise or continue with justification
```

### Issue 5: Incomplete Exception Mapping
**Problem**: API returns 409, 422 but not in ERROR_CODE_EXCEPTION_MAPPING
**Solution**: Add all documented status codes
**Verification**: Compare with API error documentation

---

## 🛠️ Debugging During Review

**Test Config Location**: `/tmp/config.json`

**Quick Test Commands**:
```bash
# Discovery mode
cd /opt/code/tap-{name}
tap-{name} --config /tmp/config.json --discover > catalog.json

# Sync mode (dry run)
tap-{name} --config /tmp/config.json --catalog catalog.json | head -50

# Check for errors
tap-{name} --config /tmp/config.json --discover 2>&1 | grep -i error
```

---

## 📚 Reference Documentation

**Singer Specification**:
- Getting Started: https://github.com/singer-io/getting-started
- Sync Mode: https://github.com/singer-io/getting-started/blob/master/docs/SYNC_MODE.md
- Discovery Mode: https://github.com/singer-io/getting-started/blob/master/docs/DISCOVERY_MODE.md
- Best Practices: https://github.com/singer-io/getting-started/blob/master/docs/BEST_PRACTICES.md

**Internal Tools**:
- Schema validator: `/opt/code/singer-tap-generator/utils/validate_singer_schemas.py`
- Tap tester framework: `/opt/code/tap-tester/`

---

**Last Updated**: 2026-03-31 (based on tap-sparkpost, tap-sendgrid, tap-sap-success-factors reviews)
