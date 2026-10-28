"""Offline regression tests for the Windows P0 helper.

These tests never call DPAPI, never touch the real certificate store or the
user's IDE database, and never open a socket: the container, report and
network layers are replaced with local fakes and temporary directories.
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib import error as urlerror

from server.qoder_cloud import windows_p0
from server.qoder_cloud.windows_p0 import (
    ConnectHandler,
    ValidationError,
    account_check,
    account_inventory,
    decode_ide_secret,
    endpoint_check,
    extract_token,
    main,
    prepare,
    read_account_endpoint,
    secret_text,
    select_candidate,
)

PROTECTED = b"p0-offline:"


def fake_dpapi(data, decrypt=False):
    """Reversible stand-in for current-user DPAPI; no crypt32 call is made."""
    if decrypt:
        if not data.startswith(PROTECTED):
            raise ValidationError("当前用户 DPAPI 操作失败")
        return data[len(PROTECTED):]
    return PROTECTED + data


def buffer_value(clear, prefix=b"v10"):
    return json.dumps({"type": "Buffer", "data": list(prefix + fake_dpapi(clear))})


def raw_container(payload):
    """未经假 DPAPI 包装的原始容器，用于验证解密降级路径。"""
    return json.dumps({"type": "Buffer", "data": list(payload)})


class Recorder:
    def __init__(self):
        self.reports = {}
        self.seen = []

    def report(self, root, name, value):
        self.reports[name] = value

    def dpapi(self, data, decrypt=False):
        self.seen.append((data, decrypt))
        return fake_dpapi(data, decrypt)


def secret_row(clear, extension="aicoding.qoder", key="qoderAuthToken"):
    return "secret://" + json.dumps({"extensionId": extension, "key": key}), buffer_value(clear)


def build_state_database(path, rows):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    connection.executemany("INSERT INTO ItemTable (key, value) VALUES (?, ?)", rows)
    connection.commit()
    connection.close()


class SecretTextTests(unittest.TestCase):
    def test_bom_utf16_is_detected(self):
        self.assertEqual(secret_text(b"\xff\xfe" + "账号".encode("utf-16-le")), ("账号", "utf-16"))

    def test_utf16le_without_bom_is_detected(self):
        payload = json.dumps({"access_token": "a" * 24}).encode("utf-16-le")
        self.assertEqual(secret_text(payload), (payload.decode("utf-16-le"), "utf-16-le"))

    def test_utf8_payload_is_not_taken_as_utf16(self):
        payload = json.dumps({"token": "b" * 24}).encode("utf-8")
        self.assertEqual(secret_text(payload), (payload.decode("utf-8"), "utf-8-sig"))

    def test_short_payload_falls_back_to_utf8(self):
        self.assertEqual(secret_text(b"abc"), ("abc", "utf-8-sig"))


class DecodeSecretTests(unittest.TestCase):
    def test_utf16le_without_bom_container_is_decoded(self):
        clear = json.dumps({"access_token": "c" * 32}).encode("utf-16-le")
        metadata = {}
        with mock.patch.object(windows_p0, "dpapi", fake_dpapi):
            value = decode_ide_secret(buffer_value(clear), metadata)
        self.assertEqual(value, {"access_token": "c" * 32})
        self.assertEqual(metadata["decoded_encoding"], "utf-16-le")
        self.assertFalse(metadata["utf16_bom"])
        self.assertEqual(metadata["dpapi_decrypted_bytes"], len(clear))

    def test_version_prefix_is_stripped_before_dpapi(self):
        recorder = Recorder()
        clear = json.dumps({"token": "d" * 20}).encode("utf-8")
        with mock.patch.object(windows_p0, "dpapi", recorder.dpapi):
            decode_ide_secret(buffer_value(clear, prefix=b"v10"))
        self.assertEqual(recorder.seen, [(fake_dpapi(clear), True)])

    def test_bare_token_fallback(self):
        clear = b"e" * 32
        with mock.patch.object(windows_p0, "dpapi", fake_dpapi):
            self.assertEqual(decode_ide_secret(buffer_value(clear)), {"token": clear.decode()})

    def test_unknown_content_is_rejected(self):
        for clear in (b"", b"\x00\x01\x02", b"short", b"with space and\nnewline"):
            with self.subTest(clear=clear):
                with mock.patch.object(windows_p0, "dpapi", fake_dpapi):
                    with self.assertRaises(ValidationError):
                        decode_ide_secret(buffer_value(clear))

    def test_invalid_containers_are_rejected(self):
        payloads = (
            "not json",
            json.dumps({"type": "String", "data": []}),
            json.dumps({"type": "Buffer", "data": "abc"}),
            json.dumps({"type": "Buffer", "data": [1, True]}),
            json.dumps({"type": "Buffer", "data": [1, 300]}),
            json.dumps({"type": "Buffer", "data": [1, -1]}),
        )
        for payload in payloads:
            with self.subTest(payload=payload[:32]):
                with mock.patch.object(windows_p0, "dpapi", fake_dpapi):
                    with self.assertRaises(ValidationError):
                        decode_ide_secret(payload)

    def test_dpapi_failure_is_reported_not_echoed(self):
        with mock.patch.object(windows_p0, "dpapi", fake_dpapi):
            with self.assertRaises(ValidationError) as caught:
                decode_ide_secret(json.dumps({"type": "Buffer", "data": list(b"v10plain")}))
        self.assertIn("DPAPI", str(caught.exception))
        self.assertNotIn("plain", str(caught.exception))


class TokenExtractionTests(unittest.TestCase):
    def test_direct_and_nested_tokens(self):
        token = "f" * 32
        self.assertEqual(extract_token({"access_token": token}), token)
        self.assertEqual(extract_token({"security_oauth_token": token}), token)
        self.assertEqual(extract_token({"account": {"auth": {"token": token}}}), token)
        self.assertEqual(extract_token({"credentials": [token]}), None)

    def test_depth_limit_is_bounded(self):
        value = {"token": "g" * 32}
        for _ in range(5):
            value = {"account": value}
        self.assertIsNone(extract_token(value))

    def test_rejects_untrusted_shapes(self):
        self.assertIsNone(extract_token("h" * 32))
        self.assertIsNone(extract_token({"token": "short"}))
        self.assertIsNone(extract_token({"token": "i" * 32 + "\r\n"}))
        self.assertIsNone(extract_token({"token": "j" * 20000}))
        self.assertIsNone(extract_token({"token": 12345}))


class EndpointTests(unittest.TestCase):
    def test_endpoint_allowlist(self):
        for endpoint in ("https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation",
                         "http://openapi.qoder.sh/api/v2/user/plan",
                         "https://openapi.qoder.sh/api/v2/user/plan?x=1"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValidationError):
                    read_account_endpoint("token", endpoint)

    def test_success_response_is_parsed_without_echoing_token(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exit):
                return False

            def read(self, size=-1):
                return b'{"data":{"plan_tier_name":"Pro"}}'

        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch.object(windows_p0.urlrequest, "build_opener", return_value=opener):
            status, payload = read_account_endpoint("SECRET-TOKEN", "https://openapi.qoder.sh/api/v2/user/plan")
        self.assertEqual(status, {"http_status": 200, "valid_json": True})
        self.assertEqual(payload["data"]["plan_tier_name"], "Pro")
        self.assertNotIn("SECRET-TOKEN", json.dumps(status))
        self.assertIn("SECRET-TOKEN", opener.open.call_args.args[0].get_header("Authorization"))

    def test_oversized_response_is_not_parsed(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exit):
                return False

            def read(self, size=-1):
                return b"x" * (2 * 1024 * 1024 + 1)

        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch.object(windows_p0.urlrequest, "build_opener", return_value=opener):
            status, payload = read_account_endpoint("token", "https://openapi.qoder.sh/api/v2/quota/usage")
        self.assertEqual(status, {"http_status": 200, "valid_json": False})
        self.assertIsNone(payload)

    def test_http_and_network_failures_are_folded(self):
        for failure, expected in (
                (urlerror.HTTPError("https://openapi.qoder.sh", 401, "Unauthorized", {}, None),
                 {"http_status": 401, "valid_json": False}),
                (urlerror.URLError(OSError("offline")),
                 {"http_status": None, "valid_json": False, "network_error": True})):
            with self.subTest(failure=type(failure).__name__):
                opener = mock.Mock()
                opener.open.side_effect = failure
                with mock.patch.object(windows_p0.urlrequest, "build_opener", return_value=opener):
                    status, payload = read_account_endpoint("token", "https://openapi.qoder.sh/api/v2/user/plan")
                self.assertEqual(status, expected)
                self.assertIsNone(payload)


class FakeRequest:
    def __init__(self, payload):
        self.buffer = bytearray(payload)
        self.sent = bytearray()
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def recv(self, size):
        chunk = bytes(self.buffer[:size])
        del self.buffer[:size]
        return chunk

    def sendall(self, data):
        self.sent.extend(data)


class FakeUpstream:
    def __enter__(self):
        return self

    def __exit__(self, *exit):
        return False


class ConnectAllowlistTests(unittest.TestCase):
    def test_foreign_target_is_forbidden(self):
        request = FakeRequest(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        with mock.patch.object(windows_p0.socket, "create_connection",
                               mock.Mock(side_effect=AssertionError("upstream must not be dialled"))):
            ConnectHandler(request, ("127.0.0.1", 1), None)
        self.assertTrue(bytes(request.sent).startswith(b"HTTP/1.1 403"))
        self.assertEqual(request.timeout, 10)

    def test_loopback_target_is_relayed(self):
        request = FakeRequest(b"CONNECT 127.0.0.1:15889 HTTP/1.1\r\nHost: 127.0.0.1:15889\r\n\r\n")
        dialled = mock.Mock(return_value=FakeUpstream())
        with mock.patch.object(windows_p0.socket, "create_connection", dialled), \
                mock.patch.object(windows_p0.select, "select", return_value=([], [], [])):
            ConnectHandler(request, ("127.0.0.1", 1), None)
        self.assertTrue(bytes(request.sent).startswith(b"HTTP/1.1 200 Connection Established"))
        dialled.assert_called_once_with(("127.0.0.1", 15889), timeout=10)


class OsCryptTests(unittest.TestCase):
    def test_v10_falls_back_to_os_crypt_when_dpapi_fails(self):
        clear = json.dumps({"platform": "windows"}).encode("utf-8")
        metadata = {}
        fallback = mock.Mock(return_value=clear)
        with mock.patch.object(windows_p0, "dpapi", fake_dpapi), \
                mock.patch.object(windows_p0, "os_crypt_decrypt", fallback):
            value = decode_ide_secret(raw_container(b"v10" + b"cipher-body"), metadata, Path("unused"))
        self.assertEqual(value, {"platform": "windows"})
        self.assertEqual(metadata["container_scheme"], "os_crypt_aes_gcm")
        fallback.assert_called_once()

    def test_dpapi_is_preferred_and_never_triggers_os_crypt(self):
        clear = json.dumps({"token": "q" * 24}).encode("utf-8")
        metadata = {}
        fallback = mock.Mock(side_effect=AssertionError("os_crypt must not run when DPAPI works"))
        with mock.patch.object(windows_p0, "dpapi", fake_dpapi), \
                mock.patch.object(windows_p0, "os_crypt_decrypt", fallback):
            decode_ide_secret(buffer_value(clear), metadata, Path("unused"))
        self.assertEqual(metadata["container_scheme"], "dpapi")
        fallback.assert_not_called()

    def test_dpapi_failure_without_key_source_is_not_silently_degraded(self):
        with mock.patch.object(windows_p0, "dpapi", fake_dpapi):
            with self.assertRaises(ValidationError) as caught:
                decode_ide_secret(raw_container(b"v10" + b"cipher-body"))
        self.assertIn("DPAPI", str(caught.exception))

    def test_short_ciphertext_is_rejected(self):
        with self.assertRaises(ValidationError):
            windows_p0.os_crypt_decrypt(b"short", Path("unused"))

    def test_desktop_auth_rejects_unversioned_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "auth.v1.dat"
            path.write_bytes(b"plain-text")
            with self.assertRaises(ValidationError):
                windows_p0.decode_desktop_auth(Path(temporary), path)

    def test_key_loader_requires_dpapi_wrapped_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "Local State").write_text(json.dumps({"os_crypt": {"audit_enabled": True}}),
                                                encoding="utf-8")
            with self.assertRaises(ValidationError):
                windows_p0.os_crypt_key(source)
            (source / "Local State").write_text(json.dumps({"os_crypt": {"encrypted_key": "bm90LWRwYXBp"}}),
                                                encoding="utf-8")
            with self.assertRaises(ValidationError):
                windows_p0.os_crypt_key(source)

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "需要 cryptography 才能跑真实 AES-GCM")
    def test_real_aes_gcm_round_trip(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = bytes(range(32))
        nonce = bytes(range(12))
        clear = json.dumps({"access_token": "r" * 32}).encode("utf-16-le")
        blob = nonce + AESGCM(key).encrypt(nonce, clear, None)
        with mock.patch.object(windows_p0, "os_crypt_key", return_value=key):
            self.assertEqual(windows_p0.os_crypt_decrypt(blob, Path("unused")), clear)
        with mock.patch.object(windows_p0, "os_crypt_key", return_value=bytes(32)):
            with self.assertRaises(ValidationError):
                windows_p0.os_crypt_decrypt(blob, Path("unused"))


class InventoryTests(unittest.TestCase):
    def test_containers_are_described_without_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            appdata = root / "appdata"
            storage = appdata / "Qoder/User/globalStorage/storage.json"
            storage.parent.mkdir(parents=True)
            storage.write_text(json.dumps({"token": "SECRET-VALUE", "theme": "dark"}), encoding="utf-8")
            database = appdata / "Qoder/User/globalStorage/state.vscdb"
            build_state_database(database, [
                secret_row(b"BASE64BLOB-CONTENT"),
                ("secret://" + json.dumps({"extensionId": "aicoding.other", "key": "x"}),
                 json.dumps({"value": "SECRET-VALUE-2"})),
                ("qoder.auth.credential", json.dumps({"accessToken": "SECRET-VALUE"})),
                ("editor.fontSize", "14"),
            ])
            recorder = Recorder()
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "report", recorder.report):
                status = account_inventory(root)
            rendered = json.dumps(status, ensure_ascii=False)
            self.assertNotIn("SECRET-VALUE", rendered)
            self.assertNotIn("BASE64BLOB-CONTENT", rendered)
            self.assertNotIn("SECRET-VALUE-2", rendered)
            self.assertEqual(status["upstream_requests"], 0)
            self.assertFalse(status["existing_login_modified"])
            storage_item = status["account_sources"][0]
            self.assertEqual(storage_item["recognized_top_level_fields"], ["token"])
            summary = status["account_sources"][-1]
            self.assertEqual(summary["credential_entries"], 2)
            self.assertEqual(summary["auth_entries"], 1)
            safe = summary["safe_container_metadata"][0]
            self.assertTrue(safe["qoder_extension"])
            self.assertEqual(safe["secret_field_category"], "account_related")
            self.assertFalse(safe["has_base64_value"])
            self.assertEqual(safe["recognized_fields"], ["data", "type"])
            self.assertTrue(summary["safe_container_metadata"][1]["has_base64_value"])
            self.assertEqual(summary["safe_container_metadata"][1]["secret_field_category"], "other")
            self.assertEqual(summary["auth_record_fields"], {"accessToken": "str"})
            self.assertIn("account-inventory.json", recorder.reports)

    def test_unreadable_value_is_counted_as_opaque(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            appdata = root / "appdata"
            database = appdata / "Qoder/User/globalStorage/state.vscdb"
            database.parent.mkdir(parents=True)
            build_state_database(database, [("secret://" + json.dumps({"extensionId": "aicoding.x"}), "not-json")])
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_inventory(root)
            safe = status["account_sources"][-1]["safe_container_metadata"][0]
            self.assertEqual(safe["value_type"], "opaque")

    def test_missing_database_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.dict(os.environ, {"APPDATA": str(root / "appdata")}), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_inventory(root)
            self.assertFalse(status["account_sources"][-1]["exists"])
            self.assertFalse(status["account_imported"])


class CandidateSelectionTests(unittest.TestCase):
    """排序键的优先顺序：字段完整度 > 写入时间 > 固定来源顺序。"""

    def item(self, token, label, fields=1, modified=0.0):
        snapshot = {"token": token}
        for index in range(fields - 1):
            snapshot[f"field{index}"] = "x"
        return (token, snapshot, label, modified)

    def test_richer_snapshot_ranks_first(self):
        rich = self.item("t", "com.qodercn.app.stable", fields=3)
        lean = self.item("t", "com.qoder.app.stable", fields=1, modified=10 ** 9)
        chosen, blocked = select_candidate([lean, rich])
        self.assertIsNone(blocked)
        self.assertIs(chosen, rich)

    def test_recency_breaks_richness_tie(self):
        old = self.item("t", "com.qoder.app.stable", modified=100.0)
        new = self.item("t", "com.qodercn.app.stable", modified=200.0)
        chosen, blocked = select_candidate([old, new])
        self.assertIsNone(blocked)
        self.assertIs(chosen, new)

    def test_source_priority_breaks_remaining_tie(self):
        ide = self.item("t", "ide_secret", modified=100.0)
        desktop = self.item("t", "com.qoder.app.stable", modified=100.0)
        chosen, _ = select_candidate([ide, desktop])
        self.assertIs(chosen, desktop)

    def test_distinct_tokens_block_automatic_selection(self):
        chosen, blocked = select_candidate([self.item("a", "com.qoder.app.stable"),
                                            self.item("b", "com.qodercn.app.stable")])
        self.assertIsNone(chosen)
        self.assertEqual(blocked, "multiple_distinct_identities")

    def test_requested_source_selects_or_reports_missing(self):
        first = self.item("a", "com.qoder.app.stable")
        second = self.item("b", "com.qodercn.app.stable")
        chosen, blocked = select_candidate([first, second], "com.qodercn.app.stable")
        self.assertIsNone(blocked)
        self.assertIs(chosen, second)
        chosen, blocked = select_candidate([first], "nope")
        self.assertIsNone(chosen)
        self.assertEqual(blocked, "requested_source_not_available")


class AccountCheckTests(unittest.TestCase):
    def prepare_environment(self, temporary, rows):
        appdata = Path(temporary) / "appdata"
        database = appdata / "Qoder/User/globalStorage/state.vscdb"
        database.parent.mkdir(parents=True)
        build_state_database(database, rows)
        return appdata

    def test_missing_database_skips_every_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.dict(os.environ, {"APPDATA": str(root / "appdata")}), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root)
            self.assertFalse(status["usable_credential_verified"])
            self.assertEqual(status["read_only_checks"], [])
            self.assertEqual(status["upstream_inference_requests"], 0)

    def test_multiple_candidates_are_not_verified_automatically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = json.dumps({"access_token": "k" * 32}).encode("utf-16-le")
            second = json.dumps({"access_token": "m" * 32}).encode("utf-8")
            appdata = self.prepare_environment(temporary, [secret_row(first, key="loginToken"),
                                                           secret_row(second, key="refreshToken")])
            endpoint = mock.Mock(side_effect=AssertionError("no account may be verified automatically"))
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "dpapi", fake_dpapi), \
                    mock.patch.object(windows_p0, "read_account_endpoint", endpoint), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root)
            self.assertEqual(status["candidate_count"], 2)
            self.assertFalse(status["usable_credential_verified"])
            self.assertEqual(status["read_only_checks"], [])
            endpoint.assert_not_called()

    def test_single_candidate_is_verified_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clear = json.dumps({"access_token": "n" * 32}).encode("utf-16-le")
            appdata = self.prepare_environment(temporary, [secret_row(clear)])

            def endpoint(token, url):
                self.assertEqual(token, "n" * 32)
                if url.endswith("/user/plan"):
                    return {"http_status": 200, "valid_json": True}, {"data": {"plan_tier_name": "Pro"}}
                return {"http_status": 200, "valid_json": True}, {"data": {"userQuota": {"remaining": "0.5"}}}

            recorder = Recorder()
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "dpapi", recorder.dpapi), \
                    mock.patch.object(windows_p0, "read_account_endpoint", endpoint), \
                    mock.patch.object(windows_p0, "protect_directory"), \
                    mock.patch.object(windows_p0, "report", recorder.report):
                status = account_check(root)
            rendered = json.dumps(status, ensure_ascii=False)
            self.assertNotIn("n" * 32, rendered)
            self.assertTrue(status["usable_credential_verified"])
            self.assertEqual(status["upstream_remaining_micro"], 500000)
            self.assertEqual([check["endpoint"] for check in status["read_only_checks"]],
                             ["user_plan", "quota_usage"])
            self.assertTrue((root / "account-snapshot.dpapi").exists())
            self.assertFalse(status["token_refreshed"])
            self.assertFalse(status["existing_login_modified"])

    def test_desktop_auth_container_is_verified_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            appdata = Path(temporary) / "appdata"
            source = appdata / "com.qoder.app.stable"
            source.mkdir(parents=True)
            (source / "auth.v1.dat").write_bytes(b"v10" + b"cipher")
            snapshot = {"token": "s" * 32, "refreshToken": "u" * 32, "user": {"id": "1"}}
            endpoint = mock.Mock(side_effect=[
                ({"http_status": 200, "valid_json": True}, {"data": {"plan_tier_name": "Pro"}}),
                ({"http_status": 200, "valid_json": True}, {"data": {"userQuota": {"remaining": "2"}}})])
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "decode_desktop_auth",
                                      return_value=(snapshot, {"container_scheme": "os_crypt_aes_gcm"})), \
                    mock.patch.object(windows_p0, "read_account_endpoint", endpoint), \
                    mock.patch.object(windows_p0, "protect_directory"), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root)
            self.assertEqual(status["candidate_count"], 1)
            self.assertEqual(status["candidate_sources"], ["com.qoder.app.stable"])
            self.assertEqual(status["verified_source"], "com.qoder.app.stable")
            self.assertTrue(status["usable_credential_verified"])
            entry = status["desktop_auth_containers"][0]
            self.assertEqual(entry["recognized_account_fields"], ["refreshToken", "token", "user"])
            self.assertTrue(entry["token_extractable"])
            self.assertTrue(entry["usable_credential_verified"])
            rendered = json.dumps(status, ensure_ascii=False)
            self.assertNotIn("s" * 32, rendered)
            self.assertNotIn("u" * 32, rendered)
            self.assertFalse(status["token_refreshed"])

    def desktop_environment(self, temporary, snapshots, ages=None):
        """建两个桌面 App 凭据容器，解码结果由 label 直接给定，绕开真实密钥链。"""
        appdata = Path(temporary) / "appdata"
        ages = ages or {}
        for index, label in enumerate(snapshots):
            source = appdata / label
            source.mkdir(parents=True)
            path = source / "auth.v1.dat"
            path.write_bytes(b"v10" + b"cipher")
            # 默认让先列出的容器最新，避免测试依赖真实时钟。
            moment = ages.get(label, 1_700_000_000 - index * 1000)
            os.utime(path, (moment, moment))

        def decode(source, path):
            return snapshots[source.name], {"container_scheme": "os_crypt_aes_gcm"}
        return appdata, decode

    def test_same_token_in_two_containers_is_verified_once(self):
        snapshots = {"com.qoder.app.stable": {"token": "t" * 32},
                     "com.qodercn.app.stable": {"token": "t" * 32}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            appdata, decode = self.desktop_environment(temporary, snapshots)
            endpoint = mock.Mock(side_effect=[
                ({"http_status": 200, "valid_json": True}, {"data": {"plan_tier_name": "Pro"}}),
                ({"http_status": 200, "valid_json": True}, {"data": {"userQuota": {"remaining": "3"}}})])
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "decode_desktop_auth", decode), \
                    mock.patch.object(windows_p0, "read_account_endpoint", endpoint), \
                    mock.patch.object(windows_p0, "protect_directory"), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root)
            # 同一 token 只是冗余副本，不是两个账号。
            self.assertEqual(status["candidate_count"], 2)
            self.assertEqual(status["distinct_identity_count"], 1)
            self.assertEqual(status["verified_source"], "com.qoder.app.stable")
            self.assertNotIn("verification_blocked", status)
            self.assertTrue(status["usable_credential_verified"])
            self.assertEqual(endpoint.call_count, 2)

    def test_richest_snapshot_wins_within_one_identity(self):
        # 同一 token 的两个副本字段完整度不同：优先留档字段更齐的那份。
        snapshots = {"com.qoder.app.stable": {"token": "t" * 32},
                     "com.qodercn.app.stable": {"token": "t" * 32, "refreshToken": "r" * 32,
                                                "user": {"id": "1"}}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            appdata, decode = self.desktop_environment(temporary, snapshots)
            endpoint = mock.Mock(side_effect=[
                ({"http_status": 200, "valid_json": True}, {"data": {"plan_tier_name": "Pro"}}),
                ({"http_status": 200, "valid_json": True}, {"data": {"userQuota": {"remaining": "1"}}})])
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "decode_desktop_auth", decode), \
                    mock.patch.object(windows_p0, "read_account_endpoint", endpoint), \
                    mock.patch.object(windows_p0, "protect_directory"), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root)
            self.assertEqual(status["distinct_identity_count"], 1)
            self.assertEqual(status["verified_source"], "com.qodercn.app.stable")

    def test_newest_copy_wins_when_richness_is_equal(self):
        snapshots = {"com.qoder.app.stable": {"token": "t" * 32},
                     "com.qodercn.app.stable": {"token": "t" * 32}}
        ages = {"com.qoder.app.stable": 1_600_000_000, "com.qodercn.app.stable": 1_700_000_000}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            appdata, decode = self.desktop_environment(temporary, snapshots, ages)
            endpoint = mock.Mock(side_effect=[
                ({"http_status": 200, "valid_json": True}, {"data": {"plan_tier_name": "Pro"}}),
                ({"http_status": 200, "valid_json": True}, {"data": {"userQuota": {"remaining": "1"}}})])
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "decode_desktop_auth", decode), \
                    mock.patch.object(windows_p0, "read_account_endpoint", endpoint), \
                    mock.patch.object(windows_p0, "protect_directory"), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root)
            self.assertEqual(status["distinct_identity_count"], 1)
            self.assertEqual(status["verified_source"], "com.qodercn.app.stable")
            entries = {entry["source"]: entry for entry in status["desktop_auth_containers"]}
            self.assertEqual(entries["com.qodercn.app.stable"]["data_directory_files"], 1)
            self.assertIsNotNone(entries["com.qoder.app.stable"]["container_modified_at"])

    def test_desktop_source_outranks_ide_secret_for_same_identity(self):
        # 同一 token 同时出现在 IDE 插件密钥和桌面 App 容器里时，以桌面容器为准。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clear = json.dumps({"token": "t" * 32}).encode("utf-16-le")
            appdata = self.prepare_environment(temporary, [secret_row(clear)])
            source = appdata / "com.qoder.app.stable"
            source.mkdir(parents=True)
            (source / "auth.v1.dat").write_bytes(b"v10" + b"cipher")
            endpoint = mock.Mock(side_effect=[
                ({"http_status": 200, "valid_json": True}, {"data": {"plan_tier_name": "Pro"}}),
                ({"http_status": 200, "valid_json": True}, {"data": {"userQuota": {"remaining": "1"}}})])
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "dpapi", fake_dpapi), \
                    mock.patch.object(windows_p0, "decode_desktop_auth",
                                      return_value=({"token": "t" * 32, "refreshToken": "r" * 32},
                                                    {"container_scheme": "os_crypt_aes_gcm"})), \
                    mock.patch.object(windows_p0, "read_account_endpoint", endpoint), \
                    mock.patch.object(windows_p0, "protect_directory"), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root)
            self.assertEqual(status["distinct_identity_count"], 1)
            self.assertEqual(status["verified_source"], "com.qoder.app.stable")

    def test_distinct_identities_are_not_verified_automatically(self):
        snapshots = {"com.qoder.app.stable": {"token": "a" * 32},
                     "com.qodercn.app.stable": {"token": "b" * 32}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            appdata, decode = self.desktop_environment(temporary, snapshots)
            endpoint = mock.Mock(side_effect=AssertionError("distinct identities must be chosen explicitly"))
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "decode_desktop_auth", decode), \
                    mock.patch.object(windows_p0, "read_account_endpoint", endpoint), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root)
            self.assertEqual(status["candidate_count"], 2)
            self.assertEqual(status["distinct_identity_count"], 2)
            self.assertEqual(status["verification_blocked"], "multiple_distinct_identities")
            self.assertNotIn("verified_source", status)
            self.assertEqual(status["read_only_checks"], [])
            self.assertEqual([entry["usable_credential_verified"]
                              for entry in status["desktop_auth_containers"]], [False, False])
            endpoint.assert_not_called()

    def test_explicit_source_verifies_only_that_identity(self):
        snapshots = {"com.qoder.app.stable": {"token": "a" * 32},
                     "com.qodercn.app.stable": {"token": "b" * 32}}
        seen = []

        def endpoint(token, url):
            seen.append(token)
            if url.endswith("/user/plan"):
                return {"http_status": 200, "valid_json": True}, {"data": {"plan_tier_name": "Pro"}}
            return {"http_status": 200, "valid_json": True}, {"data": {"userQuota": {"remaining": "1"}}}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            appdata, decode = self.desktop_environment(temporary, snapshots)
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "decode_desktop_auth", decode), \
                    mock.patch.object(windows_p0, "read_account_endpoint", endpoint), \
                    mock.patch.object(windows_p0, "protect_directory"), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root, "com.qodercn.app.stable")
            self.assertEqual(status["verified_source"], "com.qodercn.app.stable")
            self.assertNotIn("verification_blocked", status)
            self.assertEqual(seen, ["b" * 32, "b" * 32])
            self.assertEqual([entry["source"] for entry in status["desktop_auth_containers"]
                              if entry["usable_credential_verified"]], ["com.qodercn.app.stable"])

    def test_unknown_requested_source_is_blocked(self):
        snapshots = {"com.qoder.app.stable": {"token": "a" * 32}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            appdata, decode = self.desktop_environment(temporary, snapshots)
            endpoint = mock.Mock(side_effect=AssertionError("unknown source must not be verified"))
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "decode_desktop_auth", decode), \
                    mock.patch.object(windows_p0, "read_account_endpoint", endpoint), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root, "com.example.other")
            self.assertEqual(status["verification_blocked"], "requested_source_not_available")
            self.assertFalse(status["usable_credential_verified"])
            endpoint.assert_not_called()

    def test_undecodable_secret_is_reported_as_unsupported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            appdata = self.prepare_environment(
                temporary, [("secret://" + json.dumps({"extensionId": "aicoding.qoder"}), "not-json")])
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "dpapi", fake_dpapi), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root)
            self.assertTrue(status["recognized_qoder_secret"])
            self.assertEqual(status["secret_decode_state"], "unsupported_container")
            self.assertEqual(status["candidate_count"], 0)

    def test_foreign_or_malformed_rows_do_not_pollute_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clear = json.dumps({"access_token": "p" * 32}).encode("utf-16-le")
            rows = [("secret://not-json", "{}"),
                    ("secret://" + json.dumps({"extensionId": "ms-python.python", "key": "token"}),
                     buffer_value(b"z" * 16)),
                    secret_row(clear)]
            appdata = self.prepare_environment(temporary, rows)
            endpoint = mock.Mock(side_effect=[
                ({"http_status": 200, "valid_json": True}, {"data": {"plan_tier_name": "Pro"}}),
                ({"http_status": 200, "valid_json": True}, {"data": {"userQuota": {"remaining": "1"}}})])
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "dpapi", fake_dpapi), \
                    mock.patch.object(windows_p0, "read_account_endpoint", endpoint), \
                    mock.patch.object(windows_p0, "protect_directory"), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root)
            self.assertNotIn("secret_decode_state", status)
            self.assertEqual(status["candidate_count"], 1)
            self.assertEqual(status["decoded_qoder_container_type"], "dict")
            self.assertEqual(status["recognized_account_fields"], ["access_token"])
            self.assertTrue(status["usable_credential_verified"])

    def test_decoded_but_unrecognised_content_has_its_own_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clear = b"plain text, not a token or json"
            appdata = self.prepare_environment(temporary, [secret_row(clear)])
            with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}), \
                    mock.patch.object(windows_p0, "dpapi", fake_dpapi), \
                    mock.patch.object(windows_p0, "report", Recorder().report):
                status = account_check(root)
            self.assertEqual(status["secret_decode_state"], "decoded_content_not_supported")
            self.assertEqual(status["candidate_count"], 0)


def region_cache(path, fetched=None, **nodes):
    """写一份只含节点字段的区域配置缓存；不写任何身份信息。"""
    content = {key: {"endpoint": value} for key, value in nodes.items() if value}
    payload = {"version": "offline-test", "content": content}
    if fetched is not None:
        payload["fetchTime"] = fetched
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class EndpointCheckTests(unittest.TestCase):
    def run_check(self, temporary, **nodes):
        root = Path(temporary) / "root"
        cache = region_cache(Path(temporary) / "region-config-cache.json", **nodes)
        return endpoint_check(root, cache)

    def test_matching_nodes_stay_within_the_default_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = self.run_check(temporary, preferredInferenceNode="https://api3.qoder.sh",
                                    preferredNesNode="https://api5.qoder.com.cn",
                                    preferredCodebaseNode="https://api2.qoder.sh")
        self.assertTrue(status["within_default_scope"])
        self.assertEqual(status["missing_billing_domains"], [])
        self.assertEqual(status["suggested_target_domains"], list(windows_p0.DEFAULT_TARGET_DOMAINS))

    def test_billing_node_drift_is_reported_but_not_applied(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = self.run_check(temporary, preferredInferenceNode="https://api7.qoder.sh",
                                    preferredNesNode="https://api5.qoder.com.cn")
        self.assertFalse(status["within_default_scope"])
        self.assertEqual(status["missing_billing_domains"], ["api7.qoder.sh"])
        self.assertEqual(status["suggested_target_domains"][-1], "api7.qoder.sh")
        self.assertNotIn("api7.qoder.sh", status["default_target_domains"])

    def test_codebase_node_is_never_promoted_into_the_billing_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = self.run_check(temporary, preferredCodebaseNode="https://api2.qoder.sh")
        self.assertEqual(status["missing_billing_domains"], [])
        self.assertEqual(status["unbilled_domains_in_scope"], [])

    def test_unbilled_node_inside_the_scope_is_flagged(self):
        with mock.patch.object(windows_p0, "DEFAULT_TARGET_DOMAINS", ("api2.qoder.sh",)):
            with tempfile.TemporaryDirectory() as temporary:
                status = self.run_check(temporary, preferredCodebaseNode="https://api2.qoder.sh")
        self.assertEqual(status["unbilled_domains_in_scope"], ["api2.qoder.sh"])

    def test_stale_cache_is_marked_after_the_refresh_window(self):
        old = int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp() * 1000)
        with tempfile.TemporaryDirectory() as temporary:
            status = self.run_check(temporary, fetched=old, preferredInferenceNode="https://api3.qoder.sh")
        self.assertTrue(status["region_cache_stale"])
        self.assertGreater(status["region_cache_age_seconds"], 1800)

    def test_fresh_cache_is_not_marked_stale(self):
        fresh = int(datetime.now(timezone.utc).timestamp() * 1000)
        with tempfile.TemporaryDirectory() as temporary:
            status = self.run_check(temporary, fetched=fresh, preferredInferenceNode="https://api3.qoder.sh")
        self.assertFalse(status["region_cache_stale"])
        self.assertIsNotNone(status["region_cache_fetched_at"])

    def test_missing_cache_is_reported_without_guessing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            status = endpoint_check(root, Path(temporary) / "absent.json")
            written = json.loads((root / "endpoint-check.json").read_text(encoding="utf-8"))
        self.assertFalse(status["region_cache_present"])
        self.assertIsNone(status["within_default_scope"])
        self.assertEqual(written["nodes"], [])
        self.assertEqual(written["suggested_target_domains"], list(windows_p0.DEFAULT_TARGET_DOMAINS))

    def test_non_https_endpoint_is_not_treated_as_a_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = self.run_check(temporary, preferredInferenceNode="http://api9.qoder.sh",
                                    preferredNesNode="not a url")
        self.assertEqual([item["host"] for item in status["nodes"]], ["", "", ""])
        self.assertEqual(status["missing_billing_domains"], [])

    def test_default_scope_matches_the_rust_defaults(self):
        config = Path(__file__).resolve().parents[2] / "src-tauri" / "src" / "config.rs"
        if not config.is_file():
            self.skipTest("未找到 Rust 配置源")
        block = config.read_text(encoding="utf-8").split("DEFAULT_TARGET_DOMAINS", 1)[1].split("];", 1)[0]
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', block)), windows_p0.DEFAULT_TARGET_DOMAINS)


class FakePublicKey:
    def __init__(self, der):
        self.der = der

    def public_bytes(self, encoding, fmt):
        return self.der


class FakeCertificate:
    def __init__(self, expires, der=b"same"):
        self.not_valid_after_utc = expires
        self.der = der

    def public_key(self):
        return FakePublicKey(self.der)


class PrepareGuardTests(unittest.TestCase):
    def run_prepare(self, root, certificate, key_der=b"same"):
        serialization = types.SimpleNamespace(
            Encoding=types.SimpleNamespace(DER="der"),
            PublicFormat=types.SimpleNamespace(SubjectPublicKeyInfo="spki"),
            load_pem_private_key=lambda data, password=None:
                types.SimpleNamespace(public_key=lambda: FakePublicKey(key_der)))
        crypto = (types.SimpleNamespace(load_pem_x509_certificate=lambda data: certificate),
                  None, serialization, None)
        with mock.patch.object(windows_p0, "protect_directory"), \
                mock.patch.object(windows_p0, "crypto", return_value=crypto), \
                mock.patch.object(windows_p0, "dpapi", fake_dpapi):
            return prepare(root)

    def test_incomplete_identity_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ca_dir = root / "proxy-ca"
            ca_dir.mkdir()
            (ca_dir / "ca.crt").write_bytes(b"pem")
            with self.assertRaises(ValidationError) as caught:
                self.run_prepare(root, FakeCertificate(datetime.now(timezone.utc)))
            self.assertIn("不完整", str(caught.exception))

    def test_mismatched_key_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ca_dir = root / "proxy-ca"
            ca_dir.mkdir()
            (ca_dir / "ca.crt").write_bytes(b"pem")
            (ca_dir / "ca-key.dpapi").write_bytes(fake_dpapi(b"pem"))
            with self.assertRaises(ValidationError) as caught:
                self.run_prepare(root, FakeCertificate(datetime.now(timezone.utc), der=b"cert"), key_der=b"key")
            self.assertIn("不匹配", str(caught.exception))

    def test_expired_identity_is_refused_without_renewal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ca_dir = root / "proxy-ca"
            ca_dir.mkdir()
            (ca_dir / "ca.crt").write_bytes(b"pem")
            (ca_dir / "ca-key.dpapi").write_bytes(fake_dpapi(b"pem"))
            expired = datetime(2020, 1, 1, tzinfo=timezone.utc)
            with self.assertRaises(ValidationError) as caught:
                self.run_prepare(root, FakeCertificate(expired))
            self.assertIn("过期", str(caught.exception))


class CommandLineTests(unittest.TestCase):
    def run_main(self, argv, workspace):
        output = io.StringIO()
        with mock.patch.object(windows_p0, "WORKSPACE", workspace), \
                contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()

    def test_root_outside_local_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    self.run_main(["prepare", "--root", str(workspace / "elsewhere")], workspace)
            self.assertNotEqual(caught.exception.code, 0)

    def test_actions_are_dispatched(self):
        cases = {"prepare": ("prepare", []), "install": ("install", []),
                 "tls-check": ("tls_check", ["--electron", "ide.exe"]),
                 "account-inventory": ("account_inventory", []), "account-check": ("account_check", []),
                 "endpoint-check": ("endpoint_check", [])}
        for action, (target, extra) in cases.items():
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                root = workspace / ".local" / "windows-p0"
                called = mock.Mock(return_value={"diagnostic_only": True})
                with mock.patch.object(windows_p0, target, called):
                    code, printed = self.run_main([action, "--root", str(root)] + extra, workspace)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(printed), {"diagnostic_only": True})
                self.assertEqual(called.call_args.args[0], root.resolve())

    def test_source_flag_is_passed_to_account_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / ".local" / "windows-p0"
            called = mock.Mock(return_value={"diagnostic_only": True})
            with mock.patch.object(windows_p0, "account_check", called):
                code, printed = self.run_main(
                    ["account-check", "--root", str(root), "--source", "com.qoder.app.stable"], workspace)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(printed), {"diagnostic_only": True})
            self.assertEqual(called.call_args.args, (root.resolve(), "com.qoder.app.stable"))

    def test_failure_is_answered_with_fixed_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / ".local" / "windows-p0"
            with mock.patch.object(windows_p0, "prepare",
                                   mock.Mock(side_effect=ValidationError("private-path-leak"))):
                code, printed = self.run_main(["prepare", "--root", str(root)], workspace)
            self.assertEqual(code, 1)
            self.assertTrue(json.loads(printed)["failed"])
            self.assertNotIn("private-path-leak", printed)


if __name__ == "__main__":
    unittest.main()
