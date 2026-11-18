"""Error classification, throttle detection and secret scrubbing."""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional

from config import utc_iso

RATE_LIMIT = "RATE_LIMIT"
THROTTLING = "THROTTLING"
AUTH = "AUTH"
NETWORK = "NETWORK"
SERVER_ERROR = "SERVER_ERROR"
TIMEOUT = "TIMEOUT"
CLIENT_ERROR = "CLIENT_ERROR"
EXTRACTION_ERROR = "EXTRACTION_ERROR"
UNKNOWN = "UNKNOWN"

TRACKED_STATUSES = (400, 401, 403, 404, 408, 409, 429, 500, 502, 503, 504)

THROTTLE_TEXT = re.compile(
    r"(rate[\s_-]?limit|too many requests|throttl|quota (?:has been )?exceeded|request limit|retry[\s_-]?after)", re.I)
_TIMEOUT_EXC = re.compile(r"timeout|timed out", re.I)
_NETWORK_EXC = re.compile(
    r"(connection|chunkedencoding|ssl|proxy|name or service|getaddrinfo|reset by peer|broken pipe|remotedisconnected)", re.I)
_STATUS_IN_TEXT = re.compile(
    r"(?:http[\s_-]*(?:error[\s_-]*)?code|status(?:[\s_-]*code)?|error code)\W{0,3}(\d{3})\b|\b(\d{3}) (?:client|server) error", re.I)
_AUTH_TEXT = re.compile(r"(unauthori[sz]ed|invalid[_ ]client|invalid credentials|authentication failed|invalid_grant|access denied)", re.I)

_GENERIC_SECRET_PATTERNS = [
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*)(bearer|basic)?\s*[^\s,'\"}]+"), r"\1<redacted>"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <redacted>"),
    (re.compile(r"(?i)((?:password|passwd|client_secret|access_token|refresh_token|assertion|private_key|api[_-]?key)['\"]?\s*[:=]\s*)['\"]?[^\s,'\"&}]+"),
     r"\1<redacted>"),
]


class Scrubber:
    """Removes known secret values and common credential patterns from text."""

    def __init__(self, secrets: Iterable[str] = ()):
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        if not text:
            return text
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "<redacted>")
        for pattern, replacement in _GENERIC_SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text


@dataclass
class ErrorRecord:
    tap_name: str
    stream_name: str
    worker_id: Optional[int]
    worker_count: int
    timestamp: str
    category: str
    http_status: Optional[int]
    message: str
    retry_count: int
    stream_eventually_succeeded: bool
    source: str = "http"
    url: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


_REMAINING_ZERO = re.compile(r"\b(?:r|remaining)\s*=\s*0\b")


def rate_headers_signal_throttle(headers: Optional[Dict[str, str]], status: Optional[int] = None) -> bool:
    """True when rate-limit headers say "slow down".

    ``Retry-After`` only counts on an error response (>= 400): some APIs (e.g. ActiveCampaign) send an
    informational ``Retry-After: 1`` on every 200 response, which is not throttling.
    """
    if not headers:
        return False
    for name, value in headers.items():
        text = str(value).strip()
        if "retry-after" in name and status is not None and status >= 400:
            return True
        if "remaining" in name and text == "0":
            return True
        if name == "ratelimit" and _REMAINING_ZERO.search(text):
            return True
    return False


def is_near_limit(event: Dict[str, Any]) -> bool:
    """Early warning only (e.g. Jira's X-RateLimit-NearLimit: true); not counted as throttling."""
    return any("nearlimit" in name and str(value).strip().lower() == "true"
               for name, value in (event.get("rate_headers") or {}).items())


def is_throttle_signal(event: Dict[str, Any]) -> bool:
    """A response that says "slow down": 429, Retry-After, or an exhausted X-RateLimit-Remaining."""
    return event.get("status") == 429 or rate_headers_signal_throttle(event.get("rate_headers"), event.get("status"))


def classify_http(status: Optional[int], exc_name: Optional[str] = None, body: str = "",
                  headers: Optional[Dict[str, str]] = None) -> str:
    if exc_name:
        if _TIMEOUT_EXC.search(exc_name):
            return TIMEOUT
        if _NETWORK_EXC.search(exc_name):
            return NETWORK
        return UNKNOWN
    if status == 429:
        return RATE_LIMIT
    if status is not None and (rate_headers_signal_throttle(headers, status) or (
            status >= 400 and THROTTLE_TEXT.search(body or ""))):
        return THROTTLING
    if status in (401, 403):
        return AUTH
    if status in (408, 504):
        return TIMEOUT
    if status is not None and status >= 500:
        return SERVER_ERROR
    if status is not None and status >= 400:
        return CLIENT_ERROR
    return UNKNOWN


def classify_text(text: str) -> Optional[str]:
    """Classify a free-text failure message; None when nothing specific is recognised."""
    if not text:
        return None
    match = _STATUS_IN_TEXT.search(text)
    if match:
        status = int(match.group(1) or match.group(2))
        return classify_http(status, body=text)
    if THROTTLE_TEXT.search(text):
        return THROTTLING
    if _AUTH_TEXT.search(text):
        return AUTH
    if _TIMEOUT_EXC.search(text):
        return TIMEOUT
    if _NETWORK_EXC.search(text):
        return NETWORK
    return None


class StderrScanner:
    """Incrementally inspects a tap's stderr for errors and throttle mentions."""

    def __init__(self) -> None:
        self.lines = 0
        self.throttle_mentions = 0
        self.retry_mentions = 0
        self.error_lines: List[str] = []
        self.last_error_line = ""
        self.last_exception_line = ""
        self._in_traceback = False

    def feed(self, line: str) -> None:
        self.lines += 1
        stripped = line.rstrip("\r\n")
        if "METRIC:" not in stripped and THROTTLE_TEXT.search(stripped):
            self.throttle_mentions += 1
        if "Backing off" in stripped:
            self.retry_mentions += 1
        if stripped.startswith(("CRITICAL", "ERROR", "FATAL")):
            self.last_error_line = stripped[:500]
            if len(self.error_lines) < 50:
                self.error_lines.append(self.last_error_line)
        if stripped.startswith("Traceback"):
            self._in_traceback = True
        elif self._in_traceback and stripped and not stripped.startswith((" ", "\t")):
            self.last_exception_line = stripped[:500]
            self._in_traceback = False

    @property
    def final_error_text(self) -> str:
        return self.last_exception_line or self.last_error_line


def build_unit_errors(tap: str, unit_id: str, worker_id: Optional[int], worker_count: int,
                      events: List[Dict[str, Any]], succeeded: bool, retry_count: int,
                      scanner: StderrScanner, exit_code: Optional[int], timed_out: bool,
                      scrub, end_ts: float) -> List[ErrorRecord]:
    """Turn raw HTTP events plus process outcome into ErrorRecord rows."""
    records: List[ErrorRecord] = []
    last_http_error: Optional[ErrorRecord] = None
    for event in events:
        if event.get("ev") != "http":
            continue
        status = event.get("status")
        exc_name = event.get("exc")
        throttle = is_throttle_signal(event)
        if not exc_name and not (status is not None and status >= 400) and not throttle:
            continue
        category = classify_http(status, exc_name, event.get("body", ""), event.get("rate_headers"))
        message = event.get("msg") or event.get("body") or ""
        if exc_name:
            message = "%s: %s" % (exc_name, message)
        record = ErrorRecord(
            tap_name=tap, stream_name=unit_id, worker_id=worker_id, worker_count=worker_count,
            timestamp=utc_iso(event.get("ts")), category=category, http_status=status,
            message=scrub(message)[:300], retry_count=retry_count, stream_eventually_succeeded=succeeded,
            source="http", url=scrub(event.get("url", "")))
        records.append(record)
        if category != THROTTLING or (status is not None and status >= 400):
            last_http_error = record

    if not succeeded:
        text = scrub(scanner.final_error_text)
        if timed_out:
            category, message = TIMEOUT, "work unit exceeded its timeout and was terminated"
        else:
            category = classify_text(text)
            if category is None and last_http_error is not None:
                category = last_http_error.category
            if category is None:
                category = EXTRACTION_ERROR if text else UNKNOWN
            message = text or "process exited with code %s and no error message" % exit_code
        status_match = _STATUS_IN_TEXT.search(text or "")
        status = int(status_match.group(1) or status_match.group(2)) if status_match else (
            last_http_error.http_status if last_http_error else None)
        records.append(ErrorRecord(
            tap_name=tap, stream_name=unit_id, worker_id=worker_id, worker_count=worker_count,
            timestamp=utc_iso(end_ts), category=category, http_status=status, message=message[:300],
            retry_count=retry_count, stream_eventually_succeeded=False, source="process"))
    return records
