"""Offline regression tests: these fixtures are synthetic, not live receipts."""

from decimal import Decimal
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from server.qoder_cloud.protocol import ProtocolError, SSEDecoder, UsageCollector, decode_json, sample
from server.qoder_cloud.usage import InvalidUsage, MAX_LEDGER_UNITS, normalize_usage


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


if __name__ == "__main__":
    unittest.main()
