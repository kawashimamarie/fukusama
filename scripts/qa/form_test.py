#!/usr/bin/env python3
"""弁当事業サイトのお問い合わせフォームのローカルテスト（実際のメールは送らない）。

  python3 scripts/qa/form_test.py [out/]

PHP の組み込みサーバーで out/ebisenka.jp を動かし、sendmail の代わりにファイルへ書き出す仕組みで、
送られるはずのメールの宛先・差出人・返信先・件名・本文を確かめる。
"""
import email
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def post(port, data, origin="https://ebisenka.jp"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/contact/send.php", method="POST",
                                 data=urllib.parse.urlencode(data).encode(),
                                 headers={"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=10) as r:
            return r.status, r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "out")
    site = out / "ebisenka.jp"
    tmp = Path(tempfile.mkdtemp(prefix="formtest-"))
    mail = tmp / "mail.eml"
    sm = tmp / "sendmail.sh"
    sm.write_text(f'#!/bin/sh\n{{ echo "ARGS: $*"; cat; }} >> "{mail}"\n')
    sm.chmod(0o755)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    php = subprocess.Popen(["php", "-d", f"sendmail_path={sm}", "-d", f"sys_temp_dir={tmp}", "-S", f"127.0.0.1:{port}", "-t", str(site)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
    try:
        time.sleep(1)
        ok = {"company": "テスト株式会社", "name": "テスト 太郎", "email": "test@example.com", "type": "その他",
              "message": "テスト送信", "agree": "1", "website": ""}
        n = 0  # 送信の間隔制限（60秒）に当たらないよう、拒否されるべき入力を先に試す
        # 送ってはいけない入力
        cases = [
            ("他サイトからの送信", {**ok}, "https://invalid.example", (403,)),
            ("迷惑投稿の罠（website 欄）", {**ok, "website": "http://spam"}, "https://ebisenka.jp", None),
            ("メールアドレスに改行（ヘッダーの差し込み）", {**ok, "email": "a@example.com\r\nBcc: x@example.com"}, "https://ebisenka.jp", None),
            ("必須項目なし", {**ok, "message": ""}, "https://ebisenka.jp", None),
        ]
        for name, data, origin, want in cases:
            st, loc = post(port, data, origin)
            sent = (mail.stat().st_size if mail.is_file() else 0) != n
            if sent and name != "迷惑投稿の罠（website 欄）":
                problems.append(f"{name}：メールが作られた")
            if want and st not in want or (not want and st == 429):
                problems.append(f"{name}：HTTP {st}（期待 {want}）")
            if name == "迷惑投稿の罠（website 欄）" and sent:
                problems.append(f"{name}：メールが作られた")
            n = mail.stat().st_size if mail.is_file() else 0
            print(f"{name}：HTTP {st}・メール{'あり' if sent else 'なし'}")
        st, loc = post(port, ok)
        if st != 303 or not loc.endswith("/contact/thanks/"):
            problems.append(f"正しい入力で完了ページへ進まない（HTTP {st} {loc}）")
        raw = mail.read_bytes() if mail.is_file() else b""
        if not raw:
            problems.append("メールが作られていない")
        else:
            first, body = raw.split(b"\n", 1)
            if b"-finfo@fukushin-e.co.jp" not in first:
                problems.append("送信エラーの戻り先（-f）が info@fukushin-e.co.jp ではない")
            m = email.message_from_bytes(body)
            h = {k: str(make_header(decode_header(m[k] or ""))) for k in ("To", "From", "Reply-To", "Subject")}
            if h["To"] != "info@fukushin-e.co.jp":
                problems.append(f"宛先が違う：{h['To']}")
            if not h["From"].endswith("<info@fukushin-e.co.jp>"):
                problems.append(f"差出人が違う：{h['From']}")
            if h["Reply-To"] != "test@example.com":
                problems.append(f"返信先がお客様のアドレスではない：{h['Reply-To']}")
            text = m.get_payload(decode=True).decode("iso-2022-jp", "replace")
            if "テスト株式会社" not in text or "送信日時" not in text:
                problems.append("本文に入力内容が入っていない")
            print("メールの確認：", h)
    finally:
        php.terminate()
    if problems:
        print("【不合格】\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("フォームのローカルテスト：合格（実際のメールは送っていません）")


if __name__ == "__main__":
    main()
