"""Launcher executed *inside a tap's own virtualenv* for one benchmark work unit.

It runs the tap's unmodified console-script entry point, but first installs
non-invasive, process-local instrumentation:

* ``requests.Session.send`` is wrapped to record one JSON line per HTTP request
  (status, duration, rate-limit headers, redacted URL, short error body).
* A handler on the ``backoff`` logger records retry / give-up events.

Nothing on disk in the tap repository is touched. Only the standard library and
``requests`` (already a dependency of every tap) are imported.

Usage:
    python worker_runner.py --script tap-jira -- --config c.json --properties cat.json
    python worker_runner.py --script x --entry my_tap:main -- --config c.json   (module:function instead of a console script)

Environment:
    BENCH_EVENTS_FILE  JSONL file this process appends events to (required to record)
    BENCH_WORKER_ID    worker slot id, copied into events
    BENCH_UNIT         work-unit id, copied into events
    BENCH_LIMIT_RULE   optional JSON {"roots": [...], "limits": {stream: N}, "selected": [...]}: stop the process
                       the moment the driving stream reaches its record limit
"""
import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit, urlencode

_SENSITIVE_QUERY = re.compile(
    r"(pass|secret|token|key|auth|assertion|signature|credential|sig$|code$)", re.I
)
_RATE_HEADER = re.compile(r"(retry-after|ratelimit|rate-limit)", re.I)
_MAX_BODY = 300
_MAX_URL = 500

_events_lock = threading.Lock()
_events_fh = None
_context = {
    "worker_id": os.environ.get("BENCH_WORKER_ID"),
    "unit": os.environ.get("BENCH_UNIT"),
    "pid": os.getpid(),
}


def _emit(event):
    if _events_fh is None:
        return
    event.update(_context)
    line = json.dumps(event, default=str)
    with _events_lock:
        _events_fh.write(line + "\n")
        _events_fh.flush()


def _redact_url(url):
    try:
        parts = urlsplit(url)
        netloc = parts.hostname or ""
        if parts.port:
            netloc += ":%d" % parts.port
        query = [
            (k, "<redacted>" if _SENSITIVE_QUERY.search(k) else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        redacted = urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))
    except Exception:  # pylint: disable=broad-except
        redacted = "<unparseable-url>"
    return redacted[:_MAX_URL]


def _install_http_hook():
    import requests  # pylint: disable=import-outside-toplevel

    original_send = requests.Session.send
    guard = threading.local()

    def instrumented_send(self, request, **kwargs):
        # Redirect hops re-enter send(); only the outermost call is recorded.
        if getattr(guard, "active", False):
            return original_send(self, request, **kwargs)
        guard.active = True
        started = time.time()
        event = {
            "ev": "http",
            "ts": started,
            "method": request.method,
            "url": _redact_url(request.url),
        }
        try:
            response = original_send(self, request, **kwargs)
        except BaseException as exc:  # pylint: disable=broad-except
            event.update(
                status=None,
                dur=round(time.time() - started, 4),
                exc=type(exc).__name__,
                msg=str(exc)[:_MAX_BODY],
            )
            _emit(event)
            raise
        else:
            event.update(status=response.status_code, dur=round(time.time() - started, 4))
            headers = {
                k.lower(): str(v)[:100]
                for k, v in response.headers.items()
                if _RATE_HEADER.search(k)
            }
            if headers:
                event["rate_headers"] = headers
            if response.status_code >= 400:
                try:
                    if not kwargs.get("stream") or getattr(response, "_content", False) is not False:
                        event["body"] = response.text[:_MAX_BODY]
                except Exception:  # pylint: disable=broad-except
                    pass
            _emit(event)
            return response
        finally:
            guard.active = False

    requests.Session.send = instrumented_send


class _BackoffHandler(logging.Handler):
    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:  # pylint: disable=broad-except
            return
        if message.startswith("Backing off"):
            _emit({"ev": "retry", "ts": time.time(), "msg": message[:_MAX_BODY]})
        elif message.startswith("Giving up"):
            _emit({"ev": "giveup", "ts": time.time(), "msg": message[:_MAX_BODY]})


class _LimitWriter:
    """Wraps ``sys.stdout`` and stops the tap process the instant a record limit is reached.

    Counting happens inside the tap process, in the very ``write()`` call that emits the limit-th record, so the
    tap cannot go on to request another API page. (The parent executor performs the same accounting on the
    pipe and is the source of truth for reports; this wrapper only guarantees a prompt stop.)
    """
    _RECORD = re.compile(r'^\{"type": ?"RECORD", ?"stream": ?"((?:[^"\\]|\\.)*)"')

    def __init__(self, inner, rule):
        self._inner = inner
        self._roots = set(rule.get("roots", []))
        self._limits = rule.get("limits", {})
        self._selected = rule.get("selected", [])
        self._counts = {}
        self._reached = set()

    def write(self, data):
        written = self._inner.write(data)
        if data.startswith("{"):
            match = self._RECORD.match(data)
            if match:
                self._count(match.group(1))
        return written

    def _should_stop(self, stream):
        if stream in self._roots and len(self._roots) == 1:
            return True
        return len(self._roots) == 1 and all(
            self._limits.get(s) is not None and s in self._reached for s in self._selected)

    def _count(self, stream):
        limit = self._limits.get(stream)
        count = self._counts[stream] = self._counts.get(stream, 0) + 1
        if limit is None or count < limit:
            return
        self._reached.add(stream)
        if self._should_stop(stream):
            self._inner.flush()
            _emit({"ev": "limit_stop", "ts": time.time(), "stream": stream, "records": count, "limit": limit})
            os._exit(0)  # pylint: disable=protected-access

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _load_entry_point(script_name, entry_spec=None):
    if entry_spec:
        module_name, _, func_name = entry_spec.partition(":")
        import importlib  # pylint: disable=import-outside-toplevel
        return getattr(importlib.import_module(module_name), func_name or "main")
    try:
        from importlib.metadata import entry_points  # pylint: disable=import-outside-toplevel
    except ImportError:  # pragma: no cover
        from importlib_metadata import entry_points  # type: ignore
    eps = entry_points()
    if hasattr(eps, "select"):
        candidates = eps.select(group="console_scripts", name=script_name)
    else:  # Python < 3.10
        candidates = [e for e in eps.get("console_scripts", []) if e.name == script_name]
    for entry in candidates:
        return entry.load()
    raise SystemExit("worker_runner: console script %r not found in this environment" % script_name)


def main():
    global _events_fh  # pylint: disable=global-statement
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    parser.add_argument("--entry", default=None, help="optional module:function overriding console-script lookup")
    parser.add_argument("tap_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    tap_args = args.tap_args[1:] if args.tap_args[:1] == ["--"] else args.tap_args

    events_path = os.environ.get("BENCH_EVENTS_FILE")
    if events_path:
        _events_fh = open(events_path, "a", encoding="utf-8")  # pylint: disable=consider-using-with
        _install_http_hook()
        logging.getLogger("backoff").addHandler(_BackoffHandler())
        _emit({"ev": "process_start", "ts": time.time()})

    entry = _load_entry_point(args.script, args.entry)
    rule_text = os.environ.get("BENCH_LIMIT_RULE")
    if rule_text:
        sys.stdout = _LimitWriter(sys.stdout, json.loads(rule_text))
    sys.argv = [args.script] + tap_args
    exit_code = 0
    try:
        result = entry()
        if isinstance(result, int):
            exit_code = result
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        raise
    except BaseException:
        exit_code = 1
        raise
    finally:
        _emit({"ev": "process_end", "ts": time.time(), "exit_code": exit_code})
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
