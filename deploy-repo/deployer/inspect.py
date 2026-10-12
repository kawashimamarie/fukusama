"""読み取り専用：公開の仕組みが触れない既存ファイル（例：コーポレートの mail.php）を、調査のために取り出す。

  python -m deployer.inspect --site corporate --path mail.php --out inspect/

- 取り出せるのは ALLOWED に書いたファイルだけ。書き込みは一切しない（読み取り専用の接続）
- パスワード・秘密鍵・トークンらしき値は、取り出す前に伏せる（値の長さと SHA-256 の先頭8文字だけ残す。
  修正後に同じ値が保たれているかを照合するため）
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from . import config as C
from .ftps import Ftps

ALLOWED = {("corporate", "mail.php")}

SECRET_KEY = re.compile(r"(pass(word|wd)?|secret|token|api[_-]?key|private[_-]?key|smtp[_-]?pass|auth)", re.I)
# 'key' => 'value' / $key = 'value' / define('KEY', 'value') の値を伏せる
PATTERNS = [
    re.compile(r"""(?P<head>(['"])(?P<k>[^'"]{1,60})\2\s*=>\s*)(?P<q>['"])(?P<v>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
    re.compile(r"""(?P<head>\$(?P<k>[A-Za-z_][A-Za-z0-9_]{0,60})\s*=\s*)(?P<q>['"])(?P<v>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
    re.compile(r"""(?P<head>define\(\s*(['"])(?P<k>[^'"]{1,60})\2\s*,\s*)(?P<q>['"])(?P<v>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
]


def redact(text):
    found = []

    def sub(m):
        if not SECRET_KEY.search(m.group("k")) or not m.group("v"):
            return m.group(0)
        v = m.group("v")
        tag = f"***REDACTED len={len(v)} sha256={hashlib.sha256(v.encode()).hexdigest()[:8]}***"
        found.append({"key": m.group("k"), "len": len(v)})
        return m.group("head") + m.group("q") + tag + m.group("q")

    for p in PATTERNS:
        text = p.sub(sub, text)
    return text, found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--path", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if (a.site, a.path) not in ALLOWED:
        sys.exit("取り出せないファイルです")
    site = C.SITES[a.site]
    p = site.secret_prefix
    ftp = Ftps(os.environ.get(p + "_HOST", ""), os.environ.get(p + "_USER", ""), os.environ.get(p + "_PASSWORD", ""),
               readonly=True)
    try:
        data = ftp.get(a.path)
    finally:
        ftp.close()
    if data is None:
        sys.exit(f"{a.path} がありません")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    head = data[:16]
    info = {"path": a.path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "starts_with_bom": data.startswith(b"\xef\xbb\xbf"), "first_bytes_hex": head.hex(),
            "crlf": b"\r\n" in data}
    text = data.decode("utf-8", "replace")
    red, found = redact(text)
    info["redacted"] = found
    (out / (Path(a.path).name + ".redacted.txt")).write_text(red, encoding="utf-8")
    (out / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in info.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
