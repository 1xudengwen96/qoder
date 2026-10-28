"""Offline regression tests: these fixtures are synthetic, not live receipts."""

from decimal import Decimal, InvalidOperation, localcontext
import gzip
import io
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
import zlib

from server.qoder_cloud.protocol import ProtocolError, SSEDecoder, UsageCollector, decode_json, sample
from server.qoder_cloud.usage import InvalidUsage, MAX_LEDGER_UNITS, compare_credits, normalize_usage


class UsageTests(unittest.TestCase):
    def test_missing_is_not_zero(self):
        self.assertIsNone(normalize_usage({"total_tokens": 4}).credits_micro)
        self.assertEqual(normalize_usage({"credits": 0}).credits_micro, 0)
        self.assertIsNone(normalize_usage({"billable": False}).credits_micro)

    def test_decimal_precision_and_boundary(self):
        for value, expected in (("0.000001", 1), ("20.6500000", 20_650_000),
                                ("0e999999999", 0), ("9223372036854.775807", MAX_LEDGER_UNITS)):
            with self.subTest(value=value):
                self.assertEqual(normalize_usage({"credits": value}).credits_micro, expected)

    def test_invalid_credits(self):
        values = (True, -1, "NaN", "Infinity", "-Infinity", "0.0000001",
                  "9223372036854.775808", "1e999999999", "1e-999999999", "secret-not-a-number", 0.1,
                  "1" * 5000)
        for value in values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(InvalidUsage) as caught:
                    normalize_usage({"credits": value})
                self.assertNotIn("secret-not-a-number", str(caught.exception))

    def test_credit_alias_zero_is_not_replaced_by_truthy_fallback(self):
        with self.assertRaises(InvalidUsage):
            normalize_usage({"credits": 0, "credit": 9})
        self.assertEqual(normalize_usage({"credit": "0.5"}).credits_micro, 500000)

    def test_usage_aliases(self):
        result = normalize_usage({"credits": Decimal("0.2"), "input_tokens": 9,
                                  "output_tokens": 2, "input_tokens_details": {"cached_tokens": 4}})
        self.assertEqual((result.prompt_tokens, result.completion_tokens, result.cached_tokens), (9, 2, 4))

    def test_counter_and_detail_validation(self):
        for data in ({"prompt_tokens": True}, {"total_tokens": -1}, {"total_tokens": 1.5},
                     {"completion_tokens_details": []}, {"prompt_tokens_details": "private"},
                     {"prompt_tokens": 3, "input_tokens": 4},
                     {"prompt_tokens_details": {"cached_tokens": 1},
                      "input_tokens_details": {"cached_tokens": 2}},
                     {"prompt_tokens": 1, "prompt_cache_hit_tokens": 2},
                     {"billable": "false"}, {"billable": False, "credits": 1}):
            with self.subTest(keys=list(data)):
                with self.assertRaises(InvalidUsage):
                    normalize_usage(data)


class SSETests(unittest.TestCase):
    def test_utf8_crlf_and_every_byte_chunking(self):
        payload = '\ufeff: ping\r\ndata: {"text":"测试",\r\ndata: "usage":{"credits":0}}\r\n\r\ndata: [DONE]\r\n\r\n'.encode()
        decoder = SSEDecoder()
        events = []
        for byte in payload:
            events.extend(decoder.feed(bytes([byte])))
        events.extend(decoder.finish())
        self.assertEqual(len(events), 2)
        self.assertEqual(decode_json(events[0])["text"], "测试")
        self.assertEqual(events[1], "[DONE]")
        self.assertFalse(decoder.incomplete_event)

    def test_lf_and_cr_events(self):
        for delimiter in ("\n", "\r", "\r\n"):
            decoder = SSEDecoder()
            events = decoder.feed(("data: [DONE]" + delimiter * 2).encode())
            events += decoder.finish()
            self.assertEqual(events, ["[DONE]"])

    def test_incomplete_final_event_is_not_a_receipt(self):
        decoder = SSEDecoder()
        self.assertEqual(decoder.feed(b'data: {"usage":{"credits":1}}\n'), [])
        self.assertEqual(decoder.finish(), [])
        self.assertTrue(decoder.incomplete_event)

    def test_size_bounds_even_for_comment_and_unterminated_line(self):
        for payload in (b":" + b"x" * 100, b"data: " + b"x" * 100 + b"\n\n"):
            with self.assertRaises(ProtocolError):
                SSEDecoder(max_event_bytes=32).feed(payload)

    def test_utf8_error_does_not_echo_payload(self):
        with self.assertRaises(ProtocolError) as caught:
            SSEDecoder().feed(b'data: private\xff')
        self.assertNotIn("private", str(caught.exception))
        decoder = SSEDecoder()
        decoder.feed(b'data: \xe4')
        with self.assertRaises(ProtocolError):
            decoder.finish()

    def test_data_after_done_is_rejected(self):
        collector = UsageCollector()
        collector.event("[DONE]")
        with self.assertRaises(ProtocolError):
            collector.event('{"usage":{"credits":9}}')


class SamplingTests(unittest.TestCase):
    def report(self, data):
        return sample(io.BytesIO(json.dumps(data).encode()), "json")

    def test_native_envelope_and_allowlisted_output(self):
        report = self.report({"headers": {"Authorization": "private-sentinel"},
                              "statusCodeValue": 200,
                              "body": json.dumps({"choices": [{"delta": {"content": "private-sentinel"}}],
                                                  "usage": {"credits": 0.125, "prompt_tokens": 10}})})
        self.assertEqual(report["usage"]["credits_micro"], 125000)
        self.assertTrue(report["diagnostic_only"])
        self.assertNotIn("private-sentinel", json.dumps(report))

    def test_latest_cumulative_snapshot_is_not_summed(self):
        payload = b'data: {"usage":{"credits":1,"total_tokens":2}}\n\ndata: {"usage":{"credits":2,"total_tokens":4}}\n\ndata: [DONE]\n\n'
        report = sample(io.BytesIO(payload), "sse")
        self.assertEqual(report["usage"]["credits_micro"], 2000000)
        self.assertEqual(report["usage"]["total_tokens"], 4)
        self.assertEqual(report["usage_snapshots"], 2)

    def test_partial_latest_snapshot_is_not_filled_from_an_older_snapshot(self):
        collector = UsageCollector()
        collector.observe({"usage": {"credits": 5}})
        collector.observe({"usage": {"total_tokens": 10}})
        self.assertEqual(collector.report()["usage_state"], "credits_missing")

    def test_regression_is_reported_not_silently_combined(self):
        collector = UsageCollector()
        collector.observe({"usage": {"credits": 2}})
        collector.observe({"usage": {"credits": 1}})
        self.assertEqual(collector.report()["snapshot_regressions"], 1)
        self.assertEqual(collector.report()["usage"]["credits_micro"], 1000000)

    def test_no_recursive_search_in_message_or_unknown_body(self):
        for payload in ({"choices": [{"message": {"usage": {"credits": 10}}}]},
                        {"body": {"usage": {"credits": 10}}}):
            self.assertEqual(self.report(payload)["usage_state"], "usage_absent")

    def test_error_even_when_http_envelope_contains_usage(self):
        report = self.report({"statusCodeValue": 429, "body": {"usage": {"credits": 0}}})
        self.assertEqual(report["usage_state"], "upstream_error")
        self.assertEqual(report["upstream_status_codes"], [429])

    def test_explicit_inner_error(self):
        self.assertEqual(self.report({"error": {"message": "private"}})["usage_state"], "upstream_error")

    def test_invalid_usage_is_not_zero(self):
        report = self.report({"usage": {"credits": "NaN"}})
        self.assertEqual(report["usage_state"], "invalid_usage")
        self.assertIsNone(report["usage"])

    def test_invalid_json_nonfinite_and_input_limits(self):
        for payload in (b'{"usage":{"credits":NaN}}', b'{"usage":', b"\xff"):
            with self.assertRaises(ProtocolError):
                sample(io.BytesIO(payload), "json")
        with self.assertRaises(ProtocolError):
            sample(io.BytesIO(b"too much data"), "sse", max_bytes=4)

    def test_invalid_envelope_status_and_depth(self):
        for status in (True, "200", 999):
            with self.assertRaises(ProtocolError):
                self.report({"statusCodeValue": status})
        value = {"usage": {"credits": 0}}
        for _ in range(6):
            value = {"statusCodeValue": 200, "body": value}
        with self.assertRaises(ProtocolError):
            self.report(value)

    def test_synthetic_fixture(self):
        with open(Path(__file__).parent / "fixtures/synthetic-native.sse", "rb") as source:
            report = sample(source, "sse")
        self.assertEqual(report["usage"]["credits_micro"], 125000)
        self.assertTrue(report["done_seen"])


class CompressedSamplingTests(unittest.TestCase):
    """人工压缩样本；不读取任何真实响应。"""

    JSON = '{"text":"测试 private-sentinel","usage":{"credits":0.125}}'.encode()
    SSE = 'data: {"text":"测试 private-sentinel","usage":{"credits":0.125}}\n\ndata: [DONE]\n\n'.encode()

    def encode(self, payload, encoding):
        return gzip.compress(payload, mtime=0) if encoding == "gzip" else zlib.compress(payload)

    def test_json_compression_preserves_usage_and_redaction(self):
        for encoding in ("gzip", "deflate"):
            with self.subTest(encoding=encoding):
                encoded = self.encode(self.JSON, encoding)
                report = sample(io.BytesIO(encoded), "json", content_encoding=encoding)
                self.assertEqual(report["usage"]["credits_micro"], 125000)
                self.assertEqual(report["input_bytes"], len(encoded))
                self.assertEqual(report["decoded_bytes"], len(self.JSON))
                self.assertEqual(report["content_encoding"], encoding)
                self.assertEqual(report["stream_state"], "json_complete")
                self.assertTrue(report["diagnostic_ready"])
                self.assertTrue(report["diagnostic_only"])
                self.assertEqual(report["schema_version"], 2)
                self.assertNotIn("private-sentinel", json.dumps(report))

    def test_sse_compression_preserves_completion(self):
        for encoding in ("gzip", "deflate"):
            with self.subTest(encoding=encoding):
                report = sample(io.BytesIO(self.encode(self.SSE, encoding)), "sse", content_encoding=encoding)
                self.assertEqual(report["stream_state"], "sse_done")
                self.assertTrue(report["diagnostic_ready"])
                self.assertEqual(report["usage"]["credits_micro"], 125000)

    def test_compressed_input_split_every_byte(self):
        class ByteStream(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 1))

        for encoding in ("gzip", "deflate"):
            with self.subTest(encoding=encoding):
                encoded = self.encode(self.SSE, encoding)
                report = sample(ByteStream(encoded), "sse", content_encoding=encoding)
                self.assertEqual(report["input_bytes"], len(encoded))
                self.assertEqual(report["decoded_bytes"], len(self.SSE))
                self.assertTrue(report["diagnostic_ready"])

    def test_decoded_chunks_cross_64k_boundary(self):
        # 固定种子的人工数据，同时覆盖压缩输入与解压输出跨 64 KiB 分块。
        contents = ("x" * 200000, random.Random(0).randbytes(200000).hex())
        for content in contents:
            payload = json.dumps({"content": content, "usage": {"credits": 0}}).encode()
            for encoding in ("gzip", "deflate"):
                with self.subTest(encoding=encoding, decoded_length=len(payload)):
                    encoded = self.encode(payload, encoding)
                    if len(content) > 200000:
                        self.assertGreater(len(encoded), 65536)
                    report = sample(io.BytesIO(encoded), "json", content_encoding=encoding)
                    self.assertEqual(report["input_bytes"], len(encoded))
                    self.assertEqual(report["decoded_bytes"], len(payload))
                    self.assertEqual(report["usage"]["credits_micro"], 0)

    def test_exact_size_limits_are_accepted(self):
        for encoding in ("identity", "gzip", "deflate"):
            with self.subTest(encoding=encoding):
                encoded = self.JSON if encoding == "identity" else self.encode(self.JSON, encoding)
                report = sample(io.BytesIO(encoded), "json", max_bytes=len(encoded),
                                content_encoding=encoding, max_decoded_bytes=len(self.JSON))
                self.assertTrue(report["diagnostic_ready"])

    def test_wire_input_limit_is_independent(self):
        encoded = self.encode(self.JSON, "gzip")
        with self.assertRaises(ProtocolError):
            sample(io.BytesIO(encoded), "json", max_bytes=len(encoded) - 1, content_encoding="gzip")

    def test_decompressed_size_is_limited(self):
        for encoding in ("identity", "gzip", "deflate"):
            with self.subTest(encoding=encoding):
                encoded = self.JSON if encoding == "identity" else self.encode(self.JSON, encoding)
                with self.assertRaises(ProtocolError):
                    sample(io.BytesIO(encoded), "json", content_encoding=encoding,
                           max_decoded_bytes=len(self.JSON) - 1)

    def test_compression_bomb_is_rejected_before_json_parsing(self):
        payload = b'{"text":"' + b'x' * (2 * 1024 * 1024) + b'"}'
        for encoding in ("gzip", "deflate"):
            with self.subTest(encoding=encoding):
                with self.assertRaisesRegex(ProtocolError, "decoded input exceeds size limit"):
                    sample(io.BytesIO(self.encode(payload, encoding)), "json",
                           content_encoding=encoding, max_decoded_bytes=1024)

    def test_truncated_compressed_trailer_does_not_return_partial_usage(self):
        for encoding in ("gzip", "deflate"):
            with self.subTest(encoding=encoding):
                encoded = self.encode(self.SSE, encoding)
                with self.assertRaisesRegex(ProtocolError, "incomplete compressed stream"):
                    sample(io.BytesIO(encoded[:-1]), "sse", content_encoding=encoding)

    def test_bad_checksum_is_rejected(self):
        for encoding in ("gzip", "deflate"):
            with self.subTest(encoding=encoding):
                encoded = bytearray(self.encode(self.JSON, encoding))
                encoded[-1] ^= 0xFF
                with self.assertRaises(ProtocolError):
                    sample(io.BytesIO(encoded), "json", content_encoding=encoding)

    def test_trailing_data_and_concatenated_members_are_rejected(self):
        for encoding in ("gzip", "deflate"):
            encoded = self.encode(self.SSE, encoding)
            for tail in (b"private-tail-sentinel", encoded):
                with self.subTest(encoding=encoding, tail_length=len(tail)):
                    with self.assertRaises(ProtocolError) as caught:
                        sample(io.BytesIO(encoded + tail), "sse", content_encoding=encoding)
                    self.assertNotIn("private-tail-sentinel", str(caught.exception))

    def test_trailing_data_in_later_input_chunk_is_rejected(self):
        encoded = self.encode(self.SSE, "gzip")

        class SplitStream(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, len(encoded)))

        with self.assertRaises(ProtocolError):
            sample(SplitStream(encoded + b"tail"), "sse", content_encoding="gzip")

    def test_raw_deflate_is_not_guessed(self):
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        encoded = compressor.compress(self.SSE) + compressor.flush()
        with self.assertRaises(ProtocolError):
            sample(io.BytesIO(encoded), "sse", content_encoding="deflate")

    def test_unsupported_and_mislabeled_encodings_are_rejected(self):
        for encoding in ("br", "zstd", "gzip, deflate", "GZIP"):
            with self.subTest(encoding=encoding):
                with self.assertRaises(ProtocolError):
                    sample(io.BytesIO(b""), "sse", content_encoding=encoding)
        for encoding in ("gzip", "deflate"):
            with self.subTest(encoding=encoding):
                with self.assertRaises(ProtocolError) as caught:
                    sample(io.BytesIO(b"private-binary-sentinel"), "sse", content_encoding=encoding)
                self.assertNotIn("private-binary-sentinel", str(caught.exception))
                with self.assertRaises(ProtocolError):
                    sample(io.BytesIO(b""), "sse", content_encoding=encoding)

    def test_gzip_private_filename_is_not_in_report(self):
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", filename="private-filename-sentinel", mtime=0) as target:
            target.write(self.JSON)
        report = sample(io.BytesIO(buffer.getvalue()), "json", content_encoding="gzip")
        self.assertNotIn("private-filename-sentinel", json.dumps(report))

    def test_invalid_limits_are_rejected(self):
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit):
                with self.assertRaises(ProtocolError):
                    sample(io.BytesIO(b""), "sse", max_bytes=limit)
                with self.assertRaises(ProtocolError):
                    sample(io.BytesIO(b""), "sse", max_decoded_bytes=limit)


class CompletionTests(unittest.TestCase):
    def report(self, payload):
        return sample(io.BytesIO(payload), "sse")

    def test_eof_at_event_boundary_is_not_confirmation(self):
        report = self.report(b'data: {"usage":{"credits":1}}\n\n')
        self.assertEqual(report["usage_state"], "credits_observed")
        self.assertFalse(report["incomplete_event"])
        self.assertFalse(report["done_seen"])
        self.assertEqual(report["stream_state"], "sse_end_unconfirmed")
        self.assertEqual(report["review_reasons"], ["sse_end_unconfirmed"])
        self.assertFalse(report["diagnostic_ready"])

    def test_incomplete_tail_keeps_observed_snapshot_but_requires_review(self):
        report = self.report(b'data: {"usage":{"credits":1}}\n\ndata: {"usage":{"credits":2}}\n')
        self.assertEqual(report["usage"]["credits_micro"], 1000000)
        self.assertEqual(report["stream_state"], "sse_incomplete_event")
        self.assertIn("sse_incomplete_event", report["review_reasons"])
        self.assertFalse(report["diagnostic_ready"])

    def test_snapshot_regression_requires_review_even_with_done(self):
        report = self.report(b'data: {"usage":{"credits":2}}\n\ndata: {"usage":{"credits":1}}\n\ndata: [DONE]\n\n')
        self.assertEqual(report["stream_state"], "sse_done")
        self.assertEqual(report["review_reasons"], ["usage_snapshot_regression"])
        self.assertFalse(report["diagnostic_ready"])

    def test_done_without_credits_is_not_ready(self):
        for payload, state in ((b'data: [DONE]\n\n', "usage_absent"),
                               (b'data: {"usage":{"total_tokens":1}}\n\ndata: [DONE]\n\n', "credits_missing")):
            with self.subTest(state=state):
                report = self.report(payload)
                self.assertEqual(report["stream_state"], "sse_done")
                self.assertIn(state, report["review_reasons"])
                self.assertFalse(report["diagnostic_ready"])

    def test_native_done_marker_is_recognized(self):
        report = self.report(b'data: {"statusCodeValue":200,"body":{"usage":{"credits":0}}}\n\n'
                             b'data: {"statusCodeValue":200,"body":"[DONE]"}\n\n')
        self.assertTrue(report["diagnostic_ready"])
        self.assertEqual(report["stream_state"], "sse_done")

    def test_user_content_cannot_confirm_completion(self):
        report = self.report(b'data: {"message":{"body":"[DONE]"},"usage":{"credits":0}}\n\n')
        self.assertFalse(report["done_seen"])
        self.assertFalse(report["diagnostic_ready"])

    def test_valid_json_needs_no_sse_marker(self):
        report = sample(io.BytesIO(b'{"usage":{"credits":0}}'), "json")
        self.assertTrue(report["diagnostic_ready"])
        self.assertEqual(report["stream_state"], "json_complete")
        self.assertEqual(report["content_encoding"], "identity")
        self.assertEqual(report["input_bytes"], report["decoded_bytes"])


class CLITests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "server.qoder_cloud.probe", *args],
                              capture_output=True, text=True, check=False)

    def test_output_file_cannot_overwrite_input_or_existing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "raw.json"
            source.write_text('{"usage":{"credits":0}}')
            result = self.run_cli("--input", str(source), "--format", "json", "--output", str(source))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(source.read_text(), '{"usage":{"credits":0}}')
            output = Path(directory) / "report.json"
            args = ("--input", str(source), "--format", "json", "--output", str(output))
            self.assertEqual(self.run_cli(*args).returncode, 0)
            before = output.read_bytes()
            self.assertEqual(self.run_cli(*args).returncode, 1)
            self.assertEqual(output.read_bytes(), before)

    def test_unknown_credits_nonzero_exit_and_safe_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "private-path-sentinel.json"
            source.write_text('{"usage":{"total_tokens":1}}')
            result = self.run_cli("--input", str(source), "--format", "json")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["usage_state"], "credits_missing")
            source.write_text('{"password":"private-value-sentinel",bad}')
            result = self.run_cli("--input", str(source), "--format", "json")
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("private-value-sentinel", result.stdout + result.stderr)
            self.assertNotIn("private-path-sentinel", result.stdout + result.stderr)

    def test_cli_requires_done_and_nonregressing_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic-stream.sse"
            for payload, reason in ((b'data: {"usage":{"credits":1}}\n\n', "sse_end_unconfirmed"),
                                    (b'data: {"usage":{"credits":2}}\n\ndata: {"usage":{"credits":1}}\n\ndata: [DONE]\n\n',
                                     "usage_snapshot_regression")):
                with self.subTest(reason=reason):
                    source.write_bytes(payload)
                    result = self.run_cli("--input", str(source))
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(reason, json.loads(result.stdout)["review_reasons"])

    def test_compressed_cli_file_and_stdin(self):
        encoded = gzip.compress(CompressedSamplingTests.SSE, mtime=0)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic-response.gz"
            source.write_bytes(encoded)
            output = Path(directory) / "diagnostic.json"
            result = self.run_cli("--input", str(source), "--content-encoding", "gzip", "--output", str(output))
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["stream_state"], "sse_done")
            source.write_bytes(encoded[:-1])
            result = self.run_cli("--input", str(source), "--content-encoding", "gzip")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
        result = subprocess.run([sys.executable, "-m", "server.qoder_cloud.probe", "--input", "-",
                                 "--content-encoding", "gzip"], input=encoded, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)["diagnostic_ready"])


class ProtocolBoundaryTests(unittest.TestCase):
    """人工数据覆盖歧义 JSON、SSE 线框边界与累计快照回退。"""

    def test_duplicate_keys_and_escaped_equivalents_are_rejected(self):
        for text in ('{"usage":{"credits":1,"credits":0}}',
                     '{"statusCodeValue":401,"statusCodeValue":200}',
                     '{"private-key-sentinel":1,"private-key-sentinel":2}',
                     r'{"usage":{"credits":1,"\u0063redits":0}}'):
            with self.subTest(length=len(text)), self.assertRaises(ProtocolError) as caught:
                decode_json(text)
            self.assertNotIn("private-key-sentinel", str(caught.exception))

    def test_duplicate_keys_in_native_string_and_compressed_sse(self):
        inner = '{"usage":{"credits":1,"credits":0}}'
        payload = ('data: ' + json.dumps({"statusCodeValue": 200, "body": inner}) + '\n\n').encode()
        for encoding, data in (("identity", payload), ("gzip", gzip.compress(payload, mtime=0)),
                               ("deflate", zlib.compress(payload))):
            with self.subTest(encoding=encoding), self.assertRaises(ProtocolError):
                sample(io.BytesIO(data), "sse", content_encoding=encoding)

    def test_unique_json_still_preserves_exact_decimal(self):
        value = decode_json('{"usage":{"credits":0.123456},"text":"private-sentinel"}')
        self.assertEqual(value["usage"]["credits"], Decimal("0.123456"))
        self.assertEqual(normalize_usage(value["usage"]).credits_micro, 123456)

    def test_sse_event_limit_counts_crlf_and_utf8_bytes(self):
        for delimiter in ("\n", "\r", "\r\n"):
            for content in ("ok", "测试"):
                payload = ("data: " + content + delimiter * 2).encode()
                for chunk_size in (1, len(payload)):
                    with self.subTest(delimiter=repr(delimiter), chunk_size=chunk_size):
                        decoder = SSEDecoder(max_event_bytes=len(payload))
                        events = []
                        for offset in range(0, len(payload), chunk_size):
                            events += decoder.feed(payload[offset:offset + chunk_size])
                        events += decoder.finish()
                        self.assertEqual(events, [content])
                        self.assertFalse(decoder.incomplete_event)
                        decoder = SSEDecoder(max_event_bytes=len(payload) - 1)
                        with self.assertRaises(ProtocolError):
                            for offset in range(0, len(payload), chunk_size):
                                decoder.feed(payload[offset:offset + chunk_size])
                            decoder.finish()

    def test_invalid_sse_event_limits_are_rejected(self):
        for limit in (None, True, 0, -1, 1.5, "32"):
            with self.subTest(limit_type=type(limit).__name__), self.assertRaises(ProtocolError):
                SSEDecoder(max_event_bytes=limit)

    def test_bare_data_field_dispatches_empty_event(self):
        for payload in (b"data\n\n", b"data:\n\n", b"data: \n\n"):
            self.assertEqual(SSEDecoder().feed(payload), [""])

    def test_sse_decoder_cannot_be_reused_after_finish(self):
        decoder = SSEDecoder()
        decoder.finish()
        for action in (lambda: decoder.feed(b""), decoder.finish):
            with self.assertRaises(ProtocolError):
                action()

    def test_comment_and_unknown_field_tail_requires_boundary(self):
        prefix = b'data: {"usage":{"credits":0}}\n\ndata: [DONE]\n\n'
        for tail in (b": keepalive\n", b"event: private-sentinel\n", b"id: ignored\r\n"):
            report = sample(io.BytesIO(prefix + tail), "sse")
            self.assertEqual(report["stream_state"], "sse_incomplete_event")
            self.assertFalse(report["diagnostic_ready"])
            self.assertNotIn("private-sentinel", json.dumps(report))
            self.assertTrue(sample(io.BytesIO(prefix + tail + b"\n"), "sse")["diagnostic_ready"])

    def test_empty_data_after_done_is_not_silently_ignored(self):
        with self.assertRaises(ProtocolError):
            sample(io.BytesIO(b'data: {"usage":{"credits":0}}\n\ndata: [DONE]\n\ndata\n\n'), "sse")

    def test_non_success_statuses_cannot_confirm_usage(self):
        for status in (100, 199, 300, 302, 399, 400, 599):
            payload = json.dumps({"statusCodeValue": status, "body": {"usage": {"credits": 0}}}).encode()
            report = sample(io.BytesIO(payload), "json")
            self.assertEqual(report["usage_state"], "upstream_error")
            self.assertFalse(report["diagnostic_ready"])
            self.assertEqual(report["upstream_status_codes"], [status])

    def test_success_status_range_remains_supported(self):
        for status in (200, 204, 299):
            payload = json.dumps({"statusCodeValue": status, "body": {"usage": {"credits": 0}}}).encode()
            self.assertTrue(sample(io.BytesIO(payload), "json")["diagnostic_ready"])

    def test_status_type_and_range_are_strict(self):
        for status in (False, 0, 99, 600, 200.0, "200", [], {}):
            with self.subTest(status_type=type(status).__name__), self.assertRaises(ProtocolError):
                UsageCollector().observe({"statusCodeValue": status})

    def test_native_envelope_depth_exact_boundary(self):
        for string_body in (False, True):
            value = {"usage": {"credits": 0}}
            for _ in range(4):
                value = {"statusCodeValue": 200, "body": json.dumps(value) if string_body else value}
            self.assertTrue(sample(io.BytesIO(json.dumps(value).encode()), "json")["diagnostic_ready"])
            value = {"statusCodeValue": 200, "body": json.dumps(value) if string_body else value}
            with self.assertRaises(ProtocolError):
                sample(io.BytesIO(json.dumps(value).encode()), "json")

    def test_native_done_marker_obeys_same_envelope_depth_limit(self):
        for depth in (4, 5):
            value = "[DONE]"
            for _ in range(depth):
                value = {"statusCodeValue": 200, "body": value}
            payload = b'data: {"usage":{"credits":0}}\n\n' + ('data: ' + json.dumps(value) + '\n\n').encode()
            if depth == 4:
                self.assertTrue(sample(io.BytesIO(payload), "sse")["diagnostic_ready"])
            else:
                with self.assertRaises(ProtocolError):
                    sample(io.BytesIO(payload), "sse")

    def test_explicit_null_usage_clears_older_credits(self):
        payload = b'data: {"usage":{"credits":1}}\n\ndata: {"usage":null}\n\ndata: [DONE]\n\n'
        report = sample(io.BytesIO(payload), "sse")
        self.assertIsNone(report["usage"])
        self.assertEqual(report["usage_state"], "usage_absent")
        self.assertFalse(report["diagnostic_ready"])

    def test_null_intermediate_usage_can_be_followed_by_complete_usage(self):
        payload = b'data: {"usage":null}\n\ndata: {"usage":{"credits":0}}\n\ndata: [DONE]\n\n'
        self.assertTrue(sample(io.BytesIO(payload), "sse")["diagnostic_ready"])

    def test_regression_is_detected_across_partial_or_null_snapshots(self):
        for middle in (None, {"total_tokens": 10}):
            events = [{"usage": {"credits": 2}}, {"usage": middle}, {"usage": {"credits": 1}}]
            payload = ''.join('data: ' + json.dumps(value) + '\n\n' for value in events).encode() + b'data: [DONE]\n\n'
            report = sample(io.BytesIO(payload), "sse")
            self.assertEqual(report["snapshot_regressions"], 1)
            self.assertEqual(report["usage"]["credits_micro"], 1000000)
            self.assertFalse(report["diagnostic_ready"])

    def test_cached_and_reasoning_token_regressions_require_review(self):
        for field in ("cached_tokens", "reasoning_tokens"):
            events = [{"usage": {"credits": 1, "completion_tokens_details": {field: count}}} for count in (2, 1)]
            payload = ''.join('data: ' + json.dumps(value) + '\n\n' for value in events).encode() + b'data: [DONE]\n\n'
            report = sample(io.BytesIO(payload), "sse")
            self.assertEqual(report["snapshot_regressions"], 1)
            self.assertIn("usage_snapshot_regression", report["review_reasons"])

    def test_invalid_usage_remains_invalid_after_later_valid_snapshot(self):
        payload = b'data: {"usage":{"credits":true}}\n\ndata: {"usage":{"credits":0}}\n\ndata: [DONE]\n\n'
        report = sample(io.BytesIO(payload), "sse")
        self.assertEqual(report["usage_state"], "invalid_usage")
        self.assertFalse(report["diagnostic_ready"])

    def test_billable_flag_and_credits_type_boundaries(self):
        for value in ([], {}, True, "private-sentinel", -1):
            with self.assertRaises(InvalidUsage) as caught:
                normalize_usage({"credits": value})
            self.assertNotIn("private-sentinel", str(caught.exception))
        for value in (0, [], {}, "true"):
            with self.assertRaises(InvalidUsage):
                normalize_usage({"billable": value})
        self.assertEqual(normalize_usage({"billable": False, "credits": "0"}).credits_micro, 0)
        self.assertIsNone(normalize_usage({"billable": True}).credits_micro)


class MalformedAndChunkedProtocolTests(unittest.TestCase):
    """固定种子的人工拆包与畸形输入，不依赖平台账号或系统证书。"""

    def test_out_of_range_decimal_exponent_has_safe_protocol_error(self):
        for sign in ("", "-"):
            number = "1e" + sign + "9" * 30
            with self.assertRaises(ProtocolError):
                decode_json('{"private-sentinel":' + number + '}')

    def test_decimal_error_is_rejected_even_when_caller_disables_traps(self):
        with localcontext() as context:
            context.traps[InvalidOperation] = False
            with self.assertRaises(ProtocolError):
                decode_json('1e99999999999999999999999999')

    def test_deep_json_recursion_has_safe_protocol_error(self):
        text = '{"private-sentinel":' + '[' * 2000 + '0' + ']' * 2000 + '}'
        with self.assertRaises(ProtocolError) as caught:
            decode_json(text)
        self.assertNotIn("private-sentinel", str(caught.exception))

    def test_json_depth_accepts_exact_limit_and_rejects_extra_level(self):
        value = decode_json('[' * 64 + '0' + ']' * 64)
        for _ in range(64):
            self.assertIsInstance(value, list)
            self.assertEqual(len(value), 1)
            value = value[0]
        self.assertEqual(value, 0)
        with self.assertRaises(ProtocolError):
            decode_json('[' * 65 + '0' + ']' * 65)

    def test_brackets_and_escaped_quotes_inside_strings_do_not_count_as_depth(self):
        text = '[{\\"\\\\\\"' * 100
        self.assertEqual(decode_json(json.dumps({"text": text}))["text"], text)

    def test_random_chunking_matches_whole_stream_across_encodings(self):
        class ChunkStream(io.BytesIO):
            def __init__(self, data, seed):
                super().__init__(data)
                self.random = random.Random(seed)

            def read(self, size=-1):
                return super().read(min(size, self.random.randint(1, 31)))

        for delimiter in ("\n", "\r", "\r\n"):
            payload = ('\ufeff: 心跳' + delimiter * 2 +
                       'data: {"text":"人工 private-sentinel",' + delimiter +
                       'data: "usage":{"credits":0.125,"total_tokens":10}}' + delimiter * 2 +
                       'data: {"statusCodeValue":200,"body":"[DONE]"}' + delimiter * 2).encode()
            for encoding in ("identity", "gzip", "deflate"):
                if encoding == "identity":
                    encoded = payload
                elif encoding == "gzip":
                    encoded = gzip.compress(payload, mtime=0)
                else:
                    encoded = zlib.compress(payload)
                expected = sample(io.BytesIO(encoded), "sse", content_encoding=encoding)
                self.assertTrue(expected["diagnostic_ready"])
                for seed in range(10):
                    with self.subTest(delimiter=repr(delimiter), encoding=encoding, seed=seed):
                        actual = sample(ChunkStream(encoded, seed), "sse", content_encoding=encoding)
                        self.assertEqual(actual, expected)
                        self.assertNotIn("private-sentinel", json.dumps(actual))

    def test_empty_and_binary_inputs_do_not_produce_credits(self):
        for payload in (b"", b"\x00\x01\xff", b"{", b'{"usage":'):
            with self.assertRaises(ProtocolError):
                sample(io.BytesIO(payload), "json")
        report = sample(io.BytesIO(b""), "sse")
        self.assertIsNone(report["usage"])
        self.assertFalse(report["diagnostic_ready"])

    def test_malformed_cli_input_never_prints_traceback_or_private_text(self):
        payloads = (b'{"private-sentinel":1e99999999999999999999999999}',
                    b'{"private-sentinel":0,"private-sentinel":1}',
                    b'{"private-sentinel":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}')
        for payload in payloads:
            with tempfile.TemporaryDirectory() as directory:
                source, output = Path(directory) / "private-path-sentinel", Path(directory) / "report.json"
                source.write_bytes(payload)
                result = subprocess.run([sys.executable, "-m", "server.qoder_cloud.probe", "--input", str(source),
                                         "--format", "json", "--output", str(output), "--summary"],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertFalse(output.exists())
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("private-sentinel", result.stderr)
                self.assertNotIn("private-path-sentinel", result.stderr)


class OfficialComparisonTests(unittest.TestCase):
    """纯离线数字核对；不读取账号，不将人工值当作可信官方回执。"""

    def test_exact_decimal_match_and_diagnostic_scope(self):
        result = compare_credits(125000, "0.125", sample_ready=True)
        self.assertEqual(result["state"], "matched")
        self.assertEqual(result["difference_micro"], 0)
        self.assertTrue(result["diagnostic_only"])
        self.assertFalse(result["request_correlation_verified"])
        self.assertEqual(result["official_value_source"], "manual_unverified")

    def test_zero_is_a_real_match_not_missing(self):
        self.assertEqual(compare_credits(0, "0", sample_ready=True)["state"], "matched")

    def test_mismatch_has_signed_integer_difference(self):
        for observed, expected in ((124999, -1), (125001, 1)):
            result = compare_credits(observed, "0.125", sample_ready=True)
            self.assertEqual(result["state"], "mismatch")
            self.assertEqual(result["difference_micro"], expected)

    def test_missing_credits_never_matches_official_zero(self):
        result = compare_credits(None, 0, sample_ready=True)
        self.assertEqual(result["state"], "credits_missing")
        self.assertIsNone(result["difference_micro"])

    def test_unfinished_or_invalid_sample_cannot_match(self):
        result = compare_credits(125000, "0.125", sample_ready=False)
        self.assertEqual(result["state"], "sample_unconfirmed")
        self.assertEqual(result["difference_micro"], 0)

    def test_invalid_official_values_are_rejected_without_echo(self):
        for value in (None, True, 0.125, "NaN", "Infinity", "-1", "0.0000001", "private-sentinel", "1e999999999"):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(InvalidUsage) as caught:
                compare_credits(0, value, sample_ready=True)
            self.assertNotIn("private-sentinel", str(caught.exception))

    def test_invalid_observed_units_and_state_are_rejected(self):
        for value in (True, -1, 0.1, "125000", MAX_LEDGER_UNITS + 1):
            with self.assertRaises(InvalidUsage):
                compare_credits(value, 0, sample_ready=True)
        with self.assertRaises(InvalidUsage):
            compare_credits(0, 0, sample_ready="true")

    def test_large_comparison_preserves_integer_precision(self):
        result = compare_credits(MAX_LEDGER_UNITS - 1, "9223372036854.775807", sample_ready=True)
        self.assertEqual(result["difference_micro"], -1)
        self.assertEqual(result["official_credits_micro"], MAX_LEDGER_UNITS)


class OfficialComparisonCLITests(unittest.TestCase):
    def run_cli(self, payload, *options):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic-response"
            source.write_bytes(payload)
            return subprocess.run([sys.executable, "-m", "server.qoder_cloud.probe", "--input", str(source), *options],
                                  capture_output=True, text=True, check=False)

    def test_json_match_with_summary_keeps_stdout_json(self):
        result = self.run_cli(b'{"text":"private-sentinel","usage":{"credits":0.125}}',
                              "--format", "json", "--official-credits", "0.125", "--summary")
        self.assertEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertEqual(report["official_comparison"]["state"], "matched")
        self.assertTrue(report["diagnostic_only"])
        self.assertIn("credits_observed", result.stderr)
        self.assertNotIn("private-sentinel", result.stdout + result.stderr)

    def test_sse_match_requires_end_marker(self):
        prefix = b'data: {"usage":{"credits":0.125}}\n\n'
        for suffix, expected in ((b'data: [DONE]\n\n', 0), (b'', 2)):
            result = self.run_cli(prefix + suffix, "--official-credits", "0.125")
            self.assertEqual(result.returncode, expected)
            report = json.loads(result.stdout)
            self.assertEqual(report["official_comparison"]["state"], "matched" if expected == 0 else "sample_unconfirmed")

    def test_mismatch_exits_two_and_exposes_fixed_reason(self):
        result = self.run_cli(b'{"usage":{"credits":0.125}}', "--format", "json", "--official-credits", "0.125001")
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertEqual(report["official_comparison"]["difference_micro"], -1)
        self.assertIn("official_credits_mismatch", report["review_reasons"])
        self.assertFalse(report["diagnostic_ready"])

    def test_missing_usage_cannot_be_compared_as_zero(self):
        result = self.run_cli(b'{"text":"private-sentinel"}', "--format", "json", "--official-credits", "0")
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertEqual(report["official_comparison"]["state"], "credits_missing")
        self.assertIn("official_comparison_unconfirmed", report["review_reasons"])

    def test_upstream_error_cannot_be_confirmed_by_equal_official_value(self):
        result = self.run_cli(b'{"statusCodeValue":302,"body":{"usage":{"credits":0}}}',
                              "--format", "json", "--official-credits", "0")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["official_comparison"]["state"], "sample_unconfirmed")

    def test_invalid_official_value_has_safe_error_and_no_report(self):
        result = self.run_cli(b'{"usage":{"credits":0}}', "--format", "json", "--official-credits", "private-sentinel")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("private-sentinel", result.stderr)

    def test_default_report_has_no_implicit_official_verification(self):
        result = self.run_cli(b'{"usage":{"credits":0}}', "--format", "json")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("official_comparison", json.loads(result.stdout))
        self.assertEqual(result.stderr, "")

    def test_gzip_stdin_supports_comparison_and_summary(self):
        payload = gzip.compress(b'data: {"usage":{"credits":0.125}}\n\ndata: [DONE]\n\n', mtime=0)
        result = subprocess.run([sys.executable, "-m", "server.qoder_cloud.probe", "--input", "-",
                                 "--content-encoding", "gzip", "--official-credits", "0.125", "--summary"],
                                input=payload, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["official_comparison"]["state"], "matched")
        self.assertIn(b"sse_done", result.stderr)

    def test_output_report_contains_comparison_and_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "synthetic.json", Path(directory) / "report.json"
            source.write_bytes(b'{"usage":{"credits":0.125}}')
            options = [sys.executable, "-m", "server.qoder_cloud.probe", "--input", str(source),
                       "--format", "json", "--official-credits", "0.125", "--output", str(output), "--summary"]
            result = subprocess.run(options, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            before = output.read_bytes()
            self.assertEqual(json.loads(before)["official_comparison"]["state"], "matched")
            self.assertEqual(subprocess.run(options, capture_output=True, check=False).returncode, 1)
            self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
