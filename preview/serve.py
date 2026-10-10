#!/usr/bin/env python3
"""ローカルプレビューサーバー（公開用ではありません）

  python3 preview/serve.py

  弁当事業サイト   http://localhost:8080/
  コーポレート     http://localhost:8081/

- 両サイト間のリンク（ebisenka.jp ⇔ fukushin-e.co.jp）をプレビュー内のURLに置き換えて表示する
- PHP がインストールされていれば、お問い合わせフォームは本番と同じ send.php で処理する。
  メールは送信せず preview/outbox/ に .eml ファイルとして保存する
- PHP が無い場合は、フォーム送信時に入力内容の確認画面を表示する（送信処理は動かない）
外部ライブラリ不要（Python 3.8 以上）。終了は Ctrl+C。
"""
import argparse
import html
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENTO_DIST = ROOT / "bento-site" / "dist"
CORP_DIR = ROOT / "corporate"
OUTBOX = ROOT / "preview" / "outbox"
FAKE_SENDMAIL = ROOT / "preview" / "fake_sendmail.py"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def make_handler(docroot, rewrites, php_port=None, not_found=None):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(docroot), **kw)

        def log_message(self, fmt, *args):
            pass  # 静かに動かす

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            fs = Path(self.translate_path(path))
            if fs.is_dir():
                fs = fs / "index.html"
            if fs.suffix == ".php":
                self.send_error(403)
                return
            if not fs.exists():
                if not_found and (docroot / not_found).exists():
                    self._send_html((docroot / not_found).read_text(encoding="utf-8"), 404)
                else:
                    self.send_error(404)
                return
            if fs.suffix == ".html":
                self._send_html(fs.read_text(encoding="utf-8"))
                return
            super().do_GET()

        def _send_html(self, text, status=200):
            for old, new in rewrites:
                text = text.replace(old, new)
            data = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if urllib.parse.urlsplit(self.path).path != "/contact/send.php":
                self.send_error(405)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if php_port:
                self._proxy_php(body)
            else:
                self._preview_only(body)

        def _proxy_php(self, body):
            req = urllib.request.Request(
                f"http://127.0.0.1:{php_port}/contact/send.php", data=body, method="POST",
                headers={"Content-Type": self.headers.get("Content-Type", ""),
                         # send.php は本番ドメインからの送信のみ受け付けるため、プレビューでは本番の Origin として渡す
                         "Origin": "https://ebisenka.jp"})
            opener = urllib.request.build_opener(NoRedirect)
            try:
                res = opener.open(req, timeout=20)
                status, headers, data = res.status, res.headers, res.read()
            except urllib.error.HTTPError as e:
                status, headers, data = e.code, e.headers, e.read()
            self.send_response(status)
            loc = headers.get("Location")
            if loc:
                self.send_header("Location", urllib.parse.urlsplit(loc).path or "/")
            self.send_header("Content-Type", headers.get("Content-Type", "text/html; charset=UTF-8"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _preview_only(self, body):
            fields = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            rows = "".join(f"<div><dt>{html.escape(k)}</dt><dd>{html.escape(' / '.join(v))}</dd></div>"
                           for k, v in fields.items() if k != "website")
            page = ("<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                    "<link rel='stylesheet' href='/assets/css/site.css'><title>プレビュー：送信内容</title></head>"
                    "<body><main class='section'><div class='wrap' style='max-width:760px'>"
                    "<p class='eyebrow'>PREVIEW</p><h1 class='h2'>プレビューでは送信されません</h1>"
                    "<p class='note' style='margin:16px 0 24px'>PHP がインストールされていないため、送信処理は動作していません。"
                    "フォームに入力された内容は次のとおりです。</p>"
                    f"<dl class='dl'>{rows}</dl>"
                    "<p class='btn-row' style='margin-top:28px'><a class='btn btn--ghost' href='/contact/'>フォームへ戻る</a></p>"
                    "</div></main></body></html>")
            self._send_html(page)

    return Handler


def free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def main():
    ap = argparse.ArgumentParser(description="ローカルプレビュー")
    ap.add_argument("--bento-port", type=int, default=8080)
    ap.add_argument("--corp-port", type=int, default=8081)
    ap.add_argument("--host", default="127.0.0.1",
                    help="同じネットワークのスマートフォンから見る場合は 0.0.0.0（社内LANのみで使用）")
    ap.add_argument("--no-build", action="store_true", help="弁当サイトの再ビルドを省略")
    args = ap.parse_args()

    if not args.no_build:
        subprocess.run([sys.executable, str(ROOT / "bento-site" / "build.py")], check=True,
                       stdout=subprocess.DEVNULL)

    for p in (args.bento_port, args.corp_port):
        if not free(p):
            sys.exit(f"ポート {p} は使用中です。--bento-port / --corp-port で変更してください。")

    shown_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    bento_url = f"http://{shown_host}:{args.bento_port}/"
    corp_url = f"http://{shown_host}:{args.corp_port}/"

    php_port, php_proc = None, None
    # Windows の PHP は sendmail_path を使わず SMTP 送信になるため、プレビューでは送信処理を動かさない
    if shutil.which("php") and os.name != "nt":
        php_port = 8090
        while not free(php_port):
            php_port += 1
        OUTBOX.mkdir(parents=True, exist_ok=True)
        php_proc = subprocess.Popen(
            ["php", "-d", f"sendmail_path={sys.executable} {FAKE_SENDMAIL}", "-S", f"127.0.0.1:{php_port}"],
            cwd=str(BENTO_DIST), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.6)

    bento_rw = [("https://fukushin-e.co.jp/", corp_url), ("https://ebisenka.jp/", bento_url)]
    corp_rw = [("https://ebisenka.jp/", bento_url)]

    servers = [
        ThreadingHTTPServer((args.host, args.bento_port), make_handler(BENTO_DIST, bento_rw, php_port, "404.html")),
        ThreadingHTTPServer((args.host, args.corp_port), make_handler(CORP_DIR, corp_rw)),
    ]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()

    print("プレビューを起動しました（Ctrl+C で終了）")
    print(f"  弁当事業サイト : {bento_url}")
    print(f"  コーポレート   : {corp_url}")
    if php_port:
        print(f"  フォーム       : 本番と同じ send.php で処理。メールは送らず {OUTBOX} に保存します")
    else:
        print("  フォーム       : PHP が無いため、送信時は入力内容の確認画面を表示します")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.shutdown()
        if php_proc:
            php_proc.terminate()


if __name__ == "__main__":
    main()
