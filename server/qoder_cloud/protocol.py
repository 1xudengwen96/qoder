"""Bounded JSON/SSE sampling for P0. Output contains only allowlisted metadata."""

import codecs
import json
import re
from decimal import Decimal
from typing import Any

from .usage import InvalidUsage, UsageSnapshot, normalize_usage

MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 64 * 1024 * 1024
_NEWLINE = re.compile(r"\r\n|\r|\n")


class ProtocolError(ValueError):
    """Safe diagnostics; never includes raw payloads or request credentials."""


def decode_json(text: str) -> Any:
    def reject_constant(_value):
        raise ProtocolError("non-finite JSON number")

    try:
        return json.loads(text, parse_float=Decimal, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise ProtocolError("invalid JSON payload") from None


class SSEDecoder:
    """Incremental UTF-8 and CR/LF decoder; incomplete EOF events are discarded."""

    def __init__(self, max_event_bytes: int = MAX_EVENT_BYTES):
        self.max_event_bytes = max_event_bytes
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self._buffer = ""
        self._data: list[str] = []
        self._event_bytes = 0
        self.incomplete_event = False
        self._finished = False

    def feed(self, chunk: bytes) -> list[str]:
        if self._finished:
            raise ProtocolError("SSE decoder is already finished")
        try:
            self._buffer += self._decoder.decode(chunk)
        except UnicodeDecodeError:
            raise ProtocolError("input is not valid UTF-8") from None
        return self._drain(final=False)

    def _drain(self, final: bool) -> list[str]:
        events = []
        cursor = 0
        for match in _NEWLINE.finditer(self._buffer):
            if not final and match.group() == "\r" and match.end() == len(self._buffer):
                break
            line = self._buffer[cursor:match.start()]
            cursor = match.end()
            self._event_bytes += len(line.encode("utf-8")) + 1
            if self._event_bytes > self.max_event_bytes:
                raise ProtocolError("SSE event exceeds size limit")
            if not line:
                if self._data:
                    events.append("\n".join(self._data))
                self._data = []
                self._event_bytes = 0
            elif line.startswith("data:"):
                value = line[5:]
                self._data.append(value[1:] if value.startswith(" ") else value)
        self._buffer = self._buffer[cursor:]
        if len(self._buffer.encode("utf-8")) + self._event_bytes > self.max_event_bytes:
            raise ProtocolError("SSE event exceeds size limit")
        return events

    def finish(self) -> list[str]:
        if self._finished:
            raise ProtocolError("SSE decoder is already finished")
        self._finished = True
        try:
            self._buffer += self._decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            raise ProtocolError("input is not valid UTF-8") from None
        events = self._drain(final=True)
        self.incomplete_event = bool(self._buffer or self._data)
        self._buffer = ""
        self._data = []
        return events


class UsageCollector:
    """Retains the latest usage snapshot; never sums cumulative stream snapshots."""

    def __init__(self):
        self.latest: UsageSnapshot | None = None
        self.events_seen = 0
        self.usage_snapshots = 0
        self.invalid_usage_events = 0
        self.upstream_error_events = 0
        self.upstream_status_codes: set[int] = set()
        self.done_seen = False
        self.incomplete_event = False
        self.snapshot_regressions = 0

    def observe(self, value: Any, depth: int = 0) -> None:
        if depth > 4:
            raise ProtocolError("too many nested upstream envelopes")
        if not isinstance(value, dict):
            return
        if value.get("error") is not None:
            self.upstream_error_events += 1
        status = value.get("statusCodeValue")
        if status is not None:
            if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
                raise ProtocolError("invalid upstream envelope status")
            self.upstream_status_codes.add(status)
            if status >= 400:
                self.upstream_error_events += 1
        if "usage" in value and value["usage"] is not None:
            self.usage_snapshots += 1
            try:
                snapshot = normalize_usage(value["usage"])
            except InvalidUsage:
                self.invalid_usage_events += 1
                self.latest = None
            else:
                if self.latest:
                    before, after = self.latest.report(), snapshot.report()
                    if any(
                        after[key] < before[key]
                        for key in ("credits_micro", "prompt_tokens", "completion_tokens", "total_tokens")
                        if after[key] is not None and before[key] is not None
                    ):
                        self.snapshot_regressions += 1
                self.latest = snapshot
        # Only unwrap a recognized transport envelope. Do not search arbitrary
        # message content, tool results, headers, or user-authored JSON for usage.
        if "body" in value and status is not None:
            body = value["body"]
            if isinstance(body, dict):
                self.observe(body, depth + 1)
            elif isinstance(body, str):
                if body.strip() == "[DONE]":
                    self.done_seen = True
                elif body.lstrip().startswith("{"):
                    self.observe(decode_json(body), depth + 1)

    def event(self, text: str) -> None:
        self.events_seen += 1
        if self.done_seen and text.strip() != "[DONE]":
            raise ProtocolError("data received after end marker")
        if text.strip() == "[DONE]":
            self.done_seen = True
            return
        self.observe(decode_json(text))

    def report(self) -> dict:
        if self.upstream_error_events:
            state = "upstream_error"
        elif self.invalid_usage_events:
            state = "invalid_usage"
        elif self.latest is None:
            state = "usage_absent"
        elif self.latest.credits_micro is None:
            state = "credits_missing"
        else:
            state = "credits_observed"
        return {
            "schema_version": 1,
            "diagnostic_only": True,
            "usage_state": state,
            "events_seen": self.events_seen,
            "usage_snapshots": self.usage_snapshots,
            "invalid_usage_events": self.invalid_usage_events,
            "upstream_error_events": self.upstream_error_events,
            "upstream_status_codes": sorted(self.upstream_status_codes),
            "done_seen": self.done_seen,
            "incomplete_event": self.incomplete_event,
            "snapshot_regressions": self.snapshot_regressions,
            "usage": self.latest.report() if self.latest else None,
        }


def sample(stream, input_format: str, max_bytes: int = MAX_INPUT_BYTES) -> dict:
    if input_format not in ("sse", "json"):
        raise ProtocolError("unsupported input format")
    collector = UsageCollector()
    decoder = SSEDecoder()
    size = 0
    json_chunks = []
    while chunk := stream.read(65536):
        size += len(chunk)
        if size > max_bytes:
            raise ProtocolError("input exceeds size limit")
        if input_format == "sse":
            for event in decoder.feed(chunk):
                collector.event(event)
        else:
            json_chunks.append(chunk)
    if input_format == "sse":
        for event in decoder.finish():
            collector.event(event)
        collector.incomplete_event = decoder.incomplete_event
    else:
        try:
            text = b"".join(json_chunks).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ProtocolError("input is not valid UTF-8") from None
        collector.events_seen = 1
        collector.observe(decode_json(text))
    return collector.report()
