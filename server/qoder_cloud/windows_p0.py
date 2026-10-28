"""Windows P0 实测工具：独立 CA、当前用户信任库、loopback TLS、只读账号盘点。

这是工程验证辅助工具，不是生产网关，不读取对话、不修改现用 IDE 配置。
证书操作需要 cryptography；离线协议工具不依赖它。私有数据只写 .local/。
"""

import argparse
import base64
import ctypes
from contextlib import closing
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
from urllib.parse import urlsplit

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
        # 只读打开；closing 确保退出时释放文件句柄，避免长时间占用现用 IDE 数据库。
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
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


def secret_text(clear: bytes):
    """还原 safeStorage 容器文本，返回 (文本, 编码名)；只用显式识别，不做静默替换。"""
    if clear.startswith((b"\xff\xfe", b"\xfe\xff")):
        return clear.decode("utf-16"), "utf-16"
    # Electron 部分版本写入无 BOM 的 UTF-16LE：ASCII 正文字节的高位字节为 0x00，
    # 因此判断依据必须是奇偶字节的 NUL 分布，不能对固定切片做定长比较。
    if len(clear) >= 8 and len(clear) % 2 == 0:
        odd_nulls = clear[1::2].count(0)
        even_nulls = clear[0::2].count(0)
        if odd_nulls > len(clear) // 4 and even_nulls < odd_nulls // 2:
            return clear.decode("utf-16-le"), "utf-16-le"
    return clear.decode("utf-8-sig"), "utf-8-sig"


def os_crypt_key(source: Path) -> bytes:
    """读取 Chromium os_crypt 密钥：Local State 里的 DPAPI 包装的 32 字节密钥。"""
    state_path = source / "Local State"
    if not state_path.is_file():
        raise ValidationError("未找到 os_crypt 状态文件")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise ValidationError("os_crypt 状态文件不可解析") from None
    section = state.get("os_crypt") if isinstance(state, dict) else None
    encoded = section.get("encrypted_key") if isinstance(section, dict) else None
    if not isinstance(encoded, str):
        raise ValidationError("os_crypt 密钥缺失")
    try:
        wrapped = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise ValidationError("os_crypt 密钥编码无效") from None
    if not wrapped.startswith(b"DPAPI"):
        raise ValidationError("os_crypt 密钥不是当前用户 DPAPI 包装")
    material = dpapi(wrapped[len(b"DPAPI"):], decrypt=True)
    if len(material) != 32:
        raise ValidationError("os_crypt 密钥长度异常")
    return material


def os_crypt_decrypt(blob: bytes, source: Path) -> bytes:
    """按 Chromium 的 v10 格式（12 字节 nonce + 密文 + tag）解密。"""
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise ValidationError("缺少隔离环境中的 cryptography 依赖") from None
    if len(blob) <= 12:
        raise ValidationError("账号容器密文长度异常")
    try:
        return AESGCM(os_crypt_key(source)).decrypt(blob[:12], blob[12:], None)
    except (InvalidTag, ValueError, OSError):
        raise ValidationError("DPAPI 解密失败且 os_crypt 密钥不匹配") from None


def decode_ide_secret(value, metadata=None, key_source=None):
    """识别 Electron Windows safeStorage 容器，使用当前用户 DPAPI 或 os_crypt 密钥；不刷新凭据。"""
    try:
        container = json.loads(value)
    except (ValueError, UnicodeError):
        raise ValidationError("未知账号容器") from None
    if metadata is not None:
        metadata["container_json"] = True
    if not isinstance(container, dict) or container.get("type") != "Buffer":
        raise ValidationError("未知账号容器")
    items = container.get("data")
    if not isinstance(items, list) or len(items) > 1024 * 1024 or not all(
            isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 255 for item in items):
        raise ValidationError("无效账号容器")
    encrypted = bytes(items)
    # 两种容器共存：Electron 自己直接 DPAPI；VS Code / Chromium 系为 v10 + AES-GCM，
    # 密钥由 Local State 中的 DPAPI 包装。先试纯 DPAPI，失败再走 os_crypt。
    clear = None
    scheme = "dpapi"
    if encrypted.startswith(b"v10"):
        body = encrypted[3:]
        try:
            clear = dpapi(body, decrypt=True)
        except ValidationError:
            if key_source is None:
                raise
            clear = os_crypt_decrypt(body, key_source)
            scheme = "os_crypt_aes_gcm"
    else:
        clear = dpapi(encrypted, decrypt=True)
    # Electron Windows safeStorage 的版本差异：UTF-16（含 / 不含 BOM）与 UTF-8 容器都需显式识别。
    text, encoding = secret_text(clear)
    if metadata is not None:
        metadata["container_scheme"] = scheme
        metadata["dpapi_decrypted_bytes"] = len(clear)
        metadata["utf16_bom"] = clear.startswith((b"\xff\xfe", b"\xfe\xff"))
        metadata["decoded_encoding"] = encoding
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
        # HTTPError 同时是文件对象，显式关闭后才能释放句柄。
        failure.close()
        return {"http_status": failure.code, "valid_json": False}, None
    except (OSError, urlerror.URLError):
        return {"http_status": None, "valid_json": False, "network_error": True}, None


def modification_time(path: Path) -> float:
    """容器最后写入时间；取不到时按最旧处理，不阻断盘点。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def iso_mtime(path: Path):
    moment = modification_time(path)
    if not moment:
        return None
    return datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec="seconds")


def count_files(root: Path, limit=50000) -> int:
    """统计数据目录文件数，只用于区分“在用”与“残留”目录；不读内容。"""
    total = 0
    try:
        for entry in os.scandir(root):
            if total >= limit:
                break
            if entry.is_dir(follow_symlinks=False):
                total += count_files(Path(entry.path), limit - total)
            elif entry.is_file(follow_symlinks=False):
                total += 1
    except OSError:
        return total
    return total


def desktop_auth_containers():
    """枚举桌面 App 的独立凭据容器，只读，不刷新。"""
    appdata = Path(os.environ["APPDATA"])
    found = []
    for label in ("com.qoder.app.stable", "com.qodercn.app.stable"):
        source = appdata / label
        path = source / "auth.v1.dat"
        if path.is_file():
            found.append((label, source, path))
    return found


def decode_desktop_auth(source: Path, path: Path):
    """按 v10 + os_crypt 解密桌面 App 凭据容器，返回 (快照, 元数据)。"""
    blob = path.read_bytes()
    if not blob.startswith(b"v10"):
        raise ValidationError("未知账号容器")
    text, encoding = secret_text(os_crypt_decrypt(blob[3:], source))
    try:
        snapshot = json.loads(text)
    except ValueError:
        raise ValidationError("未知已解密账号格式") from None
    metadata = {"container_scheme": "os_crypt_aes_gcm", "decoded_encoding": encoding,
                "dpapi_decrypted_bytes": len(blob) - 3}
    return snapshot, metadata


SOURCE_PRIORITY = ("com.qoder.app.stable", "com.qodercn.app.stable", "ide_secret")


def candidate_rank(snapshot_item):
    """同一身份有多个副本时的排序键：字段更完整 > 最近写入 > 来源顺序。

    字段完整度优先，因为要留档的是能复用的那份；同等完整度时看写入时间，
    较新的副本刷新令牌更可能仍有效；最后才用固定来源顺序兜底。
    不比较不同种类的修改时间：IDE 数据库会被频繁重写，其 mtime 不代表密钥新鲜度。
    """
    _, snapshot, label, modified = snapshot_item
    richness = len(snapshot) if isinstance(snapshot, dict) else 0
    order = SOURCE_PRIORITY.index(label) if label in SOURCE_PRIORITY else len(SOURCE_PRIORITY)
    return (-richness, -modified, order)


def select_candidate(snapshots, requested=""):
    """选出唯一待验证快照，返回 (快照四项组, 阻塞原因)。

    同一 token 出现在多个容器里属于同一身份的冗余副本，可以去重后验证；
    不同 token 代表不同登录身份，没有明确指定来源时一律不验证。
    """
    if requested:
        for item in snapshots:
            if item[2] == requested:
                return item, None
        return None, "requested_source_not_available"
    groups = {}
    for item in snapshots:
        groups.setdefault(item[0], []).append(item)
    if len(groups) != 1:
        return None, "multiple_distinct_identities"
    return min(next(iter(groups.values())), key=candidate_rank), None


def verify_candidate(root: Path, status: dict, token: str, snapshot):
    """只读校验计划与配额；成功后把快照单独加密留档，不改现用登录。"""
    plan, payload = read_account_endpoint(token, "https://openapi.qoder.sh/api/v2/user/plan")
    status["read_only_checks"].append({"endpoint": "user_plan", **plan})
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    verified = plan.get("http_status") == 200 and isinstance(data, dict) and any(
        field in data for field in ("plan_tier_name", "planTierName", "user_type", "userType"))
    if not verified:
        return
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


def account_check(root: Path, requested_source: str = ""):
    database = Path(os.environ["APPDATA"]) / "Qoder/User/globalStorage/state.vscdb"
    status = {"diagnostic_only": True, "existing_login_modified": False, "token_refreshed": False,
              "account_imported": False, "usable_credential_verified": False, "upstream_inference_requests": 0,
              "recognized_qoder_secret": False, "read_only_checks": []}
    snapshots = []
    # IDE 数据库可能不存在；桌面 App 的容器仍可独立检查。
    if database.exists():
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            for key, value in connection.execute("SELECT key, value FROM ItemTable WHERE key LIKE 'secret://%'"):
                # 描述或扩展名不属 Qoder 时直接跳过：非目标容器不得参与状态判定，
                # 否则一行畸形记录会掩盖真正的 Qoder 容器。
                try:
                    descriptor = json.loads(key[len("secret://"):])
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(descriptor, dict) or not str(descriptor.get("extensionId", "")).startswith("aicoding."):
                    continue
                status["recognized_qoder_secret"] = True
                metadata = {}
                status["safe_decode_metadata"] = metadata
                try:
                    snapshot = decode_ide_secret(value, metadata, database.parents[2])
                    status["decoded_qoder_container_type"] = type(snapshot).__name__
                    if isinstance(snapshot, dict):
                        fields = {"token", "access_token", "accessToken", "security_oauth_token",
                                  "securityOauthToken", "auth", "account", "session", "data", "user",
                                  "credentials", "credential"}
                        status["recognized_account_fields"] = sorted(fields.intersection(snapshot))
                    token = extract_token(snapshot)
                    if token:
                        snapshots.append((token, snapshot, "ide_secret", modification_time(database)))
                except ValidationError as failure:
                    reason = str(failure)
                    if "DPAPI" in reason:
                        status["secret_decode_state"] = "dpapi_failed"
                    elif "已解密" in reason:
                        status["secret_decode_state"] = "decoded_content_not_supported"
                    else:
                        status["secret_decode_state"] = "unsupported_container"
                except UnicodeError:
                    status["secret_decode_state"] = "decrypted_secret_not_text"
                except (ValueError, TypeError) as failure:
                    status["secret_decode_state"] = "unsupported_container"
                    status["safe_failure_type"] = type(failure).__name__
    # 桌面 App 的独立容器：与 IDE 互不影响，只用同级 Local State 的 os_crypt 密钥只读解密。
    for label, source, path in desktop_auth_containers():
        entry = {"source": label, "exists": True, "usable_credential_verified": False}
        status.setdefault("desktop_auth_containers", []).append(entry)
        try:
            snapshot, metadata = decode_desktop_auth(source, path)
        except ValidationError as failure:
            entry["decode_state"] = "dpapi_failed" if "DPAPI" in str(failure) else "unsupported_container"
            continue
        except (ValueError, UnicodeError):
            entry["decode_state"] = "decoded_content_not_supported"
            continue
        entry["decode_metadata"] = metadata
        entry["decoded_container_type"] = type(snapshot).__name__
        if isinstance(snapshot, dict):
            fields = {"token", "refreshToken", "user", "expiresAt", "schemaVersion", "authenticationContext"}
            entry["recognized_account_fields"] = sorted(fields.intersection(snapshot))
        token = extract_token(snapshot)
        entry["token_extractable"] = bool(token)
        entry["data_directory_files"] = count_files(source)
        if token:
            entry["container_modified_at"] = iso_mtime(path)
            snapshots.append((token, snapshot, label, modification_time(path)))
    status["candidate_count"] = len(snapshots)
    status["candidate_sources"] = [label for _, _, label, _ in snapshots]
    status["distinct_identity_count"] = len({token for token, _, _, _ in snapshots})
    chosen, blocked = select_candidate(snapshots, requested_source)
    if blocked:
        # 不自动挑选不同身份的登录快照；阻塞原因是固定文案，可用 --source 显式指定。
        status["verification_blocked"] = blocked
    else:
        token, snapshot, source_label, _ = chosen
        status["verified_source"] = source_label
        verify_candidate(root, status, token, snapshot)
        if status["usable_credential_verified"]:
            # 容器条目只标记实际通过只读校验的那一个来源。
            for entry in status.get("desktop_auth_containers", []):
                entry["usable_credential_verified"] = entry["source"] == source_label
    report(root, "account-check.json", status)
    return status


# 代理默认解密范围；与 src-tauri/src/config.rs 的 DEFAULT_TARGET_DOMAINS 保持同一份，
# 跨语言一致性由 EndpointCheckTests 守住。
DEFAULT_TARGET_DOMAINS = ("center.qoder.sh", "openapi.qoder.sh", "api3.qoder.sh", "api5.qoder.com.cn")
REGION_NODE_ROLES = {"preferredInferenceNode": "inference",
                     "preferredNesNode": "nes",
                     "preferredCodebaseNode": "codebase"}
BILLED_ROLES = ("inference", "nes")
REGION_CACHE_MAX_AGE = 1800


def endpoint_host(value):
    """从区域配置的 endpoint 取出主机名；只接受 https，取不到时返回空串。"""
    if not isinstance(value, str):
        return ""
    parts = urlsplit(value.strip())
    if parts.scheme != "https" or not parts.hostname:
        return ""
    host = parts.hostname.lower()
    return host if re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", host) else ""


def region_cache_nodes(cache: Path):
    """读取 IDE 区域配置缓存里的节点，返回 (节点列表, 取数时间)；不读任何身份信息。"""
    try:
        value = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return [], None
    content = value.get("content") if isinstance(value, dict) else None
    nodes = []
    for key, role in REGION_NODE_ROLES.items():
        item = content.get(key) if isinstance(content, dict) else None
        endpoint = item.get("endpoint") if isinstance(item, dict) else None
        host = endpoint_host(endpoint)
        nodes.append({"node": key, "role": role, "host": host,
                      "billed": role in BILLED_ROLES, "in_default_scope": host in DEFAULT_TARGET_DOMAINS})
    fetched = value.get("fetchTime") if isinstance(value, dict) else None
    moment = None
    if isinstance(fetched, (int, float)) and not isinstance(fetched, bool) and fetched > 0:
        moment = datetime.fromtimestamp(fetched / 1000, timezone.utc)
    return nodes, moment


def endpoint_check(root: Path, cache: Path = None):
    """只读核对 IDE 区域节点与默认解密范围；只报告漂移，不改任何配置。"""
    path = cache or Path(os.environ["APPDATA"]) / "Qoder/User/region-config-cache.json"
    status = {"diagnostic_only": True, "region_cache_present": path.is_file(),
              "default_target_domains": list(DEFAULT_TARGET_DOMAINS), "nodes": [],
              "missing_billing_domains": [], "unbilled_domains_in_scope": [],
              "suggested_target_domains": list(DEFAULT_TARGET_DOMAINS),
              "within_default_scope": None, "region_cache_fetched_at": None,
              "region_cache_age_seconds": None, "region_cache_stale": None,
              "existing_login_modified": False}
    if status["region_cache_present"]:
        nodes, fetched = region_cache_nodes(path)
        status["nodes"] = nodes
        billed = [item["host"] for item in nodes if item["billed"] and item["host"]]
        unbilled = {item["host"] for item in nodes if not item["billed"] and item["host"]}
        status["missing_billing_domains"] = [host for host in billed if host not in DEFAULT_TARGET_DOMAINS]
        status["unbilled_domains_in_scope"] = [host for host in DEFAULT_TARGET_DOMAINS if host in unbilled]
        # 建议名单保持默认顺序，只在尾部追加漂移出来的计费节点，方便直接与 config.rs 比对。
        status["suggested_target_domains"] = list(DEFAULT_TARGET_DOMAINS) + status["missing_billing_domains"]
        status["within_default_scope"] = not status["missing_billing_domains"]
        if fetched is not None:
            status["region_cache_fetched_at"] = fetched.isoformat(timespec="seconds")
            status["region_cache_age_seconds"] = max(0, int((datetime.now(timezone.utc) - fetched).total_seconds()))
            # 缓存超过刷新周期说明 IDE 近期没联网，节点名单可能已经过时。
            status["region_cache_stale"] = status["region_cache_age_seconds"] > REGION_CACHE_MAX_AGE
    report(root, "endpoint-check.json", status)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description="Windows 独立 P0 证书及账号验证；不修改现用 IDE")
    parser.add_argument("action", choices=("prepare", "install", "tls-check", "account-inventory", "account-check",
                                            "endpoint-check"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--electron", default="")
    parser.add_argument("--source", default="",
                        help="account-check 待验证的凭据来源；多身份并存时必填")
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
            value = account_check(root, args.source)
        elif args.action == "endpoint-check":
            value = endpoint_check(root)
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
