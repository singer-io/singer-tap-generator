"""Catalog loading, stream selection and work-unit planning (tap-agnostic)."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from config import FRAMEWORK_DIR, unique_path
from tap_registry import TapSpec, load_json_file, tokenize_command, RegistryError

RUNNER_PATH = os.path.join(FRAMEWORK_DIR, "worker_support", "worker_runner.py")


def root_metadata(stream: Dict[str, Any]) -> Dict[str, Any]:
    for entry in stream.get("metadata", []):
        if entry.get("breadcrumb") == []:
            return entry.get("metadata", {})
    return {}


def stream_id(stream: Dict[str, Any]) -> str:
    return stream.get("tap_stream_id") or stream.get("stream") or ""


def is_selected(stream: Dict[str, Any]) -> bool:
    meta = root_metadata(stream)
    if "selected" in meta:
        return bool(meta["selected"])
    return bool(stream.get("selected"))


def parent_of(stream: Dict[str, Any], parent_keys: Sequence[str]) -> Optional[str]:
    meta = root_metadata(stream)
    for key in parent_keys:
        if meta.get(key):
            return meta[key]
    return None


@dataclass
class WorkUnit:
    """One schedulable piece of work: a root stream (+ its children), or a batch of unlimited roots."""
    unit_id: str
    index: int
    roots: List[str]
    members: List[str]            # every catalog stream included in this unit's catalog
    selected: List[str]           # members selected in the source catalog
    limits: Dict[str, Optional[int]] = field(default_factory=dict)   # selected stream -> record limit
    catalog_path: str = ""

    @property
    def has_limits(self) -> bool:
        return any(v is not None for v in self.limits.values())


@dataclass
class StreamPlan:
    total_catalog_streams: int
    selected_streams: List[str]
    excluded_streams: List[str]       # present in catalog but not selected
    filtered_streams: List[str]       # selected but dropped by registry/CLI filters
    units: List[WorkUnit]
    limited: bool = False
    detached_streams: List[str] = field(default_factory=list)
    parents: Dict[str, Optional[str]] = field(default_factory=dict)

    @property
    def scheduled_selected(self) -> List[str]:
        return [s for unit in self.units for s in unit.selected]

    @property
    def dependency_families(self) -> List[WorkUnit]:
        """Units whose selected streams depend on a parent stream (must stay together)."""
        return [u for u in self.units if len(u.selected) > 1 and len(u.roots) == 1]


def plan_units(catalog: Dict[str, Any], spec: TapSpec, max_units: Optional[int] = None,
               limit_for: Optional[Callable[[str], Optional[int]]] = None) -> StreamPlan:
    """Split the catalog's selected streams into independent work units.

    * A child stream stays with its parent (its records are produced by the parent's extraction).
    * ``spec.independent_streams`` are scheduled alone even if the catalog names a parent.
    * Roots are batched ``spec.streams_per_task`` per process, but only when none of their streams has a
      record limit: a limit needs a dedicated process so the tap can be stopped the moment it is reached.
    """
    limit_for = limit_for or (lambda _stream: None)
    streams = catalog.get("streams", [])
    order = [stream_id(s) for s in streams]
    by_id = {stream_id(s): s for s in streams}
    detached = [s for s in spec.independent_streams if s in by_id]
    parents = {sid: (None if sid in detached else parent_of(by_id[sid], spec.parent_metadata_keys)) for sid in order}

    def root_of(sid: str) -> str:
        seen: Set[str] = set()
        current = sid
        while parents.get(current) and parents[current] in by_id and current not in seen:
            seen.add(current)
            current = parents[current]
        return current

    selected = [sid for sid in order if is_selected(by_id[sid])]
    selected_set = set(selected)
    excluded = [sid for sid in order if sid not in selected_set]

    families: Dict[str, List[str]] = {}
    for sid in order:
        families.setdefault(root_of(sid), []).append(sid)

    include = set(spec.stream_include)
    exclude = set(spec.stream_exclude)
    candidate_roots: List[str] = []
    filtered: List[str] = []
    for root in order:
        if root not in families:
            continue
        members = families[root]
        chosen = [m for m in members if m in selected_set]
        if not chosen:
            continue
        if (include and not (include & set(members))) or (exclude & set(members)):
            filtered.extend(chosen)
            continue
        candidate_roots.append(root)

    limited = False
    if max_units is not None and len(candidate_roots) > max_units:
        for root in candidate_roots[max_units:]:
            filtered.extend(m for m in families[root] if m in selected_set)
        candidate_roots = candidate_roots[:max_units]
        limited = True

    units: List[WorkUnit] = []

    def make_unit(roots: List[str]) -> WorkUnit:
        members = [m for r in roots for m in families[r]]
        chosen = [m for m in members if m in selected_set]
        unit_id = roots[0] if len(roots) == 1 else "batch_%03d_%s" % (len(units) + 1, roots[0])
        return WorkUnit(unit_id=unit_id, index=len(units), roots=roots, members=members, selected=chosen,
                        limits={m: limit_for(m) for m in chosen})

    batch_size = max(1, int(spec.streams_per_task))
    pending: List[str] = []
    for root in candidate_roots:
        if any(limit_for(m) is not None for m in families[root] if m in selected_set):
            if pending:
                units.append(make_unit(pending))
                pending = []
            units.append(make_unit([root]))
            continue
        pending.append(root)
        if len(pending) >= batch_size:
            units.append(make_unit(pending))
            pending = []
    if pending:
        units.append(make_unit(pending))

    return StreamPlan(
        total_catalog_streams=len(order), selected_streams=selected, excluded_streams=excluded,
        filtered_streams=filtered, units=units, limited=limited or bool(filtered),
        detached_streams=detached, parents=parents)


def write_working_catalog(catalog: Dict[str, Any], path: str) -> str:
    """Write a UTF-8 copy of the full catalog with the original selection untouched."""
    target = unique_path(path)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(catalog, handle)
    return target


def write_unit_catalogs(catalog: Dict[str, Any], units: List[WorkUnit], directory: str) -> None:
    """Write one trimmed catalog per unit containing only that unit's streams."""
    os.makedirs(directory, exist_ok=True)
    by_id = {stream_id(s): s for s in catalog.get("streams", [])}
    for unit in units:
        subset = {"streams": [by_id[m] for m in unit.members]}
        path = os.path.join(directory, "_catalog_%03d_%s.json" % (unit.index, _safe_name(unit.unit_id)))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(subset, handle)
        unit.catalog_path = path


def _safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)[:80]


def select_all_streams(catalog_path: str, script_path: Optional[str]) -> None:
    """Mark every stream selected, reusing the tap's own add_selected.py when available."""
    if script_path and os.path.isfile(script_path):
        spec = importlib.util.spec_from_file_location("_tap_add_selected", script_path)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        if hasattr(module, "add_selected_to_catalog"):
            module.add_selected_to_catalog(pathlib.Path(catalog_path))
            return
    catalog = load_json_file(catalog_path)
    for stream in catalog.get("streams", []):
        for entry in stream.get("metadata", []):
            if entry.get("breadcrumb") == []:
                entry.setdefault("metadata", {})["selected"] = True
    with open(catalog_path, "w", encoding="utf-8") as handle:
        json.dump(catalog, handle)


def run_discovery_into(spec: TapSpec, run_dir: str, timeout: int) -> str:
    """Run the tap's discovery command into a new ``_discovery_output.json`` and select all streams."""
    if not spec.discovery_command_path:
        raise RegistryError("catalog_strategy discover_if_missing needs discovery_command_path")
    command = tokenize_command(spec.repo_file(spec.discovery_command_path))
    python = spec.python_executable()
    target = unique_path(os.path.join(run_dir, "_discovery_output.json"))
    runner_cmd = [python, RUNNER_PATH, "--script", command[0]]
    if spec.entry_point:
        runner_cmd += ["--entry", spec.entry_point]
    env = dict(os.environ, **spec.extra_env)
    with open(target, "wb") as out:
        proc = subprocess.run(runner_cmd + ["--"] + command[1:], cwd=spec.repo_path, stdout=out,
                              stderr=subprocess.PIPE, timeout=timeout, check=False, env=env)
    if proc.returncode != 0:
        raise RegistryError("discovery failed with exit code %d" % proc.returncode)
    select_all_streams(target, spec.repo_file(spec.add_selected_script))
    return target
