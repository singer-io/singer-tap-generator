"""Execution adapters: how a tap's streams are run concurrently without modifying the tap.

Only the process-isolation adapter exists today. It is the safe default because it needs no knowledge of
a tap's internals. Tap-specific behaviour (config overrides, independent child streams, batching,
rate-limit documentation, inspection findings) is declared as data in ``configs/tap_registry.json`` rather
than coded into the framework. A future adapter (for example an in-process thread adapter for a tap that
is proven thread-safe) can be added here and selected per tap with ``"adapter": "<name>"``.
"""
from __future__ import annotations

from typing import Dict, List, Type

from tap_registry import TapSpec


class ExecutionAdapter:
    """Contract for an adapter that executes work units for a tap."""
    name = "base"

    def guarantees(self) -> List[str]:
        """Isolation properties this adapter provides (shown in the report as technical validation)."""
        raise NotImplementedError

    def preflight(self, spec: TapSpec) -> List[str]:  # pylint: disable=unused-argument
        """Return problems that make this adapter unusable for the tap (empty list when fine)."""
        return []


class ProcessIsolationAdapter(ExecutionAdapter):
    """One OS process per work unit, running the tap's own console entry point with a trimmed catalog."""
    name = "process_isolation"

    def guarantees(self) -> List[str]:
        return [
            "Authentication, HTTP session and API client are created per process - nothing is shared between streams.",
            "Client-side rate-limit counters are per process (they are NOT shared; N workers can send N times the "
            "tap's single-process request rate - measured via 429/throttle counters).",
            "No state file is passed: every stream starts from start_date; bookmarks cannot be cross-written.",
            "Each work unit writes its own stdout/stderr/event files; outputs are merged afterwards in a controlled step.",
            "A parent stream and the child streams it emits are kept in one work unit (stream dependencies).",
        ]

    def preflight(self, spec: TapSpec) -> List[str]:
        return [] if spec.python_executable() else ["no virtualenv python found for process isolation"]


ADAPTERS: Dict[str, Type[ExecutionAdapter]] = {ProcessIsolationAdapter.name: ProcessIsolationAdapter}


def get_adapter(name: str) -> ExecutionAdapter:
    try:
        return ADAPTERS[name]()
    except KeyError as exc:
        raise ValueError("unknown adapter %r (available: %s)" % (name, ", ".join(sorted(ADAPTERS)))) from exc
