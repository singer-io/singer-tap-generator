import csv
import json
import os
from collections import defaultdict
from datetime import datetime
import sys
from typing import Dict, List, Tuple

def process_splunk_logs(file_path: str) -> None:
    logger_files: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    all_logs: List[Tuple[str, str, str]] = []  # (timestamp, logger_name, formatted_entry)
    rows_processed = 0
    rows_with_logs = 0

    with open(file_path, mode='r', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile, delimiter=',')

        for row in reader:
            rows_processed += 1

            # Parse the JSON from _raw column
            raw_data = row.get('_raw', '')
            if not raw_data:
                continue

            try:
                log_data = json.loads(raw_data)
                timestamp = log_data.get('timestamp')
                message = log_data.get('message')
                logger_name = log_data.get('loggerName')

                if timestamp and message and logger_name:
                    # Store entries for individual logger files
                    log_entry = f'[{timestamp}] {message}'
                    logger_files[logger_name].append((timestamp, log_entry))

                    # Store for combined file with logger name
                    combined_entry = f'[{timestamp}] [{logger_name}] {message}'
                    all_logs.append((timestamp, logger_name, combined_entry))

                    rows_with_logs += 1
            except json.JSONDecodeError:
                # Skip rows that don't have valid JSON
                continue

    print(f'Processed {rows_processed} rows, found {rows_with_logs} log entries')
    print(f'Found {len(logger_files)} unique loggers\n')

    # Write individual logger files
    for logger_name, log_entries in logger_files.items():
        # Sort by timestamp (chronological order - oldest first)
        log_entries.sort(key=lambda x: x[0])
        sorted_entries = [entry[1] for entry in log_entries]

        filename = f'{logger_name}.log'
        print(f'Writing {len(sorted_entries)} logs for {logger_name} to {filename}')
        with open(filename, mode='w', encoding='utf-8') as log_file:
            log_file.write('\n'.join(sorted_entries))

    # Write combined log file with all entries sorted by timestamp
    if all_logs:
        all_logs.sort(key=lambda x: x[0])
        sorted_all_entries = [entry[2] for entry in all_logs]

        print(f'\nWriting {len(sorted_all_entries)} total logs to all.log')
        with open('all.log', mode='w', encoding='utf-8') as log_file:
            log_file.write('\n'.join(sorted_all_entries))

if __name__ == "__main__":
    process_splunk_logs(sys.argv[1])
