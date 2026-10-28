"""Bounded JSON/SSE sampling for P0. Output contains only allowlisted metadata."""

import codecs
import json
import re
import zlib
from decimal import Decimal, InvalidOperation
from typing import Any

from .usage import InvalidUsage, UsageSnapshot, normalize_usage

MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_JSON_DEPTH = 64
_NEWLINE = re.compile(r"\r\n|\r|\n")


class ProtocolError(ValueError):
    """Safe diagnostics; never includes raw payloads or request credentials."""


def decode_json(text: str) -> Any:
    def reject_constant(_value):
        raise ProtocolError("non-finite JSON number")

    def finite_decimal(number):
        value = Decimal(number)
        if not value.is_finite():
            raise ProtocolError("non-finite JSON number")
        return value

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                # 不回显重复键名，避免私人字段进入错误报告。
                raise ProtocolError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        # 在解析前限制结构深度；不同 Python 版本的 C 解析器递归边界并不一致。
        depth, in_string, escaped = 0, False, False
        for character in text:
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
            elif character == '"':
                in_string = True
            elif character in "[{":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise ProtocolError("JSON nesting exceeds size limit")
            elif character in "]}":
                depth -= 1
        return json.loads(text, parse_float=finite_decimal, parse_constant=reject_constant,
                          object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ValueError, RecursionError, InvalidOperation):
        raise ProtocolError("invalid JSON payload") from None


class SSEDecoder:
    """Incremental UTF-8 and CR/LF decoder; incomplete EOF events are discarded."""

    def __init__(self, max_event_bytes: int = MAX_EVENT_BYTES):
        if isinstance(max_event_bytes, bool) or not isinstance(max_event_bytes, int) or max_event_bytes <= 0:
            raise ProtocolError("invalid SSE event size limit")
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
            self._event_bytes += len(line.encode("utf-8")) + len(match.group())
            if self._event_bytes > self.max_event_bytes:
                raise ProtocolError("SSE event exceeds size limit")
            if not line:
                if self._data:
                    events.append("\n".join(self._data))
                self._data = []
                self._event_bytes = 0
            elif line == "data":
                self._data.append("")
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
        self.incomplete_event = bool(self._buffer or self._data or self._event_bytes)
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
        self._counter_high_water: dict[str, int] = {}

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
            if not 200 <= status < 300:
                self.upstream_error_events += 1
        if "usage" in value and value["usage"] is None:
            # 显式未知的最新用量不能沿用旧快照；后续完整快照仍可恢复观测值。
            self.latest = None
        elif "usage" in value:
            self.usage_snapshots += 1
            try:
                snapshot = normalize_usage(value["usage"])
            except InvalidUsage:
                self.invalid_usage_events += 1
                self.latest = None
            else:
                counters = snapshot.report()
                regressed = False
                for key in ("credits_micro", "prompt_tokens", "completion_tokens", "total_tokens",
                            "cached_tokens", "reasoning_tokens"):
                    current = counters[key]
                    if current is not None:
                        previous = self._counter_high_water.get(key, current)
                        regressed |= current < previous
                        self._counter_high_water[key] = max(previous, current)
                if regressed:
                    self.snapshot_regressions += 1
                self.latest = snapshot
        # Only unwrap a recognized transport envelope. Do not search arbitrary
        # message content, tool results, headers, or user-authored JSON for usage.
        if "body" in value and status is not None:
            if depth >= 4:
                raise ProtocolError("too many nested upstream envelopes")
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
            "schema_version": 2,
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


def _decoded_chunks(stream, content_encoding, max_bytes, max_decoded_bytes, sizes):
    """按声明的 HTTP Content-Encoding 解码；输入和解压输出分别限额。"""
    if content_encoding not in ("identity", "gzip", "deflate"):
        raise ProtocolError("unsupported content encoding")
    for limit in (max_bytes, max_decoded_bytes):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ProtocolError("invalid input size limit")
    decompressor = None
    if content_encoding != "identity":
        # deflate 仅接受 HTTP 标准的 zlib 包装，不猜测 raw deflate 或二进制格式。
        window_bits = zlib.MAX_WBITS + 16 if content_encoding == "gzip" else zlib.MAX_WBITS
        decompressor = zlib.decompressobj(window_bits)

    def account(decoded):
        sizes["decoded_bytes"] += len(decoded)
        if sizes["decoded_bytes"] > max_decoded_bytes:
            raise ProtocolError("decoded input exceeds size limit")
        return decoded

    try:
        while chunk := stream.read(65536):
            sizes["input_bytes"] += len(chunk)
            if sizes["input_bytes"] > max_bytes:
                raise ProtocolError("input exceeds size limit")
            if decompressor is None:
                yield account(chunk)
                continue
            pending = chunk
            while pending:
                if decompressor.eof:
                    raise ProtocolError("data received after compressed stream")
                remaining = max_decoded_bytes - sizes["decoded_bytes"]
                decoded = decompressor.decompress(pending, min(65536, remaining + 1))
                pending = decompressor.unconsumed_tail
                # 本阶段只支持单个压缩流；多成员 gzip 和尾随字节不静默忽略。
                if decompressor.unused_data:
                    raise ProtocolError("data received after compressed stream")
                if decoded:
                    yield account(decoded)
        if decompressor is not None:
            while not decompressor.eof:
                remaining = max_decoded_bytes - sizes["decoded_bytes"]
                decoded = decompressor.decompress(b"", min(65536, remaining + 1))
                if not decoded:
                    raise ProtocolError("incomplete compressed stream")
                yield account(decoded)
    except zlib.error:
        raise ProtocolError("invalid compressed stream") from None


def sample(stream, input_format: str, max_bytes: int = MAX_INPUT_BYTES, *,
           content_encoding: str = "identity", max_decoded_bytes: int = MAX_INPUT_BYTES) -> dict:
    if input_format not in ("sse", "json"):
        raise ProtocolError("unsupported input format")
    collector = UsageCollector()
    decoder = SSEDecoder()
    sizes = {"input_bytes": 0, "decoded_bytes": 0}
    json_chunks = []
    for chunk in _decoded_chunks(stream, content_encoding, max_bytes, max_decoded_bytes, sizes):
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
    report = collector.report()
    if input_format == "json":
        stream_state = "json_complete"
    elif collector.incomplete_event:
        stream_state = "sse_incomplete_event"
    elif collector.done_seen:
        stream_state = "sse_done"
    else:
        # 在事件边界断流仍可能存在完整 usage 快照，不能仅凭 EOF 判定最终用量。
        stream_state = "sse_end_unconfirmed"
    review_reasons = []
    if report["usage_state"] != "credits_observed":
        review_reasons.append(report["usage_state"])
    if stream_state in ("sse_incomplete_event", "sse_end_unconfirmed"):
        review_reasons.append(stream_state)
    if collector.snapshot_regressions:
        review_reasons.append("usage_snapshot_regression")
    report.update(sizes)
    report.update({
        "input_format": input_format,
        "content_encoding": content_encoding,
        "stream_state": stream_state,
        "review_reasons": review_reasons,
        "diagnostic_ready": not review_reasons,
    })
    return report
