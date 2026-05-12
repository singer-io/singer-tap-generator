#!/usr/bin/env python3
"""
Menagerie Catalog Validator

Validates Singer catalog files using the same validation rules as 
stitch-menagerie-service (schemas.clj).

This is a standalone validator that doesn't require Clojure dependencies.

Validation Rules:
1. Schema type validation (must be valid JSON Schema types)
2. Replication method consistency (FULL_TABLE, INCREMENTAL, LOG_BASED)
3. Required properties (key_properties and replication_key must exist in schema)
4. Metadata breadcrumb validation
5. Inclusion and selected field validation
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


@dataclass
class ValidationResult:
    """Result of validation with errors and warnings."""
    errors: List[str]
    warnings: List[str]
    stream_name: str = ""

    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def has_issues(self) -> bool:
        return len(self.errors) > 0 or len(self.warnings) > 0


class MenagerieValidator:
    """Validates Singer catalog files using Menagerie service rules."""

    # Valid JSON Schema types (from schemas.clj)
    VALID_TYPES = {"null", "object", "string", "number", "array", "boolean", "integer"}

    # Valid inclusion values
    VALID_INCLUSION_VALUES = {"automatic", "available", "unsupported"}

    # Replication methods
    FULL_TABLE = "FULL_TABLE"
    INCREMENTAL = "INCREMENTAL"
    LOG_BASED = "LOG_BASED"
    VALID_REPLICATION_METHODS = {FULL_TABLE, INCREMENTAL, LOG_BASED}

    def __init__(self, catalog_file: str):
        self.catalog_file = Path(catalog_file)
        self.results: List[ValidationResult] = []

    def get_schema_types(self, schema: Dict) -> Set[str]:
        """Extract type(s) from schema - handles both string and array of types."""
        schema_type = schema.get("type")
        if isinstance(schema_type, str):
            return {schema_type}
        elif isinstance(schema_type, list):
            return set(schema_type)
        return set()

    def validate_json_schema(self, schema: Any, path: str = "root") -> List[str]:
        """
        Validate JSON schema structure and types.

        Implements the validate-json-schema function from schemas.clj
        """
        errors = []

        if not isinstance(schema, dict):
            return errors

        # Get schema types
        types = self.get_schema_types(schema)

        # Validate inclusion value
        inclusion = schema.get("inclusion")
        if inclusion and inclusion not in self.VALID_INCLUSION_VALUES:
            errors.append(f"Invalid inclusion value '{inclusion}' at {path}")

        # Validate selected field (must be boolean)
        selected = schema.get("selected")
        if selected is not None and not isinstance(selected, bool):
            errors.append(f"Invalid selected value (must be boolean) at {path}")

        # Validate types are valid
        invalid_types = types - self.VALID_TYPES
        if invalid_types:
            errors.append(f"Invalid type(s) {invalid_types} at {path}")
        
        # Validate items (for arrays)
        if "items" in schema:
            if "array" not in types:
                errors.append(f"'items' defined without 'array' type at {path}")
            errors.extend(self.validate_json_schema(schema["items"], f"{path}[]"))
        
        # Validate properties (for objects)
        if "properties" in schema:
            if "object" not in types:
                errors.append(f"'properties' defined without 'object' type at {path}")
            
            properties = schema["properties"]
            if not isinstance(properties, dict):
                errors.append(f"'properties' must be an object at {path}")
            else:
                for prop_name, prop_schema in properties.items():
                    if not isinstance(prop_name, str):
                        errors.append(f"Property name must be string at {path}")
                    errors.extend(self.validate_json_schema(
                        prop_schema, 
                        f"{path}.{prop_name}"
                    ))
        
        return errors

    def validate_table_settings(self, stream: Dict) -> List[str]:
        """
        Validate replication method and key consistency.
        
        Implements the validate-table-settings function from schemas.clj
        """
        errors = []
        
        replication_method = stream.get("replication_method")
        replication_key = stream.get("replication_key")
        
        # If neither is set, that's OK
        if not replication_method and not replication_key:
            return errors
        
        # Validate replication method value
        if replication_method and replication_method not in self.VALID_REPLICATION_METHODS:
            errors.append(f"Invalid replication method: {replication_method}")
            return errors
        
        # FULL_TABLE: replication_key must NOT be set
        if replication_method == self.FULL_TABLE:
            if replication_key:
                errors.append("Replication key must not be set for FULL_TABLE replication")
        
        # LOG_BASED: replication_key must NOT be set
        elif replication_method == self.LOG_BASED:
            if replication_key:
                errors.append("Replication key must not be set for LOG_BASED replication")
        
        # INCREMENTAL: replication_key MUST be set
        elif replication_method == self.INCREMENTAL:
            if not replication_key:
                errors.append("Replication key must be set for INCREMENTAL replication")
        
        # No replication method but has replication key
        elif replication_key and not replication_method:
            errors.append("Replication key must not be set if replication method is not set")
        
        return errors

    def validate_required_properties(self, stream: Dict) -> List[str]:
        """
        Validate that key_properties and replication_key exist in schema.
        
        Implements the validate-required-properties-for-stream function from schemas.clj
        """
        errors = []
        
        schema = stream.get("schema", {})
        properties = schema.get("properties", {})
        discovered_fields = set(properties.keys())
        
        key_properties = set(stream.get("key_properties", []))
        replication_key = stream.get("replication_key")
        replication_key_set = {replication_key} if replication_key else set()
        
        # All key properties and replication key must exist in schema
        required_fields = key_properties | replication_key_set
        missing_fields = required_fields - discovered_fields
        
        if missing_fields:
            stream_id = stream.get("tap_stream_id", stream.get("stream", "unknown"))
            errors.append(
                f"Error in schema for stream: {stream_id}. "
                f"Replication key and key properties must be present in schema. "
                f"Missing: {missing_fields}"
            )
        
        return errors

    def validate_metadata_breadcrumbs(self, stream: Dict) -> List[str]:
        """
        Validate that metadata breadcrumbs point to valid schema paths.
        
        Implements the validate-metadata function from schemas.clj
        """
        errors = []
        
        metadata = stream.get("metadata", [])
        schema = stream.get("schema", {})
        
        for entry in metadata:
            if not isinstance(entry, dict):
                errors.append("Metadata entry must be an object")
                continue
            
            breadcrumb = entry.get("breadcrumb", [])
            
            # Navigate to the schema node using breadcrumb
            current_node = schema
            path = "schema"
            
            for crumb in breadcrumb:
                path += f".{crumb}"
                
                if isinstance(current_node, dict):
                    if crumb == "properties":
                        current_node = current_node.get("properties", {})
                    elif "properties" in current_node:
                        current_node = current_node["properties"].get(crumb)
                    else:
                        current_node = current_node.get(crumb)
                    
                    if current_node is None:
                        errors.append(f"Invalid breadcrumb path: {breadcrumb} (failed at {path})")
                        break
                else:
                    errors.append(f"No valid node at breadcrumb {breadcrumb}")
                    break
        
        return errors

    def validate_stream(self, stream: Dict) -> ValidationResult:
        """Validate a single stream with all validation rules."""
        stream_name = stream.get("stream", stream.get("tap_stream_id", "unknown"))
        errors = []
        warnings = []
        
        if not isinstance(stream, dict):
            return ValidationResult(
                errors=["Stream must be an object"],
                warnings=[],
                stream_name=stream_name
            )
        
        # Required fields
        if "schema" not in stream:
            errors.append("Stream missing 'schema' field")
        
        # Validate JSON schema structure
        if "schema" in stream:
            schema_errors = self.validate_json_schema(stream["schema"])
            errors.extend(schema_errors)
        
        # Validate replication method and key consistency
        table_errors = self.validate_table_settings(stream)
        errors.extend(table_errors)
        
        # Validate required properties in schema
        if "schema" in stream:
            prop_errors = self.validate_required_properties(stream)
            errors.extend(prop_errors)
        
        # Validate metadata breadcrumbs
        if "metadata" in stream:
            metadata_errors = self.validate_metadata_breadcrumbs(stream)
            errors.extend(metadata_errors)
        
        # Warnings for missing optional but common fields
        if "tap_stream_id" not in stream and "stream" not in stream:
            warnings.append("Stream missing both 'tap_stream_id' and 'stream' fields")
        
        if "key_properties" not in stream:
            warnings.append("Stream missing 'key_properties' field")
        
        return ValidationResult(
            errors=errors,
            warnings=warnings,
            stream_name=stream_name
        )

    def validate_catalog(self) -> bool:
        """Validate the entire catalog file."""
        if not self.catalog_file.exists():
            print(f"Error: Catalog file '{self.catalog_file}' does not exist")
            return False
        
        print(f"Validating catalog: {self.catalog_file}\n")
        
        # Load catalog
        try:
            with open(self.catalog_file, 'r') as f:
                catalog = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in catalog: {e}")
            return False
        except Exception as e:
            print(f"Error: Could not read catalog file: {e}")
            return False
        
        # Validate catalog structure
        if not isinstance(catalog, dict):
            print("Error: Catalog must be a JSON object")
            return False
        
        if "streams" not in catalog:
            print("Error: Catalog missing 'streams' array")
            return False
        
        streams = catalog["streams"]
        if not isinstance(streams, list):
            print("Error: 'streams' must be an array")
            return False
        
        print(f"Found {len(streams)} streams in catalog\n")
        
        # Validate each stream
        for i, stream in enumerate(streams):
            result = self.validate_stream(stream)
            self.results.append(result)
            
            if result.has_issues():
                print(f"Stream {i + 1}: {result.stream_name}")
                if result.errors:
                    print("  ❌ ERRORS:")
                    for error in result.errors:
                        print(f"    • {error}")
                if result.warnings:
                    print("  ⚠️  WARNINGS:")
                    for warning in result.warnings:
                        print(f"    • {warning}")
                print()
        
        return all(r.is_valid() for r in self.results)

    def print_summary(self):
        """Print validation summary."""
        total_streams = len(self.results)
        valid_streams = sum(1 for r in self.results if r.is_valid())
        total_errors = sum(len(r.errors) for r in self.results)
        total_warnings = sum(len(r.warnings) for r in self.results)
        
        print("=" * 80)
        print("VALIDATION SUMMARY")
        print("=" * 80)
        print(f"Total streams:   {total_streams}")
        print(f"Valid streams:   {valid_streams}")
        print(f"Invalid streams: {total_streams - valid_streams}")
        print(f"Total errors:    {total_errors}")
        print(f"Total warnings:  {total_warnings}")
        print("=" * 80)
        
        if total_errors == 0:
            print("\n✅ All streams passed validation!\n")
        else:
            print(f"\n❌ {total_streams - valid_streams} stream(s) failed validation\n")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Validate Singer catalog using Menagerie service rules',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Validate a catalog file
  python validate_menagerie_catalog.py /tmp/catalog.json
  
  # Exit with error code if validation fails
  python validate_menagerie_catalog.py --strict /tmp/catalog.json

Validation Rules:
  1. Schema types must be valid JSON Schema types
  2. Replication method must match replication key requirements
  3. Key properties and replication key must exist in schema
  4. Metadata breadcrumbs must point to valid schema paths
  5. Inclusion values must be: automatic, available, or unsupported
        """
    )
    
    parser.add_argument(
        'catalog_file',
        help='Path to Singer catalog file to validate'
    )
    parser.add_argument(
        '--strict',
        action='store_true',
        help='Exit with error code 1 if validation fails (useful for CI/CD)'
    )
    
    args = parser.parse_args()
    
    validator = MenagerieValidator(args.catalog_file)
    success = validator.validate_catalog()
    validator.print_summary()
    
    if args.strict and not success:
        sys.exit(1)
    
    sys.exit(0)


if __name__ == '__main__':
    main()
