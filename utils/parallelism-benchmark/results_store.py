"""Timestamped results folders and a portable ``latest.json`` pointer (no symlinks needed)."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from config import utc_iso

LATEST_FILE = "latest.json"
REPORT_CSV = "parallelism_report.csv"
RUN_MARKER = "execution_summary.json"


def create_run_dir(results_dir: str, run_id: str) -> str:
    """Create ``results/<run_id>/`` (never reuses an existing folder, so older results are never touched)."""
    os.makedirs(results_dir, exist_ok=True)
    path = os.path.join(results_dir, run_id)
    os.makedirs(path)          # raises FileExistsError rather than overwrite
    return path


def write_latest(results_dir: str, run_id: str, status: str) -> str:
    """Point ``results/latest.json`` at a run. Older run folders are left untouched."""
    path = os.path.join(results_dir, LATEST_FILE)
    payload = {"run_id": run_id, "path": run_id, "summary": "%s/%s" % (run_id, RUN_MARKER),
               "status": status, "updated": utc_iso()}
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temp, path)
    return path


def read_latest(results_dir: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(results_dir, LATEST_FILE)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def list_runs(results_dir: str) -> List[str]:
    """Run ids (oldest first) that contain an execution summary."""
    if not os.path.isdir(results_dir):
        return []
    return sorted(name for name in os.listdir(results_dir)
                  if os.path.isfile(os.path.join(results_dir, name, RUN_MARKER)))


def resolve_run(results_dir: str, ref: str) -> str:
    """Resolve a run reference: a run id, ``latest`` or a path to a run folder."""
    if ref == "latest":
        latest = read_latest(results_dir)
        if not latest:
            raise FileNotFoundError("no latest.json in %s" % results_dir)
        ref = latest["run_id"]
    candidate = ref if os.path.isdir(ref) else os.path.join(results_dir, ref)
    if not os.path.isfile(os.path.join(candidate, RUN_MARKER)):
        raise FileNotFoundError("not a benchmark run folder: %s" % candidate)
    return candidate
