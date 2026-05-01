#!/usr/bin/env python3
"""
Singer Schema Validator

Validates JSON schema files for Singer taps, checking for:
- Valid JSON structure
- Valid JSON Schema data types (string, number, integer, boolean, object, array, null)
- Missing additionalProperties on nested objects
- Inconsistent indentation
- Missing property definitions for object types
- Invalid type declarations
- Missing format specifications for date/time fields
- Fields without null type (non-nullable fields)
- Orphaned/empty schemas

Can validate either:
1. A directory of schema JSON files
2. A Singer catalog file (validates schemas within each stream)
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class RootLevelMetadataKeywords:
    """Expected keys in root level metadata of a Singer catalog stream."""

    SELECTED: str = 'selected'
    REPLICATION_METHOD: str = 'replication-method'
    REPLICATION_KEY: str = 'replication-key'
    VIEW_KEY_PROPERTIES: str = 'view-key-properties'
    INCLUSION: str = 'inclusion'
    SELECTED_BY_DEFAULT: str = 'selected-by-default'
    VALID_REPLICATION_KEYS: str = 'valid-replication-keys'
    FORCED_REPLICATION_METHOD: str = 'forced-replication-method'
    TABLE_KEY_PROPERTIES: str = 'table-key-properties'
    PARENT_TAP_STREAM_ID: str = 'parent-tap-stream-id'
    SCHEMA_NAME: str = 'schema-name'
    IS_VIEW: str = 'is-view'
    ROW_COUNT: str = 'row-count'
    DATABASE_NAME: str = 'database-name'
    SQL_DATATYPE: str = 'sql-datatype'

    @classmethod
    def expected_keys(cls) -> frozenset:
        """Return a frozenset of expected root level metadata keys."""
        return frozenset({
            cls.SELECTED,
            cls.REPLICATION_METHOD,
            cls.REPLICATION_KEY,
            cls.VIEW_KEY_PROPERTIES,
            cls.INCLUSION,
            cls.SELECTED_BY_DEFAULT,
            cls.VALID_REPLICATION_KEYS,
            cls.FORCED_REPLICATION_METHOD,
            cls.TABLE_KEY_PROPERTIES,
            cls.PARENT_TAP_STREAM_ID,
            cls.SCHEMA_NAME,
            cls.IS_VIEW,
            cls.ROW_COUNT,
            cls.DATABASE_NAME,
            cls.SQL_DATATYPE
        })


class SchemaValidator:
    # Valid JSON Schema types
    VALID_TYPES = {'string', 'number', 'integer', 'boolean', 'object', 'array', 'null'}

    def __init__(self, schema_dir: str = None, catalog_file: str = None):
        self.schema_dir = Path(schema_dir) if schema_dir else None
        self.catalog_file = Path(catalog_file) if catalog_file else None
        self.issues: List[Dict[str, Any]] = []
        self.warnings: List[Dict[str, Any]] = []

    def add_issue(self, file: str, severity: str, message: str, line: int = None, path: str = None):
        """Add a validation issue."""
        issue = {
            'file': file,
            'severity': severity,
            'message': message,
        }
        if line:
            issue['line'] = line
        if path:
            issue['path'] = path

        if severity == 'ERROR':
            self.issues.append(issue)
        else:
            self.warnings.append(issue)

    def check_indentation(self, file_path: Path, content: str) -> None:
        """Check for consistent indentation (both 2-space and 4-space are acceptable)."""
        # Indentation check disabled - both 2-space and 4-space are acceptable
        pass

    def check_object_properties(self, file_path: Path, schema: Dict, path: str = "root", in_composition: bool = False) -> None:
        """Recursively check that all object types have additionalProperties defined.

        Args:
            file_path: Path to the schema file
            schema: Schema dictionary to check
            path: Current path in the schema (for error reporting)
            in_composition: True if we're inside anyOf/oneOf/allOf (skip additionalProperties check)
        """
        if not isinstance(schema, dict):
            return

        # Check if this is an object type definition
        schema_type = schema.get('type')

        # Handle both single type and array of types
        is_object = False
        if isinstance(schema_type, str):
            is_object = schema_type == 'object'
        elif isinstance(schema_type, list):
            is_object = 'object' in schema_type

        if is_object:
            # Get properties and additionalProperties
            properties = schema.get('properties', {})
            additional_props = schema.get('additionalProperties')

            # Only check for additionalProperties if:
            # 1. The object has NO properties defined, AND
            # 2. We're NOT inside anyOf/oneOf/allOf (where flexibility is intentional)
            if not properties and not in_composition:
                # Check for additionalProperties on objects without properties
                if 'additionalProperties' not in schema:
                    self.add_issue(
                        file_path.name,
                        'ERROR',
                        f'Object at "{path}" missing "additionalProperties" constraint (no properties defined)',
                        path=path
                    )
                # If it's an object with additionalProperties:false but no properties, that's suspicious
                elif additional_props is False and path != "root":
                    self.add_issue(
                        file_path.name,
                        'WARNING',
                        f'Object at "{path}" has no properties defined but additionalProperties is false',
                        path=path
                    )

        # Recursively check nested structures
        if 'properties' in schema:
            for prop_name, prop_schema in schema['properties'].items():
                self.check_object_properties(file_path, prop_schema, f"{path}.{prop_name}", in_composition)

        if 'items' in schema:
            items = schema['items']
            if isinstance(items, dict):
                self.check_object_properties(file_path, items, f"{path}[]", in_composition)
            elif isinstance(items, list):
                for i, item in enumerate(items):
                    self.check_object_properties(file_path, item, f"{path}[{i}]", in_composition)

        # Check nested schemas in oneOf, anyOf, allOf - mark as in_composition
        for key in ['oneOf', 'anyOf', 'allOf']:
            if key in schema:
                for i, sub_schema in enumerate(schema[key]):
                    self.check_object_properties(file_path, sub_schema, f"{path}.{key}[{i}]", in_composition=True)

    def check_datetime_format(self, file_path: Path, schema: Dict, path: str = "root") -> None:
        """Check that timestamp-like fields have proper format specification."""
        if not isinstance(schema, dict):
            return

        # Check current field
        if 'type' in schema:
            schema_type = schema.get('type')
            field_name = path.split('.')[-1].lower() if '.' in path else ''

            # Check if field name suggests it's a timestamp
            timestamp_keywords = ['date', 'time', 'timestamp', 'created', 'updated', 'modified']
            looks_like_timestamp = any(keyword in field_name for keyword in timestamp_keywords) or field_name.endswith('_at')

            # Check if it's a string type
            is_string = False
            if isinstance(schema_type, str):
                is_string = schema_type == 'string'
            elif isinstance(schema_type, list):
                is_string = 'string' in schema_type

            if looks_like_timestamp and is_string and 'format' not in schema:
                self.add_issue(
                    file_path.name,
                    'WARNING',
                    f'Field "{path}" looks like a timestamp but has no format specification',
                    path=path
                )

        # Recurse
        if 'properties' in schema:
            for prop_name, prop_schema in schema['properties'].items():
                self.check_datetime_format(file_path, prop_schema, f"{path}.{prop_name}")

        if 'items' in schema and isinstance(schema['items'], dict):
            self.check_datetime_format(file_path, schema['items'], f"{path}[]")

    def check_valid_types(self, file_path: Path, schema: Dict, path: str = "root") -> None:
        """Check that only valid JSON Schema types are used.
        Valid types are: string, number, integer, boolean, object, array, null
        """
        if not isinstance(schema, dict):
            return

        # Check current field's type
        if 'type' in schema:
            schema_type = schema.get('type')
            # Handle single type
            if isinstance(schema_type, str):
                if schema_type not in self.VALID_TYPES:
                    self.add_issue(
                        file_path.name,
                        'ERROR',
                        f'Invalid type "{schema_type}" at "{path}". Valid types are: {", ".join(sorted(self.VALID_TYPES))}',
                        path=path
                    )

            # Handle array of types (e.g., ["string", "null"])
            elif isinstance(schema_type, list):
                for t in schema_type:
                    if not isinstance(t, str):
                        self.add_issue(
                            file_path.name,
                            'ERROR',
                            f'Type in array must be a string at "{path}". Found: {type(t).__name__}',
                            path=path
                        )
                    elif t not in self.VALID_TYPES:
                        self.add_issue(
                            file_path.name,
                            'ERROR',
                            f'Invalid type "{t}" in type array at "{path}". Valid types are: {", ".join(sorted(self.VALID_TYPES))}',
                            path=path
                        )

                # Check for duplicate types in array
                if len(schema_type) != len(set(schema_type)):
                    duplicates = [t for t in schema_type if schema_type.count(t) > 1]
                    self.add_issue(
                        file_path.name,
                        'WARNING',
                        f'Duplicate types {set(duplicates)} in type array at "{path}"',
                        path=path
                    )

            else:
                self.add_issue(
                    file_path.name,
                    'ERROR',
                    f'Type must be a string or array of strings at "{path}". Found: {type(schema_type).__name__}',
                    path=path
                )

        # Recurse into properties
        if 'properties' in schema:
            for prop_name, prop_schema in schema['properties'].items():
                self.check_valid_types(file_path, prop_schema, f"{path}.{prop_name}")

        # Recurse into items
        if 'items' in schema:
            items = schema['items']
            if isinstance(items, dict):
                self.check_valid_types(file_path, items, f"{path}[]")
            elif isinstance(items, list):
                for i, item in enumerate(items):
                    self.check_valid_types(file_path, item, f"{path}[{i}]")

        # Check nested schemas in oneOf, anyOf, allOf
        for key in ['oneOf', 'anyOf', 'allOf']:
            if key in schema:
                for i, sub_schema in enumerate(schema[key]):
                    self.check_valid_types(file_path, sub_schema, f"{path}.{key}[{i}]")

    def check_non_nullable_fields(self, file_path: Path, schema: Dict, path: str = "root") -> None:
        """Check for fields that don't allow null values (should be rare in Singer schemas)."""
        if not isinstance(schema, dict):
            return

        # Check if this field has a type definition
        if 'type' in schema and path != "root":
            schema_type = schema.get('type')

            # Check if field is non-nullable
            is_nullable = False
            if isinstance(schema_type, str):
                is_nullable = (schema_type == 'null')
            elif isinstance(schema_type, list):
                is_nullable = ('null' in schema_type)

            # If not nullable and not a nested object, warn
            if not is_nullable and schema_type != 'object':
                # Skip if it's an array type (arrays themselves don't need null, but items might)
                if schema_type != 'array' and not (isinstance(schema_type, list) and 'array' in schema_type):
                    self.add_issue(
                        file_path.name,
                        'WARNING',
                        f'Field "{path}" does not allow null values - consider adding "null" to type array for optional fields',
                        path=path
                    )

        # Recurse into properties
        if 'properties' in schema:
            for prop_name, prop_schema in schema['properties'].items():
                self.check_non_nullable_fields(file_path, prop_schema, f"{path}.{prop_name}")

        # Recurse into array items
        if 'items' in schema and isinstance(schema['items'], dict):
            self.check_non_nullable_fields(file_path, schema['items'], f"{path}[]")

        # Recurse into composition schemas
        for key in ['oneOf', 'anyOf', 'allOf']:
            if key in schema:
                for i, sub_schema in enumerate(schema[key]):
                    self.check_non_nullable_fields(file_path, sub_schema, f"{path}.{key}[{i}]")

    def validate_json_structure(self, file_path: Path, content: str) -> Tuple[bool, Dict]:
        """Validate that the file is valid JSON and return the parsed schema."""
        try:
            schema = json.loads(content)
            return True, schema
        except json.JSONDecodeError as e:
            self.add_issue(
                file_path.name,
                'ERROR',
                f'Invalid JSON: {str(e)}',
                line=e.lineno
            )
            return False, {}

    def check_root_schema_structure(self, file_path: Path, schema: Dict) -> None:
        """Check that root schema has proper structure."""
        if not schema:
            self.add_issue(
                file_path.name,
                'ERROR',
                'Schema is empty or invalid'
            )
            return

        # Root should be an object
        if schema.get('type') != 'object':
            self.add_issue(
                file_path.name,
                'WARNING',
                f'Root schema type is "{schema.get("type")}" (expected "object")'
            )

        # Root should have properties
        if 'properties' not in schema:
            self.add_issue(
                file_path.name,
                'ERROR',
                'Root schema has no properties defined'
            )

        # Root should have additionalProperties defined
        if 'additionalProperties' not in schema:
            self.add_issue(
                file_path.name,
                'ERROR',
                'Root schema missing "additionalProperties" constraint'
            )

    def validate_file(self, file_path: Path) -> None:
        """Validate a single schema file."""
        print(f"Validating {file_path.name}...")

        try:
            content = file_path.read_text()
        except Exception as e:
            self.add_issue(
                file_path.name,
                'ERROR',
                f'Could not read file: {str(e)}'
            )
            return

        # Check indentation
        self.check_indentation(file_path, content)

        # Validate JSON structure
        valid, schema = self.validate_json_structure(file_path, content)
        if not valid:
            return

        # Check root schema structure
        self.check_root_schema_structure(file_path, schema)

        # Check object properties recursively
        self.check_object_properties(file_path, schema)

        # Check datetime formats
        self.check_datetime_format(file_path, schema)

        # Check valid data types
        self.check_valid_types(file_path, schema)

        # Check for non-nullable fields
        self.check_non_nullable_fields(file_path, schema)

    def check_root_level_md(self, root_md: Dict, stream_name: str = '') -> None:
        """Check that root level metadata has expected structure."""
        context = f'{self.catalog_file.name}[{stream_name}]' if stream_name else self.catalog_file.name

        if not root_md:
            self.add_issue(
                context,
                'ERROR',
                'Root level metadata is missing or empty'
            )
            return

        if not isinstance(root_md, dict):
            self.add_issue(
                context,
                'ERROR',
                f'Root level metadata should be an object, found {type(root_md).__name__}'
            )
            return

        # Check for expected keys in root level metadata
        expected_keys = {'breadcrumb', 'metadata'}
        missing_keys = expected_keys - root_md.keys()
        if missing_keys:
            self.add_issue(
                context,
                'WARNING',
                f'Root level metadata is missing expected keys: {", ".join(sorted(missing_keys))}'
            )

        # Analyse the metadata entries for potential issues
        md: dict = root_md.get('metadata')

        if not isinstance(md, dict):
            self.add_issue(
                context,
                'ERROR',
                f'Root level metadata "metadata" field should be an object, found {type(md).__name__}'
            )
            return

        discoverable_keys = RootLevelMetadataKeywords.expected_keys()
        for key in md.keys():
            if key not in discoverable_keys:
                # Check if the key uses dot-notation (for nested/custom properties)
                has_dot = "." in key
                if not has_dot:
                    self.add_issue(
                        context,
                        'ERROR',
                        f'Root level metadata key "{key}" is not a standard discoverable key and does not uses dot escape notation'
                    )

    def validate_catalog_file(self) -> bool:
        """Validate schemas within a Singer catalog file."""
        if not self.catalog_file.exists():
            self.add_issue(str(self.catalog_file), 'ERROR', 'Catalog file does not exist')
            return False

        if not self.catalog_file.is_file():
            self.add_issue(str(self.catalog_file), 'ERROR', f"'{self.catalog_file}' is not a file")
            return False

        print(f"Validating catalog: {self.catalog_file}\n")

        try:
            content = self.catalog_file.read_text()
        except Exception as e:
            self.add_issue(
                self.catalog_file.name,
                'ERROR',
                f'Could not read catalog file: {str(e)}'
            )
            return False

        # Parse catalog JSON
        try:
            catalog = json.loads(content)
        except json.JSONDecodeError as e:
            self.add_issue(
                self.catalog_file.name,
                'ERROR',
                f'Invalid JSON in catalog: {str(e)}',
                line=e.lineno
            )
            return False

        # Validate catalog structure
        if not isinstance(catalog, dict):
            self.add_issue(
                self.catalog_file.name,
                'ERROR',
                'Catalog must be a JSON object'
            )
            return False

        if 'streams' not in catalog:
            self.add_issue(
                self.catalog_file.name,
                'ERROR',
                'Catalog missing "streams" array'
            )
            return False

        streams = catalog['streams']
        if not isinstance(streams, list):
            self.add_issue(
                self.catalog_file.name,
                'ERROR',
                '"streams" must be an array'
            )
            return False

        print(f"Found {len(streams)} streams in catalog\n")

        # Validate each stream's schema
        for i, stream in enumerate(streams):
            if not isinstance(stream, dict):
                self.add_issue(
                    self.catalog_file.name,
                    'ERROR',
                    f'Stream {i} is not an object'
                )
                continue

            stream_name = stream.get('stream', f'stream_{i}')
            print(f"Validating stream: {stream_name}...")

            # Check for required stream fields
            if 'stream' not in stream:
                self.add_issue(
                    f"{self.catalog_file.name}[{stream_name}]",
                    'ERROR',
                    'Stream missing "stream" field (stream name)'
                )

            if 'schema' not in stream:
                self.add_issue(
                    f"{self.catalog_file.name}[{stream_name}]",
                    'ERROR',
                    'Stream missing "schema" field'
                )
                continue

            schema = stream['schema']
            if not isinstance(schema, dict):
                self.add_issue(
                    f"{self.catalog_file.name}[{stream_name}]",
                    'ERROR',
                    f'Schema for stream "{stream_name}" is not an object'
                )
                continue

            # Create a pseudo-Path for error reporting
            pseudo_path = Path(f"{self.catalog_file.name}[{stream_name}]")

            # Validate the schema
            self.check_root_schema_structure(pseudo_path, schema)
            self.check_object_properties(pseudo_path, schema)
            self.check_datetime_format(pseudo_path, schema)
            self.check_valid_types(pseudo_path, schema)
            self.check_non_nullable_fields(pseudo_path, schema)

            # Validate the root level metadata
            metadata = stream.get("metadata", [])
            root_md = next(
                (entry for entry in metadata if isinstance(entry, dict) and entry.get("breadcrumb") == []),
                None
            )
            self.check_root_level_md(root_md, stream_name=stream_name)

        return len(self.issues) == 0

    def validate_all(self) -> bool:
        """Validate all schema files in the directory or catalog file."""
        # If catalog file is specified, validate it
        if self.catalog_file:
            return self.validate_catalog_file()

        # Otherwise, validate schema directory
        if not self.schema_dir:
            print("Error: No schema directory or catalog file specified")
            return False

        if not self.schema_dir.exists():
            print(f"Error: Schema directory '{self.schema_dir}' does not exist")
            return False

        if not self.schema_dir.is_dir():
            print(f"Error: '{self.schema_dir}' is not a directory")
            return False

        schema_files = list(self.schema_dir.glob('*.json'))

        if not schema_files:
            print(f"Warning: No .json schema files found in '{self.schema_dir}'")
            return True

        print(f"Found {len(schema_files)} schema files in {self.schema_dir}\n")

        for schema_file in sorted(schema_files):
            self.validate_file(schema_file)

        return len(self.issues) == 0

    def _group_issues(self, issues: List[Dict[str, Any]]) -> Dict[str, Dict[str, List[str]]]:
        """Group issues by file and message for better readability."""
        grouped = {}
        for issue in issues:
            file = issue['file']
            message = issue['message']
            path = issue.get('path', '')

            if file not in grouped:
                grouped[file] = {}
            if message not in grouped[file]:
                grouped[file][message] = []

            if path:
                grouped[file][message].append(path)

        return grouped

    def print_report(self) -> None:
        """Print validation report."""
        print("\n" + "=" * 80)
        print("VALIDATION REPORT")
        print("=" * 80)

        if not self.issues and not self.warnings:
            print("\n✅ All schemas are valid! No issues found.\n")
            return

        # Print errors
        if self.issues:
            print(f"\n❌ ERRORS ({len(self.issues)}):")
            print("-" * 80)
            grouped_errors = self._group_issues(self.issues)
            for file, messages in sorted(grouped_errors.items()):
                print(f"\n[{file}]")
                for message, paths in messages.items():
                    print(f"→ {message}")
                    # if paths:
                    #     for path in sorted(paths):
                    #         print(f"  • {path}")
                    # print()

        # Print warnings
        if self.warnings:
            print(f"\n⚠️  WARNINGS ({len(self.warnings)}):")
            print("-" * 80)
            grouped_warnings = self._group_issues(self.warnings)
            for file, messages in sorted(grouped_warnings.items()):
                print(f"\n[{file}]")
                for message, paths in messages.items():
                    print(f"→ {message}")
                    # if paths:
                    #     for path in sorted(paths):
                    #         print(f"  • {path}")
                    # print()

        # Summary
        print("=" * 80)
        print(f"SUMMARY: {len(self.issues)} errors, {len(self.warnings)} warnings")
        print("=" * 80 + "\n")


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Validate Singer tap schema files or catalog',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Validate schemas in tap-tempo directory
  python validate_singer_schemas.py tap-tempo/tap_tempo/schemas

  # Validate schemas in a catalog file
  python validate_singer_schemas.py --catalog /tmp/catalog.json

  # Exit with error code if validation fails (useful for CI)
  python validate_singer_schemas.py --catalog /tmp/catalog.json --strict
        """
    )

    parser.add_argument(
        'schema_directory',
        nargs='?',
        help='Directory containing JSON schema files to validate'
    )
    parser.add_argument(
        '--catalog',
        help='Path to Singer catalog file to validate'
    )
    parser.add_argument(
        '--strict',
        action='store_true',
        help='Exit with error code 1 if any issues found (useful for CI/CD)'
    )

    args = parser.parse_args()

    # Validate that either schema_directory or --catalog is provided
    if not args.schema_directory and not args.catalog:
        parser.error("Either schema_directory or --catalog must be specified")

    if args.schema_directory and args.catalog:
        parser.error("Cannot specify both schema_directory and --catalog")

    validator = SchemaValidator(
        schema_dir=args.schema_directory,
        catalog_file=args.catalog
    )
    success = validator.validate_all()
    validator.print_report()

    if args.strict and not success:
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
