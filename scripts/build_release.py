#!/usr/bin/env python3
"""本番公開用の zip 3点を作る（ローカルで生成するだけ。サーバーには何も送らない）。

  python3 scripts/build_release.py

出力：release/（Git 管理外）
  1_ebisenka.jp_<日時>.zip                 弁当事業サイト一式（.htaccess・フォーム含む）
  2_ebisenka-seavans.com_<日時>.zip        福記美食リニューアル版（PHP・.htaccess なし）
  2_ebisenka-seavans.com_htaccess追記_<日時>.txt  既存 .htaccess の先頭に貼る切り替え設定
  3_fukushin-e.co.jp_差分_<日時>.zip        コーポレートの変更ファイルのみ（取り込み時点 a6ba69e との差分）
  MANIFEST_<日時>.txt                      各 zip のサイズ・SHA-256・収録ファイル一覧・作成日時

作成時に、テスト設定・プレビュー用の痕跡・非公開ファイルが混ざっていないかを検査し、
1つでも見つかれば zip を作らずに止まる。
"""
import hashlib
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "release"
BASELINE = "a6ba69e"  # 公開中コーポレートを取り込んだコミット
JST = timezone(timedelta(hours=9))

HTACCESS_SNIPPET = """# ===== 福記美食 リニューアル版への切り替え（ここから）=====
# 既存の「# BEGIN WordPress」より上に貼り付ける。
# この囲みを削除し、追加したファイルを外せば、元の WordPress の表示に戻る。
DirectoryIndex index.html index.php
<IfModule mod_rewrite.c>
RewriteEngine On
# 旧ページ（店内飲食向け）のURLを、新しいページへ転送
RewriteRule ^(lunch|food)/?$ /bento/ [R=301,L]
RewriteRule ^commitment/?$ /kitchen/ [R=301,L]
RewriteRule ^info/?$ /access/ [R=301,L]
RewriteRule ^(party|drink|campaign|news|sitemap|privacy)/?$ / [R=301,L]
RewriteRule ^wp-sitemap.*\\.xml$ /sitemap.xml [R=301,L]
</IfModule>
# ===== 福記美食 リニューアル版への切り替え（ここまで）=====
"""

# 公開物に含まれてはいけない文字列（プレビュー・テストの痕跡、使わないアドレス）
FORBIDDEN_TEXT = [
    "_preview-fk2026", "pv-badge", "noindex", "localhost", "127.0.0.1",
    "noreply@ebisenka.jp", "preview-mail", "テスト送信を受け付けました（メールは送信していません）",
]
# 検索結果に出す必要のないページ（noindex を入れてよいもの）
NOINDEX_OK = {"404.html", "contact/thanks/index.html", "contact/send.php"}
# 公開物に含まれてはいけないファイル
FORBIDDEN_NAMES = re.compile(r"(^|/)(\.DS_Store|Thumbs\.db|\.git.*|\.htpasswd.*|mail\.php|.*\.bak|.*~|partials|__pycache__)(/|$)")
# 「最大」「毎日1,500」など、使わない表現
FORBIDDEN_CLAIMS = re.compile(r"毎日\s*1,?500|1日(最大)?\s*1,?500|最大\s*1,?500|常時|製造能力|どんな大量注文")


def run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.DEVNULL)


def check_tree(name, base, files, problems):
    for rel in files:
        if FORBIDDEN_NAMES.search(rel):
            problems.append(f"{name}: 含めてはいけないファイル {rel}")
        p = base / rel
        if p.suffix in {".html", ".php", ".css", ".js", ".txt", ".xml", ".htaccess"} or p.name == ".htaccess":
            text = p.read_text(encoding="utf-8", errors="replace")
            for s in FORBIDDEN_TEXT:
                if s == "noindex" and rel in NOINDEX_OK:
                    continue
                if s in text:
                    problems.append(f"{name}: {rel} に「{s}」")
            if p.suffix == ".html":
                m = FORBIDDEN_CLAIMS.search(re.sub(r"<[^>]+>", "", text))
                if m:
                    problems.append(f"{name}: {rel} に使わない表現「{m.group(0)}」")


def check_bento_config(base, problems):
    cfg = (base / "contact" / "config.php").read_text(encoding="utf-8")
    want = {
        "to": "'to' => 'info@fukushin-e.co.jp'",
        "from": "'from' => 'info@fukushin-e.co.jp'",
        "test_mode": "'test_mode' => false",
        "base_path": "'base_path' => ''",
    }
    for k, s in want.items():
        if s not in cfg:
            problems.append(f"ebisenka.jp: config.php の {k} が本番用の値ではない")
    send = (base / "contact" / "send.php").read_text(encoding="utf-8")
    if "'Reply-To: ' . $data['email']" not in send:
        problems.append("ebisenka.jp: send.php の Reply-To がお客様のアドレスになっていない")
    if re.search(r"From:[^\n]*\$data\['email'\]", send):
        problems.append("ebisenka.jp: send.php の From にお客様のアドレスが入っている")


def list_files(base):
    return sorted(str(p.relative_to(base)).replace("\\", "/") for p in base.rglob("*") if p.is_file())


def make_zip(base, files, dest):
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            z.write(base / rel, rel)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    now = datetime.now(JST)
    stamp = now.strftime("%Y%m%d-%H%M")
    work = OUT / f"_work_{stamp}"
    if OUT.exists():
        shutil.rmtree(OUT)
    work.mkdir(parents=True)

    # 1. ebisenka.jp
    bento = work / "ebisenka.jp"
    run([sys.executable, "build.py", str(bento)], ROOT / "bento-site")
    # 2. ebisenka-seavans.com
    fukuki = work / "ebisenka-seavans.com"
    run([sys.executable, "build.py", str(fukuki)], ROOT / "fukuki-site")
    # 3. fukushin-e.co.jp（取り込み時点からの変更ファイルのみ）
    corp = work / "fukushin-e.co.jp"
    changed = subprocess.run(["git", "diff", "--name-only", BASELINE, "--", "corporate"], cwd=ROOT,
                             check=True, capture_output=True, text=True).stdout.split()
    for rel in changed:
        src = ROOT / rel
        if not src.exists():
            continue
        dst = corp / Path(rel).relative_to("corporate")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    sites = [
        ("1_ebisenka.jp", bento, "public_html/ebisenka.jp/ の直下（現在は空）"),
        ("2_ebisenka-seavans.com", fukuki, "public_html/ebisenka-seavans.com/ の直下（WordPress と同じ場所。既存ファイルは消さない）"),
        ("3_fukushin-e.co.jp_差分", corp, "public_html/fukushin-e.co.jp/ の同じ場所に上書き（ほかのファイルには触れない）"),
    ]

    problems = []
    for name, base, _ in sites:
        check_tree(name, base, list_files(base), problems)
    check_bento_config(bento, problems)
    if any(f == ".htaccess" or f.endswith(".php") for f in list_files(fukuki)):
        problems.append("ebisenka-seavans.com: .htaccess／PHP が含まれている（WordPress のファイルを上書きするおそれ）")
    if any(f.endswith(".php") or f == ".htaccess" for f in list_files(corp)):
        problems.append("fukushin-e.co.jp: PHP／.htaccess が含まれている（mail.php 等を上書きするおそれ）")
    if problems:
        print("【中止】公開用ファイルに問題があります：\n  " + "\n  ".join(problems))
        sys.exit(1)

    lines = [
        "本番公開用ファイル 一覧",
        f"作成日時：{now.strftime('%Y-%m-%d %H:%M')}（日本時間）",
        f"元のコミット：{subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip()}",
        "検査：テスト設定なし（test_mode=false, base_path 空）、プレビューの痕跡なし、mail.php・.htpasswd なし、使わない表現なし",
        "",
    ]
    for name, base, where in sites:
        files = list_files(base)
        zp = OUT / f"{name}_{stamp}.zip"
        make_zip(base, files, zp)
        total = sum((base / f).stat().st_size for f in files)
        lines += [f"■ {zp.name}",
                  f"  アップロード先：{where}",
                  f"  zip サイズ：{zp.stat().st_size:,} バイト／展開後 {total:,} バイト／{len(files)} ファイル",
                  f"  SHA-256：{sha256(zp)}",
                  "  収録ファイル："]
        lines += [f"    {f}  ({(base / f).stat().st_size:,} B)" for f in files]
        lines.append("")
    snip = OUT / f"2_ebisenka-seavans.com_htaccess追記_{stamp}.txt"
    snip.write_text(HTACCESS_SNIPPET, encoding="utf-8")
    lines += [f"■ {snip.name}", "  福記美食の既存 .htaccess の先頭（# BEGIN WordPress より上）に貼り付ける",
              f"  SHA-256：{sha256(snip)}", ""]
    (OUT / f"MANIFEST_{stamp}.txt").write_text("\n".join(lines), encoding="utf-8")
    shutil.rmtree(work)
    print("\n".join(l for l in lines if not l.startswith("    ")))


if __name__ == "__main__":
    main()
