"""Minimal Singer-style tap used by the framework's integration tests (never used for real benchmarks).

It reads ``--config`` (``base_url``) and ``--catalog``/``--properties``, requests each selected stream's pages
from a local test HTTP server using ``requests`` (so the benchmark's HTTP instrumentation is exercised) and
writes SCHEMA / RECORD / STATE messages to stdout. A stream whose catalog parent is selected is emitted by its parent.
"""
import argparse
import json
import logging
import sys
import time

import requests


def _root_meta(stream):
    for entry in stream.get("metadata", []):
        if entry.get("breadcrumb") == []:
            return entry.get("metadata", {})
    return {}


def _write(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _get(session, url):
    backoff_logger = logging.getLogger("backoff")     # same logger name the real `backoff` library uses
    backoff_logger.setLevel(logging.INFO)
    for attempt in range(1, 4):
        response = session.get(url, timeout=30)
        if response.status_code == 429:
            backoff_logger.info("Backing off get(...) for 0.0s (rate limited, attempt %d)", attempt)
            time.sleep(float(response.headers.get("Retry-After", "0")))
            continue
        if response.status_code >= 400:
            raise RuntimeError("HTTP-error-code: %d, Error: %s" % (response.status_code, response.text[:100]))
        return response.json()
    raise RuntimeError("HTTP-error-code: 429, Error: gave up after retries")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--catalog")
    parser.add_argument("--properties")
    parser.add_argument("--state")
    parser.add_argument("--discover", action="store_true")
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as handle:
        config = json.load(handle)
    with open(args.catalog or args.properties, encoding="utf-8") as handle:
        catalog = json.load(handle)
    streams = catalog["streams"]
    selected = {s["tap_stream_id"] for s in streams if _root_meta(s).get("selected")}
    session = requests.Session()
    for stream in streams:
        name = stream["tap_stream_id"]
        meta = _root_meta(stream)
        if name not in selected or meta.get("parent-tap-stream-id"):
            continue
        children = [s["tap_stream_id"] for s in streams
                    if _root_meta(s).get("parent-tap-stream-id") == name and s["tap_stream_id"] in selected]
        for emitted in [name] + children:
            _write({"type": "SCHEMA", "stream": emitted, "key_properties": ["id"],
                    "schema": {"type": "object", "properties": {"id": {"type": "integer"}}}})
        if name == "leaky":   # simulates a tap that logs a credential by mistake; the framework must scrub it
            sys.stderr.write("INFO debug config password=%s token %s\n" % (config.get("password"), config.get("api_token")))
        page = 0
        while True:
            data = _get(session, "%s/streams/%s?page=%d" % (config["base_url"], name, page))
            for record in data["records"]:
                _write({"type": "RECORD", "stream": name, "record": record})
                for child in children:
                    _write({"type": "RECORD", "stream": child, "record": record})
                time.sleep(0.002)   # per-record processing time of a real tap
            if not data["more"]:
                break
            page += 1
        _write({"type": "STATE", "value": {"bookmarks": {name: {"updated": "2026-01-01T00:00:00Z"}}}})


if __name__ == "__main__":
    main()
