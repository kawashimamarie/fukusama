"""公開の仕組みの模擬テスト用：ローカルの FTPS サーバー（証明書の検証あり）と、公開URLの代わりの HTTP サーバー。

HTTP サーバーは、お名前.com（nginx＋Apache）のうち、公開の確認に関わる動きだけを真似る：
- フォルダ → index.html（.htaccess に DirectoryIndex index.html があるとき）または index.php（WordPress・毎回中身が変わる）
- .htaccess の RewriteRule（福記美食の切り替え設定）による 301
- .htaccess・config.php は 403、send.php は他サイトからの送信を 403
- index のない空のフォルダは 403、ないファイルは 404
- 故障の注入（fail）・古いキャッシュの注入（stale）
"""
import hashlib
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import TLS_FTPHandler
from pyftpdlib.servers import ThreadedFTPServer


logging.getLogger("pyftpdlib").setLevel(logging.WARNING)


def make_ca(d):
    d = Path(d)
    run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True)
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=test-ca",
        "-keyout", "ca.key", "-out", "ca.pem", "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost", "-keyout", "srv.key", "-out", "srv.csr")
    (d / "ext.cnf").write_text("subjectAltName=DNS:localhost\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n")
    run("openssl", "x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
        "-days", "2", "-out", "srv.pem", "-extfile", "ext.cnf")
    # 別の CA（信頼していない）で作った証明書：証明書の検証を確かめる
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost",
        "-keyout", "bad.key", "-out", "bad.pem", "-addext", "subjectAltName=DNS:localhost")
    return d


class FtpsServer:
    def __init__(self, root, ca_dir, user="deploy", password=None, cert="srv"):
        self.root = Path(root)
        self.user, self.password = user, password or secrets.token_hex(8)
        auth = DummyAuthorizer()
        auth.add_user(self.user, self.password, str(self.root), perm="elradfmw")
        h = type("H", (TLS_FTPHandler,), {})
        h.certfile = str(Path(ca_dir) / f"{cert}.pem")
        h.keyfile = str(Path(ca_dir) / f"{cert}.key")
        h.tls_control_required = True
        h.tls_data_required = True
        h.authorizer = auth
        h.log_prefix = ""
        self.handler = h
        self.server = ThreadedFTPServer(("127.0.0.1", 0), h)
        self.port = self.server.address[1]
        self.fail_rename = set()
        srv = self

        orig = h.ftp_RNTO

        def rnto(self_, path):
            rel = os.path.relpath(path, str(srv.root)).replace(os.sep, "/")
            if rel in srv.fail_rename:
                self_.respond("550 Simulated rename failure.")
                return
            return orig(self_, path)

        h.ftp_RNTO = rnto
        self.t = threading.Thread(target=self.server.serve_forever, kwargs={"timeout": 0.2, "blocking": True}, daemon=True)
        self.t.start()

    def stop(self):
        self.server.close_all()


class WebServer:
    def __init__(self, root, *, wordpress=False, allowed_origins=("https://ebisenka.jp",)):
        self.root = Path(root)
        self.wordpress = wordpress
        self.fail = set()       # このパスは 500 を返す
        self.stale = {}         # キャッシュの目印なしのとき、このパスは古い中身を返す
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, st, body=b"", headers=()):
                self.send_response(st)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_POST(self):
                self.do_GET()

            def do_GET(self):
                u = urlparse(self.path)
                path = u.path
                if path in srv.fail:
                    return self._send(500, b"error")
                if "deploy-check=" not in (u.query or "") and path in srv.stale:
                    return self._send(200, srv.stale[path])
                ht = (srv.root / ".htaccess")
                ht_text = ht.read_text("utf-8") if ht.is_file() else ""
                for m in re.finditer(r"^RewriteRule \^(.+)\$ (\S+) \[R=301,L\]$", ht_text, re.M):
                    if re.match("^" + m.group(1) + "$", path.lstrip("/")):
                        return self._send(301, b"", [("Location", srv.base_url + m.group(2))])
                rel = path.lstrip("/")
                f = srv.root / rel
                name = Path(rel).name
                if name.startswith(".ht") or name == "config.php":
                    return self._send(403, b"Forbidden")
                if f.is_dir():
                    idx = ["index.html", "index.php"] if "DirectoryIndex index.html" in ht_text or not srv.wordpress else ["index.php", "index.html"]
                    for i in idx:
                        if (f / i).is_file():
                            f = f / i
                            break
                    else:
                        return self._send(403, b"Forbidden")
                if f.is_file():
                    if f.suffix == ".php":
                        if f.name == "send.php":
                            origin = self.headers.get("Origin", "")
                            return self._send(403 if origin not in allowed_origins else 303, b"")
                        if srv.wordpress and f.name == "index.php":
                            return self._send(200, b"<html>WordPress page nonce=" + secrets.token_hex(8).encode() + b"</html>")
                        return self._send(200, b"")
                    return self._send(200, f.read_bytes())
                if srv.wordpress and (srv.root / "index.php").is_file():
                    return self._send(404, b"<html>WordPress 404</html>")
                return self._send(404, b"Not Found")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.base_url = f"http://localhost:{self.port}"
        self.t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.t.start()

    def stop(self):
        self.server.shutdown()


def tree(root):
    """{相対パス: SHA-256}（比較用）"""
    root = Path(root)
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def copytree(src, dst):
    shutil.copytree(src, dst, dirs_exist_ok=True)


def tmpdir():
    return Path(tempfile.mkdtemp(prefix="deploytest-"))
