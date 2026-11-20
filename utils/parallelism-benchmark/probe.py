"""Readiness probe: can the tap's own interpreter start the tap? (No network calls, no extraction.)

A tap whose virtualenv is missing, incomplete or still being installed must be reported as an
*environment problem*, never as an extraction failure or a parallelism verdict.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from catalog_utils import RUNNER_PATH
from tap_registry import TapSpec, parse_sync_command


@dataclass
class ProbeResult:
    ok: bool
    python: str = ""
    executable: str = ""
    requests_version: Optional[str] = None
    error: str = ""
    hint: str = ""

    def summary(self) -> str:
        if self.ok:
            return "OK (python %s, requests %s)" % (self.python, self.requests_version or "not installed")
        return "FAILED: %s%s" % (self.error, (" - " + self.hint) if self.hint else "")


def probe_environment(spec: TapSpec, timeout: int = 120) -> ProbeResult:
    """Load the tap's entry point with its own virtualenv Python and report what happened."""
    python = spec.python_executable()
    if python is None:
        return ProbeResult(False, error="no virtualenv python found under %s" % spec.repo_path,
                           hint="create <repo>/venv and run 'pip install -e .' inside it, or set python_path in the registry")
    try:
        script = parse_sync_command(spec.abs_sync_command).script
    except Exception as exc:  # pylint: disable=broad-except
        return ProbeResult(False, error="sync command could not be parsed: %s" % exc)
    cmd = [python, RUNNER_PATH, "--script", script]
    if spec.entry_point:
        cmd += ["--entry", spec.entry_point]
    cmd.append("--check")
    env = dict(os.environ, **spec.extra_env)
    env["PYTHONIOENCODING"] = "utf-8"
    hint = "install the tap into its virtualenv: \"%s\" -m pip install -e \"%s\"" % (python, spec.repo_path)
    try:
        proc = subprocess.run(cmd, cwd=spec.repo_path, env=env, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return ProbeResult(False, executable=python, error="environment probe timed out after %ss" % timeout)
    except OSError as exc:
        return ProbeResult(False, executable=python, error="cannot execute %s: %s" % (python, exc), hint=hint)
    report = {}
    for line in reversed(proc.stdout.decode("utf-8", "replace").splitlines()):
        try:
            report = json.loads(line)
            break
        except ValueError:
            continue
    if report.get("ok"):
        return ProbeResult(True, python=report.get("python", ""), executable=report.get("executable", python),
                           requests_version=report.get("requests"))
    error = report.get("error") or proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["probe failed"]
    if isinstance(error, list):
        error = error[0]
    return ProbeResult(False, python=report.get("python", ""), executable=report.get("executable", python),
                       requests_version=report.get("requests"), error=str(error)[:300], hint=hint)
