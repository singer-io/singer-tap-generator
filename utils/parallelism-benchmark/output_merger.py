"""Controlled merge of per-stream outputs into one valid Singer JSONL file."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from metrics import UnitResult


@dataclass
class MergeReport:
    path: str
    lines_written: int = 0
    records: int = 0
    schemas: int = 0
    schemas_deduplicated: int = 0
    invalid_lines: int = 0
    records_by_stream: Dict[str, int] = field(default_factory=dict)
    final_state: Dict[str, Any] = field(default_factory=dict)
    consistent: bool = True
    problems: List[str] = field(default_factory=list)


def _deep_merge(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def merge_unit_outputs(units: List[UnitResult], destination: str) -> MergeReport:
    """Concatenate whole unit outputs in schedule order, then append one combined STATE.

    Units are never interleaved, so every stream's SCHEMA still precedes its RECORDs. Each unit's
    own STATE messages are folded into a single final bookmark state. Every line is JSON-validated
    and the merged record count is checked against what the executor counted while streaming.
    """
    report = MergeReport(path=destination)
    state: Dict[str, Any] = {}
    last_schema: Dict[str, str] = {}
    with open(destination, "w", encoding="utf-8", newline="\n") as out:
        for unit in units:
            if not unit.output_path or not os.path.isfile(unit.output_path):
                continue
            with open(unit.output_path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                        kind = message["type"]
                    except (ValueError, KeyError, TypeError):
                        report.invalid_lines += 1
                        report.problems.append("%s: invalid JSON line dropped from merge" % unit.unit_id)
                        continue
                    if kind == "STATE":
                        value = message.get("value")
                        if isinstance(value, dict):
                            _deep_merge(state, value)
                        continue
                    if kind == "SCHEMA":
                        stream = message.get("stream", "?")
                        # Several units may re-emit an identical SCHEMA; keep the first, drop exact repeats.
                        if last_schema.get(stream) == line:
                            report.schemas_deduplicated += 1
                            continue
                        last_schema[stream] = line
                        report.schemas += 1
                    out.write(line if line.endswith("\n") else line + "\n")
                    report.lines_written += 1
                    if kind == "RECORD":
                        report.records += 1
                        stream = message.get("stream", "?")
                        report.records_by_stream[stream] = report.records_by_stream.get(stream, 0) + 1
        state.pop("currently_syncing", None)
        if state:
            out.write(json.dumps({"type": "STATE", "value": state}) + "\n")
            report.lines_written += 1
    report.final_state = state
    expected = sum(u.records for u in units)
    if expected != report.records:
        report.consistent = False
        report.problems.append("merged %d records but executor counted %d" % (report.records, expected))
    if report.invalid_lines:
        report.consistent = False
    return report
