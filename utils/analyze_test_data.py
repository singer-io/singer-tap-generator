#!/usr/bin/env python3
"""
Analyze Singer tap extraction logs and suggest test variables.

This script reads Singer output (SCHEMA and RECORD messages) and automatically identifies:
- MISSING_FIELDS: Fields in schema but not in extracted records
- streams_to_exclude: Streams with no data or insufficient records
- expected_page_size: Based on actual record counts
- start_date suggestions: Based on replication keys and timestamps
"""

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Set, List, Any, Optional


class TestDataAnalyzer:
    """Analyze Singer tap output to suggest test configuration variables."""
    
    def __init__(self, records_file: str, min_records_for_pagination: int = 20):
        self.records_file = records_file
        self.min_records_for_pagination = min_records_for_pagination
        
        # Data structures to collect information
        self.schemas: Dict[str, Dict] = {}
        self.record_counts: Dict[str, int] = defaultdict(int)
        self.fields_seen: Dict[str, Set[str]] = defaultdict(set)
        self.replication_keys: Dict[str, str] = {}
        self.timestamps: Dict[str, List[str]] = defaultdict(list)
        self.stream_date_fields: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
        
    def analyze(self) -> Dict[str, Any]:
        """Main analysis method."""
        print(f"Analyzing: {self.records_file}")
        self._parse_singer_output()
        
        return {
            'missing_fields': self._identify_missing_fields(),
            'streams_to_exclude': self._identify_streams_to_exclude(),
            'pagination_info': self._analyze_pagination(),
            'start_date_info': self._analyze_start_dates(),
            'summary': self._generate_summary()
        }
    
    def _parse_singer_output(self):
        """Parse Singer JSONL output and collect metadata."""
        try:
            with open(self.records_file, 'r') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    
                    try:
                        msg = json.loads(line)
                        msg_type = msg.get('type')
                        
                        if msg_type == 'SCHEMA':
                            self._process_schema(msg)
                        elif msg_type == 'RECORD':
                            self._process_record(msg)
                        elif msg_type == 'STATE':
                            pass  # Could analyze bookmarks here if needed
                            
                    except json.JSONDecodeError as e:
                        print(f"Warning: Skipping invalid JSON at line {line_num}: {e}")
                        continue
                        
        except FileNotFoundError:
            print(f"Error: File not found: {self.records_file}")
            sys.exit(1)
    
    def _process_schema(self, msg: Dict):
        """Process a SCHEMA message."""
        stream = msg.get('stream')
        if not stream:
            return
        
        schema = msg.get('schema', {})
        properties = schema.get('properties', {})
        
        self.schemas[stream] = schema
        
        # Extract replication key from key_properties or metadata
        key_properties = msg.get('key_properties', [])
        if key_properties:
            self.replication_keys[stream] = key_properties[0]
    
    def _process_record(self, msg: Dict):
        """Process a RECORD message."""
        stream = msg.get('stream')
        record = msg.get('record', {})
        
        if not stream:
            return
        
        self.record_counts[stream] += 1
        
        # Collect all field names seen in this record
        self.fields_seen[stream].update(self._get_all_field_names(record))
        
        # Collect timestamps for start_date analysis
        time_extraction_utc = msg.get('time_extracted')
        if time_extraction_utc:
            self.timestamps[stream].append(time_extraction_utc)
        
        # Collect date/timestamp fields from records
        self._collect_date_fields(stream, record)
    
    def _get_all_field_names(self, obj: Any, prefix: str = '') -> Set[str]:
        """Recursively extract all field names from a nested object."""
        fields = set()
        
        if isinstance(obj, dict):
            for key, value in obj.items():
                full_key = f"{prefix}.{key}" if prefix else key
                fields.add(full_key)
                
                # Recursively process nested objects
                if isinstance(value, dict):
                    fields.update(self._get_all_field_names(value, full_key))
                elif isinstance(value, list) and value and isinstance(value[0], dict):
                    # For arrays of objects, analyze first element
                    fields.update(self._get_all_field_names(value[0], full_key))
        
        return fields
    
    def _collect_date_fields(self, stream: str, obj: Any, prefix: str = ''):
        """Collect date/timestamp values from records for start_date analysis."""
        # Common date field names
        date_field_patterns = [
            'created_at', 'updated_at', 'modified_at', 'date', 'timestamp',
            'created', 'updated', 'modified', 'time', 'datetime', 'created_date',
            'updated_date', 'last_modified', 'start_date', 'end_date'
        ]
        
        if isinstance(obj, dict):
            for key, value in obj.items():
                full_key = f"{prefix}.{key}" if prefix else key
                
                # Check if this looks like a date field
                if any(pattern in key.lower() for pattern in date_field_patterns):
                    if isinstance(value, str) and value:
                        # Try to parse as ISO datetime
                        if self._is_iso_datetime(value):
                            self.stream_date_fields[stream][full_key].append(value)
                
                # Recursively process nested objects
                if isinstance(value, dict):
                    self._collect_date_fields(stream, value, full_key)
    
    def _is_iso_datetime(self, value: str) -> bool:
        """Check if a string looks like an ISO datetime."""
        if not value or len(value) < 10:
            return False
        
        # Quick check for ISO-like format
        try:
            # Try parsing common formats
            if 'T' in value or ' ' in value:
                from datetime import datetime
                # Try ISO format with Z
                if value.endswith('Z'):
                    datetime.fromisoformat(value.replace('Z', '+00:00'))
                    return True
                try:
                    # Try ISO format
                    datetime.fromisoformat(value.replace(' ', 'T').split('+')[0])
                    return True
                except ValueError:
                    return False
        except (ValueError, AttributeError):
            pass
        
        return False
    
    def _get_schema_field_names(self, schema: Dict, prefix: str = '') -> Set[str]:
        """Extract all field names defined in schema."""
        fields = set()
        properties = schema.get('properties', {})
        
        for key, value in properties.items():
            full_key = f"{prefix}.{key}" if prefix else key
            fields.add(full_key)
            
            # Handle nested objects
            if isinstance(value, dict):
                if value.get('type') == 'object' or 'properties' in value:
                    fields.update(self._get_schema_field_names(value, full_key))
                elif value.get('type') == 'array':
                    items = value.get('items', {})
                    if isinstance(items, dict) and items.get('type') == 'object':
                        fields.update(self._get_schema_field_names(items, full_key))
        
        return fields
    
    def _identify_missing_fields(self) -> Dict[str, Set[str]]:
        """Identify fields defined in schema but not present in records."""
        missing_fields = {}
        
        for stream, schema in self.schemas.items():
            if stream not in self.record_counts or self.record_counts[stream] == 0:
                # Skip streams with no records
                continue
            
            schema_fields = self._get_schema_field_names(schema)
            seen_fields = self.fields_seen.get(stream, set())
            
            # Find fields in schema but not in actual records
            missing = schema_fields - seen_fields
            
            if missing:
                missing_fields[stream] = missing
        
        return missing_fields
    
    def _identify_streams_to_exclude(self) -> Dict[str, Set[str]]:
        """Identify streams that should be excluded from different tests."""
        exclusions = {
            'all_fields': set(),
            'automatic_fields': set(),
            'pagination': set(),
        }
        
        for stream, schema in self.schemas.items():
            record_count = self.record_counts.get(stream, 0)
            
            # Streams with no records should be excluded from all tests
            if record_count == 0:
                exclusions['all_fields'].add(stream)
                exclusions['automatic_fields'].add(stream)
                exclusions['pagination'].add(stream)
            
            # Streams with insufficient records for pagination
            elif record_count < self.min_records_for_pagination:
                exclusions['pagination'].add(stream)
        
        return exclusions
    
    def _analyze_pagination(self) -> Dict[str, Any]:
        """Analyze pagination requirements per stream."""
        pagination_info = {
            'streams_with_enough_data': [],
            'suggested_page_size': 10,  # Conservative default
            'record_counts': {},
            'per_stream_page_size': {}
        }
        
        for stream, count in sorted(self.record_counts.items(), key=lambda x: x[1], reverse=True):
            pagination_info['record_counts'][stream] = count
            
            if count >= self.min_records_for_pagination:
                pagination_info['streams_with_enough_data'].append(stream)
                # Calculate optimal page size for this stream (half to ensure 2+ pages)
                suggested = max(5, count // 2)
                # Cap at reasonable maximum
                suggested = min(suggested, 100)
                pagination_info['per_stream_page_size'][stream] = {
                    'record_count': count,
                    'suggested_page_size': suggested,
                    'min_pages': count // suggested if suggested > 0 else 0
                }
        
        # Suggest global page size based on minimum record count of testable streams
        if pagination_info['streams_with_enough_data']:
            min_count = min(
                self.record_counts[s] 
                for s in pagination_info['streams_with_enough_data']
            )
            # Suggest page size as half of minimum to ensure at least 2 pages
            pagination_info['suggested_page_size'] = max(5, min_count // 2)
        
        return pagination_info
    
    def _analyze_start_dates(self) -> Dict[str, Any]:
        """Analyze and suggest start_date values per stream."""
        start_date_info = {
            'suggested_start_date_1': None,
            'suggested_start_date_2': None,
            'suggested_start_date_3': None,
            'per_stream_dates': {},
            'replication_keys': self.replication_keys,
            'note': 'Dates based on actual data timestamps per stream - 3 dates for comprehensive testing'
        }
        
        from datetime import datetime, timedelta
        
        # Analyze per-stream date ranges
        for stream, date_fields in self.stream_date_fields.items():
            if not date_fields:
                continue
            
            # Collect all timestamps from all date fields for this stream
            all_dates = []
            for field_name, values in date_fields.items():
                for value in values:
                    try:
                        if value.endswith('Z'):
                            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                        else:
                            dt = datetime.fromisoformat(value)
                        all_dates.append(dt)
                    except:
                        continue
            
            if all_dates:
                all_dates.sort()
                earliest = all_dates[0]
                latest = all_dates[-1]
                
                # Suggest 3 dates for comprehensive testing:
                # 1. Before all data (should sync all records)
                # 2. Middle of data range (should sync partial records)
                # 3. Near end of data (should sync minimal/recent records)
                days_span = (latest - earliest).days
                
                if days_span > 30:
                    # If data spans more than 30 days, use dates within and around the range
                    start_date_1 = (earliest - timedelta(days=5)).strftime("%Y-%m-%dT00:00:00Z")
                    start_date_2 = (earliest + timedelta(days=days_span // 3)).strftime("%Y-%m-%dT00:00:00Z")
                    start_date_3 = (earliest + timedelta(days=2 * days_span // 3)).strftime("%Y-%m-%dT00:00:00Z")
                elif days_span > 5:
                    # Medium span: use before earliest, midpoint, and near latest
                    start_date_1 = (earliest - timedelta(days=10)).strftime("%Y-%m-%dT00:00:00Z")
                    start_date_2 = (earliest + timedelta(days=days_span // 2)).strftime("%Y-%m-%dT00:00:00Z")
                    start_date_3 = (latest - timedelta(days=max(1, days_span // 4))).strftime("%Y-%m-%dT00:00:00Z")
                else:
                    # Short span: use before earliest with different offsets
                    start_date_1 = (earliest - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
                    start_date_2 = (earliest - timedelta(days=10)).strftime("%Y-%m-%dT00:00:00Z")
                    start_date_3 = (earliest - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
                
                start_date_info['per_stream_dates'][stream] = {
                    'earliest_record': earliest.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    'latest_record': latest.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    'suggested_start_date_1': start_date_1,
                    'suggested_start_date_2': start_date_2,
                    'suggested_start_date_3': start_date_3,
                    'record_count': len(all_dates),
                    'date_fields': list(date_fields.keys())
                }
        
        # Get earliest extraction time if available (fallback)
        all_timestamps = []
        for timestamps in self.timestamps.values():
            all_timestamps.extend(timestamps)
        
        if all_timestamps:
            all_timestamps.sort()
            earliest = all_timestamps[0]
            
            try:
                dt = datetime.fromisoformat(earliest.replace('Z', '+00:00'))
                start_date_1 = (dt - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
                start_date_2 = (dt - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
                start_date_3 = (dt - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
                
                start_date_info['suggested_start_date_1'] = start_date_1
                start_date_info['suggested_start_date_2'] = start_date_2
                start_date_info['suggested_start_date_3'] = start_date_3
            except Exception:
                pass
        
        # Fallback to reasonable defaults if nothing else worked
        if not start_date_info['suggested_start_date_1'] and not start_date_info['per_stream_dates']:
            now = datetime.utcnow()
            start_date_info['suggested_start_date_1'] = (now - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
            start_date_info['suggested_start_date_2'] = (now - timedelta(days=15)).strftime("%Y-%m-%dT00:00:00Z")
            start_date_info['suggested_start_date_3'] = (now - timedelta(days=5)).strftime("%Y-%m-%dT00:00:00Z")
            start_date_info['note'] = 'Dates based on current date minus offset (no timestamps found in data)'
        
        return start_date_info
    
    def _generate_summary(self) -> Dict[str, Any]:
        """Generate overall summary statistics."""
        return {
            'total_streams': len(self.schemas),
            'streams_with_records': len([s for s, c in self.record_counts.items() if c > 0]),
            'empty_streams': len([s for s in self.schemas if self.record_counts.get(s, 0) == 0]),
            'total_records': sum(self.record_counts.values()),
            'record_counts_by_stream': dict(sorted(
                self.record_counts.items(), 
                key=lambda x: x[1], 
                reverse=True
            ))
        }
    
    def generate_test_code(self, results: Dict[str, Any]) -> str:
        """Generate Python code snippets for test configuration."""
        code_parts = []
        
        # MISSING_FIELDS for all_fields test
        if results['missing_fields']:
            code_parts.append("# For test_all_fields.py")
            code_parts.append("MISSING_FIELDS = {")
            for stream, fields in sorted(results['missing_fields'].items()):
                fields_str = ", ".join(f"'{f}'" for f in sorted(fields))
                code_parts.append(f"    '{stream}': {{{fields_str}}},")
            code_parts.append("}")
            code_parts.append("")
        
        # streams_to_exclude for all_fields
        exclusions = results['streams_to_exclude']
        if exclusions['all_fields']:
            code_parts.append("# For test_all_fields.py - streams_to_exclude")
            code_parts.append("streams_to_exclude = {")
            for stream in sorted(exclusions['all_fields']):
                code_parts.append(f"    '{stream}',")
            code_parts.append("}")
            code_parts.append("")
        
        # streams_to_exclude for automatic_fields
        if exclusions['automatic_fields']:
            code_parts.append("# For test_automatic_fields.py - streams_to_exclude")
            code_parts.append("streams_to_exclude = {")
            for stream in sorted(exclusions['automatic_fields']):
                code_parts.append(f"    '{stream}',")
            code_parts.append("}")
            code_parts.append("")
        
        # Pagination configuration
        pagination = results['pagination_info']
        if exclusions['pagination']:
            code_parts.append("# For test_pagination.py - streams_to_exclude")
            code_parts.append("streams_to_exclude = {")
            for stream in sorted(exclusions['pagination']):
                code_parts.append(f"    '{stream}',")
            code_parts.append("}")
            code_parts.append("")
        
        code_parts.append("# For test_pagination.py - expected_page_size")
        code_parts.append("def expected_page_size(self, stream):")
        code_parts.append(f"    # Global default: {pagination['suggested_page_size']}")
        
        # Add per-stream page sizes if available
        if pagination.get('per_stream_page_size'):
            code_parts.append("    # Per-stream optimal sizes:")
            code_parts.append("    stream_page_sizes = {")
            for stream, info in sorted(pagination['per_stream_page_size'].items()):
                code_parts.append(f"        '{stream}': {info['suggested_page_size']},  # {info['record_count']} records")
            code_parts.append("    }")
            code_parts.append("    return stream_page_sizes.get(stream, " + str(pagination['suggested_page_size']) + ")")
        else:
            code_parts.append(f"    return {pagination['suggested_page_size']}")
        
        code_parts.append("")
        
        # Start dates
        start_date_info = results['start_date_info']
        code_parts.append("# For test_start_date.py")
        
        # If we have per-stream dates, provide a method to use them
        if start_date_info.get('per_stream_dates'):
            code_parts.append("# Per-stream date ranges (based on actual data):")
            code_parts.append("PER_STREAM_START_DATES = {")
            for stream, info in sorted(start_date_info['per_stream_dates'].items()):
                code_parts.append(f"    '{stream}': {{")
                code_parts.append(f"        'start_date_1': '{info['suggested_start_date_1']}',  # Before all data")
                code_parts.append(f"        'start_date_2': '{info['suggested_start_date_2']}',  # Mid-range")
                code_parts.append(f"        'start_date_3': '{info['suggested_start_date_3']}',  # Near end")
                code_parts.append(f"        # Data range: {info['earliest_record']} to {info['latest_record']}")
                code_parts.append(f"    }},")
            code_parts.append("}")
            code_parts.append("")
        
        # Global dates as properties
        if start_date_info.get('suggested_start_date_1'):
            code_parts.append("# Global dates (fallback):")
        code_parts.append("@property")
        code_parts.append("def start_date_1(self):")
        code_parts.append(f"    return \"{start_date_info.get('suggested_start_date_1', 'NOT_SET')}\"")
        code_parts.append("")
        code_parts.append("@property")
        code_parts.append("def start_date_2(self):")
        code_parts.append(f"    return \"{start_date_info.get('suggested_start_date_2', 'NOT_SET')}\"")
        code_parts.append("")
        code_parts.append("@property")
        code_parts.append("def start_date_3(self):")
        code_parts.append(f"    return \"{start_date_info.get('suggested_start_date_3', 'NOT_SET')}\"")
        
        return "\n".join(code_parts)


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Analyze Singer tap extraction logs to suggest test variables',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze records.jsonl and output suggestions
  python analyze_test_data.py records.jsonl
  
  # Specify custom minimum records for pagination test
  python analyze_test_data.py records.jsonl --min-pagination-records 50
  
  # Output as JSON for programmatic use
  python analyze_test_data.py records.jsonl --format json
        """
    )
    
    parser.add_argument(
        'records_file',
        help='Path to Singer output file (JSONL format with SCHEMA and RECORD messages)'
    )
    parser.add_argument(
        '--min-pagination-records',
        type=int,
        default=20,
        help='Minimum records needed to test pagination (default: 20)'
    )
    parser.add_argument(
        '--format',
        choices=['text', 'json', 'code'],
        default='text',
        help='Output format (default: text)'
    )
    parser.add_argument(
        '--output',
        '-o',
        help='Output file (default: stdout)'
    )
    
    args = parser.parse_args()
    
    # Analyze the data
    analyzer = TestDataAnalyzer(args.records_file, args.min_pagination_records)
    results = analyzer.analyze()
    
    # Generate output
    if args.format == 'json':
        output = json.dumps(results, indent=2, default=str)
    elif args.format == 'code':
        output = analyzer.generate_test_code(results)
    else:  # text
        output = format_text_output(results, analyzer)
    
    # Write output
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        print(f"Results written to: {args.output}")
    else:
        print(output)


def format_text_output(results: Dict[str, Any], analyzer: TestDataAnalyzer) -> str:
    """Format results as human-readable text."""
    lines = []
    
    lines.append("=" * 80)
    lines.append("SINGER TAP TEST DATA ANALYSIS")
    lines.append("=" * 80)
    lines.append("")
    
    # Summary
    summary = results['summary']
    lines.append("SUMMARY")
    lines.append("-" * 80)
    lines.append(f"Total streams:           {summary['total_streams']}")
    lines.append(f"Streams with records:    {summary['streams_with_records']}")
    lines.append(f"Empty streams:           {summary['empty_streams']}")
    lines.append(f"Total records extracted: {summary['total_records']}")
    lines.append("")
    
    # Record counts by stream
    lines.append("RECORD COUNTS BY STREAM")
    lines.append("-" * 80)
    for stream, count in summary['record_counts_by_stream'].items():
        lines.append(f"  {stream:40} {count:>8} records")
    lines.append("")
    
    # Missing fields
    if results['missing_fields']:
        lines.append("MISSING FIELDS (in schema but not in records)")
        lines.append("-" * 80)
        for stream, fields in sorted(results['missing_fields'].items()):
            lines.append(f"  {stream}:")
            for field in sorted(fields):
                lines.append(f"    - {field}")
        lines.append("")
    else:
        lines.append("MISSING FIELDS: None (all schema fields present in records)")
        lines.append("")
    
    # Streams to exclude
    exclusions = results['streams_to_exclude']
    lines.append("STREAMS TO EXCLUDE FROM TESTS")
    lines.append("-" * 80)
    lines.append(f"  All Fields Test:     {sorted(exclusions['all_fields']) if exclusions['all_fields'] else 'None'}")
    lines.append(f"  Automatic Fields:    {sorted(exclusions['automatic_fields']) if exclusions['automatic_fields'] else 'None'}")
    lines.append(f"  Pagination Test:     {sorted(exclusions['pagination']) if exclusions['pagination'] else 'None'}")
    lines.append("")
    
    # Pagination info
    pagination = results['pagination_info']
    lines.append("PAGINATION TEST CONFIGURATION")
    lines.append("-" * 80)
    lines.append(f"  Global suggested page size: {pagination['suggested_page_size']}")
    lines.append(f"  Streams with sufficient data (>= {analyzer.min_records_for_pagination} records):")
    if pagination['streams_with_enough_data']:
        for stream in pagination['streams_with_enough_data']:
            count = pagination['record_counts'][stream]
            lines.append(f"    - {stream} ({count} records)")
    else:
        lines.append("    None")
    lines.append("")
    
    if pagination.get('per_stream_page_size'):
        lines.append("  Per-Stream Pagination Settings:")
        lines.append("")
        for stream, info in sorted(pagination['per_stream_page_size'].items(), 
                                   key=lambda x: x[1]['record_count'], reverse=True):
            lines.append(f"    {stream}:")
            lines.append(f"      Records:         {info['record_count']}")
            lines.append(f"      Page size:       {info['suggested_page_size']}")
            lines.append(f"      Expected pages:  ~{info['min_pages']}")
        lines.append("")
    
    # Start dates
    start_date_info = results['start_date_info']
    lines.append("START DATE TEST CONFIGURATION")
    lines.append("-" * 80)
    
    if start_date_info.get('per_stream_dates'):
        lines.append("Per-Stream Date Analysis:")
        lines.append("")
        for stream, info in sorted(start_date_info['per_stream_dates'].items()):
            lines.append(f"  {stream}:")
            lines.append(f"    Date fields found: {', '.join(info['date_fields'])}")
            lines.append(f"    Earliest record:   {info['earliest_record']}")
            lines.append(f"    Latest record:     {info['latest_record']}")
            lines.append(f"    Suggested dates:")
            lines.append(f"      start_date_1:    {info['suggested_start_date_1']}")
            lines.append(f"      start_date_2:    {info['suggested_start_date_2']}")
            lines.append("")
    
    if start_date_info.get('suggested_start_date_1'):
        lines.append("Global Suggested Dates (fallback):")
        lines.append(f"  start_date_1: {start_date_info['suggested_start_date_1']}")
        lines.append(f"  start_date_2: {start_date_info['suggested_start_date_2']}")
    
    lines.append(f"  Note: {start_date_info['note']}")
    lines.append("")
    
    # Generated code
    lines.append("=" * 80)
    lines.append("GENERATED TEST CODE")
    lines.append("=" * 80)
    lines.append("")
    lines.append(analyzer.generate_test_code(results))
    
    return "\n".join(lines)


if __name__ == '__main__':
    main()
