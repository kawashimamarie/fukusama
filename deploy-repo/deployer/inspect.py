"""読み取り専用：公開の仕組みが触れない既存ファイル（コーポレートの mail.php だけ）の PHP の不具合を、
GitHub Actions の実行環境の中だけで調べる。

  python -m deployer.inspect --site corporate --path mail.php

- 取り出せるのは ALLOWED に書いたファイルだけ。読み取り専用の接続で、サーバーには一切書き込まない
- 中身は実行環境の一時フォルダー（本人だけが読める）に置き、調べ終えたらすぐ消す。成果物（Artifact）は作らない
- ログに出すのは、エラーの種類・行番号と、中身を含まない形の事実（バイト数・BOM の有無・<?php より前の
  バイトの種類と数・字句の種類名と行番号など）だけ。ソースの全文・抜粋・値・ハッシュは出さない
- PHP のエラー文は、ファイルの場所を「mail.php」に置き換え、引用符の中（ソースの断片が入りうる）を伏せる
- mail.php は実行しない（php -l の構文チェックと、字句の種類の読み取りだけ）
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config as C
from .ftps import Ftps

ALLOWED = {("corporate", "mail.php")}

# php -l の出力のうち、そのまま（伏せたうえで）出してよい行の形
SAFE_LINE = re.compile(r"^(PHP )?(Parse|Fatal|Warning|Deprecated|Notice)( error)?: .{1,300} in mail\.php on line \d+$"
                       r"|^No syntax errors detected in mail\.php$|^Errors parsing mail\.php$")
QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'|`[^`]*`")

# 字句の種類名と行番号だけを出す（値は出さない）。T_INLINE_HTML は長さだけ
TOKENS_PHP = r"""
$t = token_get_all(file_get_contents($argv[1]));
$o = [];
foreach ($t as $x) {
  if (is_array($x)) {
    $n = token_name($x[0]);
    if ($n === 'T_WHITESPACE' || $n === 'T_COMMENT' || $n === 'T_DOC_COMMENT') continue;
    if ($n === 'T_INLINE_HTML') { $o[] = 'T_INLINE_HTML(len=' . strlen($x[1]) . ')@' . $x[2]; }
    else { $o[] = $n . '@' . $x[2]; }
    if ($n === 'T_DECLARE') break;
  } else {
    $o[] = in_array($x, [';', '(', ')', '{', '}', '=', ','], true) ? $x : '?';
  }
  if (count($o) >= 12) break;
}
echo json_encode($o);
"""


def sanitize(output, tmp_path):
    """php の出力から、一時ファイルの場所と、引用符の中身を除く。決まった形の行だけを残す"""
    lines, hidden = [], 0
    for line in output.replace(str(tmp_path), "mail.php").splitlines():
        line = QUOTED.sub("\"…\"", line.strip())
        if not line:
            continue
        if SAFE_LINE.match(line):
            lines.append(line)
        else:
            hidden += 1
    if hidden:
        lines.append(f"（決まった形でない出力 {hidden} 行は伏せました）")
    return lines


def facts(data):
    """中身を含まない形の事実だけ"""
    f = {"bytes": len(data), "bom": data.startswith(b"\xef\xbb\xbf"), "crlf": b"\r\n" in data,
         "lines": data.count(b"\n") + (0 if data.endswith(b"\n") else 1)}
    i = data.find(b"<?php")
    f["open_tag_found"] = i >= 0
    if i >= 0:
        pre = data[:i]
        body = pre[3:] if f["bom"] else pre
        f["bytes_before_open_tag"] = len(pre)
        f["before_open_tag_kinds"] = {
            "bom": 3 if f["bom"] else 0,
            "lf": body.count(b"\n"), "cr": body.count(b"\r"), "space": body.count(b" "), "tab": body.count(b"\t"),
            "other": sum(1 for b in body if b not in b"\n\r \t"),
        }
        f["open_tag_line"] = data[:i].count(b"\n") + 1
    m = re.search(rb"declare\s*\(\s*strict_types", data)
    f["strict_types_line"] = data[:m.start()].count(b"\n") + 1 if m else None
    f["closing_tag_count"] = data.count(b"?>")
    if data.rstrip().endswith(b"?>"):
        tail = data[data.rstrip().rfind(b"?>") + 2:]
        f["bytes_after_last_closing_tag"] = len(tail)
    f["sets_display_errors"] = bool(re.search(rb"display_errors", data))
    return f


def check(data, php="php"):
    """一時フォルダーで php -l と字句の種類の読み取りをして、伏せた結果を返す。終わったら消す"""
    base = os.environ.get("RUNNER_TEMP") or None
    d = tempfile.mkdtemp(prefix="inspect-", dir=base)
    try:
        os.chmod(d, 0o700)
        p = Path(d) / "mail.php"
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        r = subprocess.run([php, "-d", "display_errors=stderr", "-d", "log_errors=0", "-d", "html_errors=0",
                            "-l", str(p)], capture_output=True, text=True, errors="replace", timeout=60)
        out = {"lint_exit": r.returncode, "lint": sanitize(r.stdout + "\n" + r.stderr, p)}
        t = subprocess.run([php, "-d", "display_errors=0", "-r", TOKENS_PHP, "--", str(p)],
                           capture_output=True, text=True, errors="replace", timeout=60)
        try:
            toks = json.loads(t.stdout) if t.returncode == 0 else None
            ok = isinstance(toks, list) and all(isinstance(x, str) and re.fullmatch(r"[A-Z_]+(\(len=\d+\))?@\d+|[;(){}=,?]", x)
                                                for x in toks)
            out["tokens_until_declare"] = toks if ok else "読み取れませんでした"
        except ValueError:
            out["tokens_until_declare"] = "読み取れませんでした"
        return out
    finally:
        shutil.rmtree(d, ignore_errors=True)


def report(f, r):
    k = f.get("before_open_tag_kinds", {})
    lines = [
        "## mail.php の調査（読み取り専用・ソースは出していません）",
        f"- 大きさ：{f['bytes']} バイト・{f['lines']} 行・改行 {'CRLF を含む' if f['crlf'] else 'LF のみ'}",
        f"- 先頭の BOM（EF BB BF）：{'あり' if f['bom'] else 'なし'}",
        f"- `<?php` の位置：{'見つからない' if not f['open_tag_found'] else str(f['open_tag_line']) + ' 行目'}"
        + (f"（前に {f['bytes_before_open_tag']} バイト：BOM {k['bom']}・改行 LF {k['lf']}／CR {k['cr']}・空白 {k['space']}"
           f"・タブ {k['tab']}・その他 {k['other']}）" if f['open_tag_found'] else ""),
        f"- `declare(strict_types…)` の行：{f['strict_types_line'] or 'なし'}",
        f"- 閉じタグ `?>`：{f['closing_tag_count']} 個" + (f"（最後の閉じタグの後に {f['bytes_after_last_closing_tag']} バイト）"
                                                     if "bytes_after_last_closing_tag" in f else ""),
        f"- display_errors の指定：{'あり' if f['sets_display_errors'] else 'なし'}",
        f"- 字句の並び（declare まで・種類名@行のみ）：{r['tokens_until_declare']}",
        f"- PHP 構文チェック（php -l、終了コード {r['lint_exit']}）：",
    ] + [f"  - {x}" for x in r["lint"]]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--path", required=True)
    a = ap.parse_args()
    if (a.site, a.path) not in ALLOWED:
        sys.exit("調べられないファイルです")
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
    text = report(facts(data), check(data))
    del data
    print(text)
    s = os.environ.get("GITHUB_STEP_SUMMARY")
    if s:
        with open(s, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


if __name__ == "__main__":
    main()
