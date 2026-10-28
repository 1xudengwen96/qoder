"""Windows P0 实测工具：独立 CA、当前用户信任库、loopback TLS、只读账号盘点。

这是工程验证辅助工具，不是生产网关，不读取对话、不修改现用 IDE 配置。
证书操作需要 cryptography；离线协议工具不依赖它。私有数据只写 .local/。
"""

import argparse
import base64
import ctypes
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
import re
from pathlib import Path
import select
import socket
import socketserver
import sqlite3
import ssl
import subprocess
import threading
import uuid
from urllib import error as urlerror, request as urlrequest

WORKSPACE = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = WORKSPACE / ".local" / "windows-p0"


class ValidationError(ValueError):
    """错误只包含固定原因，不回显私钥、账号、响应或私人路径。"""


def crypto():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        return x509, hashes, serialization, ec
    except ImportError:
        raise ValidationError("缺少隔离环境中的 cryptography 依赖") from None


def dpapi(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise ValidationError("DPAPI 仅支持 Windows 当前用户")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    target = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        call = library.CryptUnprotectData
        call.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        call = library.CryptProtectData
        call.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), "Qoder Proxy P0 独立测试", None, None, None, 1, ctypes.byref(target))
    call.restype = wintypes.BOOL
    if not call(*args):
        raise ValidationError("当前用户 DPAPI 操作失败")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def write_new(path: Path, data: bytes):
    with path.open("xb") as target:
        target.write(data)


def report(root: Path, name: str, value: dict):
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def protect_directory(root: Path):
    """仅为新建测试目录设置当前用户与 SYSTEM 的访问权限，不改已有用户目录。"""
    if os.name != "nt":
        raise ValidationError("实测准备仅支持 Windows")
    root.mkdir(parents=True, exist_ok=True)
    code = "$i=[Security.Principal.WindowsIdentity]::GetCurrent(); $i.User.Value"
    sid = subprocess.run(["powershell", "-NoProfile", "-Command", code],
                         capture_output=True, text=True, check=True).stdout.strip()
    if not sid.startswith("S-1-") or not all(c in "S-0123456789" for c in sid):
        raise ValidationError("无法取得当前用户权限标识")
    result = subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r",
                             f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], capture_output=True)
    if result.returncode:
        raise ValidationError("测试目录权限设置失败")


def prepare(root: Path):
    protect_directory(root)
    x509, hashes, serialization, ec = crypto()
    ca_dir = root / "proxy-ca"
    ca_dir.mkdir(exist_ok=True)
    cert_path, key_path = ca_dir / "ca.crt", ca_dir / "ca-key.dpapi"
    if cert_path.exists() != key_path.exists():
        raise ValidationError("测试 CA 身份不完整，拒绝覆盖")
    now = datetime.now(timezone.utc)
    if cert_path.exists():
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(dpapi(key_path.read_bytes(), decrypt=True), password=None)
        if certificate.public_key().public_bytes(serialization.Encoding.DER,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo) != key.public_key().public_bytes(
                                                     serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
            raise ValidationError("测试 CA 与私钥不匹配")
        if certificate.not_valid_after_utc <= now:
            raise ValidationError("测试 CA 已过期，拒绝静默更新身份")
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME,
                                          "Qoder Proxy P0 Test CA " + uuid.uuid4().hex[:12])])
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                       .public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30))
                       .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                       .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                                   key_encipherment=False, data_encipherment=False,
                                                   key_agreement=False, key_cert_sign=True, crl_sign=True,
                                                   encipher_only=False, decipher_only=False), critical=True)
                       .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                       .sign(key, hashes.SHA256()))
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        write_new(key_path, dpapi(pem))
        write_new(cert_path, certificate.public_bytes(serialization.Encoding.PEM))
    leaf_cert, leaf_key = ca_dir / "loopback.crt", ca_dir / "loopback.key"
    if leaf_cert.exists() != leaf_key.exists():
        raise ValidationError("测试 leaf 身份不完整，拒绝覆盖")
    if not leaf_cert.exists():
        local_key = ec.generate_private_key(ec.SECP256R1())
        leaf = (x509.CertificateBuilder().subject_name(x509.Name([
                    x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "localhost")]))
                .issuer_name(certificate.subject).public_key(local_key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=7))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                          x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
        # 只持久化 loopback leaf 私钥，目录 ACL 受限；CA 私钥仅以 DPAPI 密文保存。
        write_new(leaf_key, local_key.private_bytes(serialization.Encoding.PEM,
                                                   serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        write_new(leaf_cert, leaf.public_bytes(serialization.Encoding.PEM))
    for name in ("ide-data/User", "extensions", "home", "appdata", "localappdata", "workspace"):
        (root / name).mkdir(parents=True, exist_ok=True)
    settings = root / "ide-data" / "User" / "settings.json"
    if not settings.exists():
        write_new(settings, json.dumps({"http.proxy": "http://127.0.0.1:15888", "http.proxyStrictSSL": True,
                                       "telemetry.telemetryLevel": "off", "update.mode": "none",
                                       "extensions.autoUpdate": False}, indent=2).encode())
    status = {"diagnostic_only": True, "ca_sha256": certificate.fingerprint(hashes.SHA256()).hex(),
              "ca_thumbprint": certificate.fingerprint(hashes.SHA1()).hex().upper(),
              "ca_expires_at": certificate.not_valid_after_utc.isoformat(),
              "ca_private_key_protection": "current_user_dpapi", "ports": [15888, 15889],
              "ide_started": False, "upstream_requests": 0}
    report(root, "preparation.json", status)
    return status


def install(root: Path):
    status = prepare(root)
    cert = root / "proxy-ca" / "ca.crt"
    completed = subprocess.run(["certutil", "-user", "-addstore", "Root", str(cert)], capture_output=True)
    if completed.returncode:
        raise ValidationError("当前用户 Root 证书安装失败")
    status["installed_current_user_root"] = any(
        hashlib.sha256(raw).hexdigest() == status["ca_sha256"]
        for raw, encoding, trust in ssl.enum_certificates("ROOT") if encoding == "x509_asn")
    if not status["installed_current_user_root"]:
        raise ValidationError("安装后未找到测试 CA 的精确指纹")
    report(root, "certificate-status.json", status)
    return status


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        content = b'{"synthetic":true,"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass


class ConnectHandler(socketserver.BaseRequestHandler):
    """仅连接本机固定 TLS 测试端口，拒绝所有外网目标。"""
    def handle(self):
        self.request.settimeout(10)
        header = bytearray()
        while b"\r\n\r\n" not in header and len(header) < 4096:
            data = self.request.recv(1)
            if not data:
                return
            header.extend(data)
        if header.split(b"\r\n", 1)[0] != b"CONNECT 127.0.0.1:15889 HTTP/1.1":
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        with socket.create_connection(("127.0.0.1", 15889), timeout=10) as upstream:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            while True:
                readable, _, _ = select.select([self.request, upstream], [], [], 10)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.request else self.request).sendall(data)


class ConnectServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # TLS 负例与连接取消属预期结果，不输出包含私人路径的线程 traceback。
        pass


def tls_check(root: Path, electron: str = ""):
    server = ThreadingHTTPServer(("127.0.0.1", 15889), HealthHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "proxy-ca" / "loopback.crt", root / "proxy-ca" / "loopback.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        proxy = ConnectServer(("127.0.0.1", 15888), ConnectHandler)
    except OSError:
        server.server_close()
        raise ValidationError("测试代理端口被占用") from None
    workers = [threading.Thread(target=item.serve_forever, daemon=True) for item in (server, proxy)]
    for worker in workers:
        worker.start()
    try:
        client_context = ssl.create_default_context()
        with socket.create_connection(("127.0.0.1", 15888), timeout=10) as connection:
            connection.sendall(b"CONNECT 127.0.0.1:15889 HTTP/1.1\r\nHost: 127.0.0.1:15889\r\n\r\n")
            header = bytearray()
            while b"\r\n\r\n" not in header:
                header.extend(connection.recv(1))
            if not header.startswith(b"HTTP/1.1 200"):
                raise ValidationError("本机 CONNECT 失败")
            with client_context.wrap_socket(connection, server_hostname="localhost") as secured:
                secured.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                result = secured.recv(4096)
                if not result.startswith(b"HTTP/1.0 200"):
                    raise ValidationError("本机 TLS 请求失败")
        status = {"diagnostic_only": True, "scope": "loopback_helper_not_rust_proxy",
                  "connect_ok": True, "windows_default_ca_tls_ok": True,
                  "tls_verification_disabled": False, "upstream_requests": 0}
        if electron:
            script = ("const tls=require('tls');const c=tls.connect({host:'127.0.0.1',port:15889,"
                      "servername:'localhost',rejectUnauthorized:true},()=>{"
                      "console.log(JSON.stringify({authorized:c.authorized}));c.end();});"
                      "c.setTimeout(5000,()=>c.destroy());c.on('error',e=>{"
                      "console.log(JSON.stringify({authorized:false,code:e.code}));process.exitCode=2;});")
            for system_ca in (False, True):
                environment = os.environ.copy()
                environment["ELECTRON_RUN_AS_NODE"] = "1"
                environment.pop("NODE_EXTRA_CA_CERTS", None)
                environment.pop("NODE_TLS_REJECT_UNAUTHORIZED", None)
                environment.pop("NODE_OPTIONS", None)
                environment.pop("NODE_USE_SYSTEM_CA", None)
                if system_ca:
                    environment["NODE_USE_SYSTEM_CA"] = "1"
                process = subprocess.run([electron, "-e", script], env=environment,
                                         capture_output=True, text=True, timeout=15)
                try:
                    value = json.loads(process.stdout.strip())
                except (json.JSONDecodeError, ValueError):
                    value = {"authorized": False, "code": "runtime_probe_unavailable"}
                status["ide_node_system_ca" if system_ca else "ide_node_default_ca"] = {
                    "authorized": value.get("authorized") is True,
                    "error_code": value.get("code") if value.get("code") in
                    ("UNABLE_TO_VERIFY_LEAF_SIGNATURE", "UNABLE_TO_GET_ISSUER_CERT_LOCALLY", "SELF_SIGNED_CERT_IN_CHAIN")
                    else None}
        report(root, "loopback-tls.json", status)
        return status
    finally:
        for item in (proxy, server):
            item.shutdown()
            item.server_close()
        for worker in workers:
            worker.join(timeout=3)


def account_inventory(root: Path):
    """只读盘点账号容器的存在性和已知字段，绝不输出值或完整数据库键。"""
    appdata = Path(os.environ["APPDATA"])
    sources = [("ide_storage", appdata / "Qoder/User/globalStorage/storage.json"),
               ("desktop_auth", appdata / "com.qoder.app.stable/auth.v1.dat"),
               ("legacy_pool", appdata / "Qoder/proxy-tokens.json")]
    items = []
    known = {"token", "access_token", "accessToken", "user_id", "userId", "email", "security_oauth_token",
             "auth", "account", "accounts", "encrypted", "value", "refreshToken", "machineToken", "type", "data"}
    for label, path in sources:
        item = {"source": label, "exists": path.exists(), "usable_credential_verified": False}
        if path.is_file():
            content = path.read_bytes()
            item["bytes"] = len(content)
            try:
                value = json.loads(content)
                item["json"] = True
                item["recognized_top_level_fields"] = sorted(known.intersection(value)) if isinstance(value, dict) else []
            except (ValueError, UnicodeError):
                item["json"] = False
        items.append(item)
    database = appdata / "Qoder/User/globalStorage/state.vscdb"
    summary = {"source": "ide_state_database", "exists": database.exists(), "credential_entries": 0,
               "auth_entries": 0, "usable_credential_verified": False}
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            for key, value in connection.execute("SELECT key, value FROM ItemTable"):
                if key.startswith("secret://"):
                    summary["credential_entries"] += 1
                    try:
                        descriptor = json.loads(key[len("secret://"):])
                    except ValueError:
                        descriptor = {}
                    extension = descriptor.get("extensionId", "")
                    item = {"qoder_extension": extension.startswith("aicoding."),
                            "value_type": "unknown", "recognized_fields": []}
                    try:
                        payload = json.loads(value)
                        item["value_type"] = type(payload).__name__
                        if isinstance(payload, dict):
                            item["recognized_fields"] = sorted(known.intersection(payload))
                            if item["qoder_extension"]:
                                item["secret_field_category"] = "account_related" if any(
                                    word in str(descriptor.get("key", "")).lower() for word in ("token", "auth", "login", "account")) else "other"
                            item["has_base64_value"] = isinstance(payload.get("value"), str)
                        elif isinstance(payload, str):
                            item["string_length"] = len(payload)
                    except (ValueError, UnicodeError):
                        item["value_type"] = "opaque"
                        item["bytes"] = len(value)
                    summary.setdefault("safe_container_metadata", []).append(item)
                elif any(word in key.lower() for word in ("auth", "credential", "cosy")):
                    summary["auth_entries"] += 1
                    try:
                        payload = json.loads(value)
                        if isinstance(payload, dict):
                            # 只输出纯字母字段名和类型，不输出身份、密文或正文。
                            summary["auth_record_fields"] = {name: type(field).__name__ for name, field in payload.items()
                                                             if re.fullmatch(r"[A-Za-z_]{1,64}", name)}
                    except (ValueError, UnicodeError):
                        summary["auth_record_json"] = False
        # 未经识别的加密格式不当成账号可用，也不提取或打印其内容。
    items.append(summary)
    status = {"diagnostic_only": True, "account_sources": items, "upstream_requests": 0,
              "existing_login_modified": False, "account_imported": False}
    report(root, "account-inventory.json", status)
    return status


def decode_ide_secret(value, metadata=None):
    """识别 Electron Windows safeStorage 容器，使用当前用户 DPAPI；不刷新凭据。"""
    container = json.loads(value)
    if metadata is not None:
        metadata["container_json"] = True
    if not isinstance(container, dict) or container.get("type") != "Buffer":
        raise ValidationError("未知账号容器")
    items = container.get("data")
    if not isinstance(items, list) or len(items) > 1024 * 1024 or not all(
            isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 255 for item in items):
        raise ValidationError("无效账号容器")
    encrypted = bytes(items)
    if encrypted.startswith(b"v10"):
        encrypted = encrypted[3:]
    clear = dpapi(encrypted, decrypt=True)
    if metadata is not None:
        metadata["dpapi_decrypted_bytes"] = len(clear)
        metadata["utf16_bom"] = clear.startswith((b"\xff\xfe", b"\xfe\xff"))
    # Electron Windows safeStorage 的版本差异：UTF-16LE 与 UTF-8 容器都需显式识别。
    if clear.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = clear.decode("utf-16")
    elif len(clear) % 2 == 0 and clear[1:8:2] == b"\x00\x00\x00":
        text = clear.decode("utf-16-le")
    else:
        text = clear.decode("utf-8-sig")
    try:
        return json.loads(text)
    except ValueError:
        # 部分版本按单个 token 存储 secret，而非完整账号 JSON。
        if re.fullmatch(r"[A-Za-z0-9_.=:+/-]{16,16384}", text):
            return {"token": text}
        raise ValidationError("未知已解密账号格式") from None


def extract_token(value, depth=0):
    """仅沿账号对象的已知层级查找，绝不遍历聊天或任意数据库记录。"""
    if depth > 4 or not isinstance(value, dict):
        return None
    for name in ("security_oauth_token", "securityOauthToken", "accessToken", "access_token", "token"):
        token = value.get(name)
        if isinstance(token, str) and 16 <= len(token) <= 16384 and not any(c in token for c in "\r\n"):
            return token
    for name in ("account", "auth", "session", "user", "data", "credential", "credentials"):
        token = extract_token(value.get(name), depth + 1)
        if token:
            return token
    return None


def read_account_endpoint(token, endpoint):
    # 只读官方计划 / 配额接口；不经过当前 9939 代理、不跟随重定向、不请求推理。
    allowed = ("https://openapi.qoder.sh/api/v2/user/plan", "https://openapi.qoder.sh/api/v2/quota/usage")
    if endpoint not in allowed:
        raise ValidationError("账号验证路由不在白名单")

    class NoRedirect(urlrequest.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}), NoRedirect(),
                                    urlrequest.HTTPSHandler(context=ssl.create_default_context()))
    outgoing = urlrequest.Request(endpoint, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with opener.open(outgoing, timeout=15) as incoming:
            payload = incoming.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                return {"http_status": incoming.status, "valid_json": False}, None
            try:
                value = json.loads(payload)
                return {"http_status": incoming.status, "valid_json": True}, value
            except (ValueError, UnicodeError):
                return {"http_status": incoming.status, "valid_json": False}, None
    except urlerror.HTTPError as failure:
        return {"http_status": failure.code, "valid_json": False}, None
    except (OSError, urlerror.URLError):
        return {"http_status": None, "valid_json": False, "network_error": True}, None


def account_check(root: Path):
    database = Path(os.environ["APPDATA"]) / "Qoder/User/globalStorage/state.vscdb"
    status = {"diagnostic_only": True, "existing_login_modified": False, "token_refreshed": False,
              "account_imported": False, "usable_credential_verified": False, "upstream_inference_requests": 0,
              "recognized_qoder_secret": False, "read_only_checks": []}
    if not database.exists():
        report(root, "account-check.json", status)
        return status
    snapshots = []
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        for key, value in connection.execute("SELECT key, value FROM ItemTable WHERE key LIKE 'secret://%'"):
            try:
                descriptor = json.loads(key[len("secret://"):])
                if not str(descriptor.get("extensionId", "")).startswith("aicoding."):
                    continue
                status["recognized_qoder_secret"] = True
                metadata = {}
                status["safe_decode_metadata"] = metadata
                snapshot = decode_ide_secret(value, metadata)
                status["decoded_qoder_container_type"] = type(snapshot).__name__
                if isinstance(snapshot, dict):
                    fields = {"token", "access_token", "accessToken", "security_oauth_token", "securityOauthToken",
                              "auth", "account", "session", "data", "user", "credentials", "credential"}
                    status["recognized_account_fields"] = sorted(fields.intersection(snapshot))
                token = extract_token(snapshot)
                if token:
                    snapshots.append((token, snapshot))
            except ValidationError as failure:
                status["secret_decode_state"] = "dpapi_failed" if "DPAPI" in str(failure) else "unsupported_container"
            except UnicodeError:
                status["secret_decode_state"] = "decrypted_secret_not_text"
            except (ValueError, TypeError) as failure:
                status["secret_decode_state"] = "decoded_content_not_supported_json"
                status["safe_failure_type"] = type(failure).__name__
    status["candidate_count"] = len(snapshots)
    # 不自动选择多个登录快照，避免对不明账号执行验证。
    if len(snapshots) == 1:
        token, snapshot = snapshots[0]
        plan, payload = read_account_endpoint(token, "https://openapi.qoder.sh/api/v2/user/plan")
        status["read_only_checks"].append({"endpoint": "user_plan", **plan})
        data = payload.get("data", payload) if isinstance(payload, dict) else None
        verified = plan.get("http_status") == 200 and isinstance(data, dict) and any(
            field in data for field in ("plan_tier_name", "planTierName", "user_type", "userType"))
        if verified:
            quota, quota_payload = read_account_endpoint(token, "https://openapi.qoder.sh/api/v2/quota/usage")
            status["read_only_checks"].append({"endpoint": "quota_usage", **quota})
            qdata = quota_payload.get("data", quota_payload) if isinstance(quota_payload, dict) else None
            user_quota = qdata.get("userQuota") if isinstance(qdata, dict) else None
            if isinstance(user_quota, dict):
                from .usage import normalize_usage, InvalidUsage
                remaining = user_quota.get("remaining")
                try:
                    if remaining is not None:
                        status["upstream_remaining_micro"] = normalize_usage({"credits": str(remaining)}).credits_micro
                except InvalidUsage:
                    status["upstream_remaining_micro"] = None
            # 独立测试快照受 DPAPI 保护，现用 IDE 的原始登录记录完全不变。
            protect_directory(root)
            protected = dpapi(json.dumps(snapshot, ensure_ascii=False).encode())
            account_path = root / "account-snapshot.dpapi"
            if not account_path.exists():
                write_new(account_path, protected)
                status["account_imported"] = True
            status["usable_credential_verified"] = True
    report(root, "account-check.json", status)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description="Windows 独立 P0 证书及账号验证；不修改现用 IDE")
    parser.add_argument("action", choices=("prepare", "install", "tls-check", "account-inventory", "account-check"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--electron", default="")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_relative_to(WORKSPACE / ".local"):
        parser.error("测试数据仅允许存入工作区 .local")
    try:
        if args.action == "prepare":
            value = prepare(root)
        elif args.action == "install":
            value = install(root)
        elif args.action == "tls-check":
            value = tls_check(root, args.electron)
        elif args.action == "account-check":
            value = account_check(root)
        else:
            value = account_inventory(root)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        print(json.dumps({"diagnostic_only": True, "failed": True,
                          "reason": "本机验证未完成；检查依赖、端口、当前用户权限及受保护的测试目录"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
