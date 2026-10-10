#!/usr/bin/env python3
"""お名前.comサーバーに置く「パスワード付きプレビュー」用のファイル一式を作る（ローカルで生成するだけ）。

  python3 preview/build_package.py [--dir _preview-fk2026] [--htpasswd-path /フルパス/.htpasswd]

出力：preview-package/<dir>/   ← このフォルダごと ebisenka.jp の公開フォルダ直下に置く想定
  index.html          プレビューの入口（両サイトへのリンクと注意書き）
  bento/              弁当事業サイト（パスを /<dir>/bento/ 用に変換、フォームはテストモード）
  corporate/          コーポレートサイト（修正版。お問い合わせはテスト用スタブ）
  .htaccess           （--auth htaccess の場合のみ）Basic認証・検索エンジン除外・一覧禁止
  ※既定（--auth panel）では .htaccess を含めず、鍵はコントロールパネルの「アクセス制限」で掛ける

- パスワードやパスワードファイル（.htpasswd）はここでは作らない（Gitにも保存しない）
- 本番用の bento-site/dist/ とは別フォルダに出力し、混在させない
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "preview-package"
NOINDEX = '<meta name="robots" content="noindex, nofollow, noarchive">'
BANNER = ('<style>.pv-badge{position:fixed;right:12px;bottom:12px;z-index:9999;padding:6px 12px;border-radius:999px;'
          'background:#1d1916;color:#fbf7f1;font:700 11px/1.4 sans-serif;letter-spacing:.08em;opacity:.85;pointer-events:none}'
          '@media (max-width:860px){.pv-badge{bottom:84px}}</style>'
          '<div class="pv-badge" aria-hidden="true">PREVIEW｜非公開の確認用</div>')


def mark_preview(text):
    """全ページ共通：検索エンジン除外・canonical 削除・プレビュー表示"""
    text = re.sub(r'\s*<link rel="canonical"[^>]*>', "", text)
    text = re.sub(r'\s*<meta name="robots"[^>]*>', "", text)
    text = text.replace("<head>", "<head>\n  " + NOINDEX, 1)
    text = text.replace("</body>", BANNER + "\n</body>", 1)
    return text


def rebase_bento(text, base, corp_base):
    """弁当サイトはドメイン直下を前提に「/」始まりのパスを使っているので、プレビューの場所に合わせて書き換える"""
    # 先に「/」始まりのパスを書き換え、その後で他ドメインへのリンクを置き換える（二重に付けないため）
    text = re.sub(r'((?:href|src|action)=")/(?!/)', r"\1" + base + "/", text)
    text = text.replace("https://fukushin-e.co.jp/", corp_base + "/")
    text = text.replace("https://ebisenka.jp/", base + "/")

    def fix_srcset(m):
        items = [base + i if i.startswith("/") else i for i in m.group(1).split(", ")]
        return 'srcset="' + ", ".join(items) + '"'

    return re.sub(r'srcset="([^"]+)"', fix_srcset, text)


def build_bento(dest, base, corp_base):
    with tempfile.TemporaryDirectory() as tmp:
        # 本番と同じビルドを一時フォルダに出力し、それを変換する（本番用 dist には触れない）
        subprocess.run([sys.executable, str(ROOT / "bento-site" / "build.py"), str(Path(tmp) / "dist")],
                       check=True, stdout=subprocess.DEVNULL)
        shutil.copytree(Path(tmp) / "dist", dest)
    for f in ("sitemap.xml", "robots.txt", ".htaccess"):
        (dest / f).unlink(missing_ok=True)
    for page in dest.rglob("*.html"):
        page.write_text(mark_preview(rebase_bento(page.read_text(encoding="utf-8"), base, corp_base)), encoding="utf-8")
    cfg = dest / "contact" / "config.php"
    c = cfg.read_text(encoding="utf-8")
    c = c.replace("'test_mode' => false,", "'test_mode' => true,")
    c = c.replace("'base_path' => '',", f"'base_path' => '{base}',")
    assert "'test_mode' => true," in c and f"'base_path' => '{base}'," in c
    cfg.write_text(c, encoding="utf-8")


def build_corporate(dest, bento_base):
    shutil.copytree(ROOT / "corporate", dest)
    for page in dest.glob("*.html"):
        t = page.read_text(encoding="utf-8")
        t = t.replace("https://ebisenka.jp/", bento_base + "/")
        # 本番の mail.php は使わない・上書きしない：プレビュー専用のテスト用スタブへ送る
        t = t.replace('action="mail.php"', 'action="preview-mail.php"')
        page.write_text(mark_preview(t), encoding="utf-8")
    (dest / "preview-mail.php").write_text(CORP_STUB, encoding="utf-8")


CORP_STUB = """<?php
// プレビュー専用：コーポレートサイトのお問い合わせフォームのテスト用。メールは送信せず、入力内容も保存しない。
header('Content-Type: text/html; charset=UTF-8');
header('X-Robots-Tag: noindex, nofollow');
$fields = [];
foreach ($_POST as $k => $v) {
    if (is_string($v)) {
        $fields[] = '<tr><th style="text-align:left;padding:6px 12px">' . htmlspecialchars($k, ENT_QUOTES, 'UTF-8')
            . '</th><td style="padding:6px 12px">' . nl2br(htmlspecialchars($v, ENT_QUOTES, 'UTF-8')) . '</td></tr>';
    }
}
echo '<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="robots" content="noindex, nofollow">'
    . '<meta name="viewport" content="width=device-width, initial-scale=1"><title>テスト送信｜プレビュー</title>'
    . '<link rel="stylesheet" href="./assets/css/style.css"></head><body><main class="section" style="max-width:760px;margin:40px auto;padding:0 16px">'
    . '<h1>テスト送信（プレビュー）</h1><p>プレビュー環境のため、メールは送信していません。入力内容はサーバーに保存していません。'
    . '本番では既存の送信処理（mail.php）で処理されます。</p><table>' . implode('', $fields) . '</table>'
    . '<p><a href="./contact.html">フォームへ戻る</a></p></main></body></html>';
"""

HTACCESS = """# ===== プレビュー専用（{dir}）=====
# 本番公開時はこのフォルダごと削除する

# --- Basic認証（パスワードファイルは公開フォルダの外に置く。パスワードはGitに保存しない）---
AuthType Basic
AuthName "Preview"
AuthUserFile {htpasswd}
Require valid-user

# --- 検索エンジンに登録させない ---
<IfModule mod_headers.c>
  Header always set X-Robots-Tag "noindex, nofollow, noarchive"
  Header always set Cache-Control "no-store"
</IfModule>

# --- ファイル一覧の表示・設定ファイルへの直接アクセスを禁止 ---
Options -Indexes
<FilesMatch "^(config\\.php|\\.ht.*)$">
  <IfModule mod_authz_core.c>
    Require all denied
  </IfModule>
  <IfModule !mod_authz_core.c>
    Order allow,deny
    Deny from all
  </IfModule>
</FilesMatch>

ErrorDocument 404 /{dir}/bento/404.html
"""

INDEX = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
{noindex}
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>プレビュー｜弁当事業サイト・コーポレートサイト</title>
<link rel="stylesheet" href="./bento/assets/css/site.css">
</head><body>
<main class="section"><div class="wrap" style="max-width:820px">
<p class="eyebrow">PREVIEW</p>
<h1 class="h2">確認用プレビュー（非公開）</h1>
<p class="lead" style="margin:20px 0 32px">株式会社福進発展 ご担当者様向けの確認用ページです。URL・ID・パスワードは社外に共有しないでください。</p>
<ul class="compartments compartments--2">
  <li><h2 style="font-size:18px">弁当事業サイト</h2><p>公開予定：https://ebisenka.jp/</p><p class="btn-row" style="margin-top:16px"><a class="btn btn--red" href="./bento/">弁当事業サイトを見る</a></p></li>
  <li><h2 style="font-size:18px">コーポレートサイト（修正版）</h2><p>公開中：https://fukushin-e.co.jp/ の修正後の状態</p><p class="btn-row" style="margin-top:16px"><a class="btn btn--ghost" href="./corporate/">コーポレートサイトを見る</a></p></li>
</ul>
<div class="notice" style="margin-top:32px">
<p><strong>お問い合わせフォームについて</strong>：プレビューではメールは送信されません。送信ボタンを押すと、本番で届くメールの内容が画面に表示されます。<br>テストの際は、実在の個人情報ではなく「テスト株式会社／test@example.com」などの架空の内容を入力してください。</p>
</div>
<p class="note" style="margin-top:24px">確認ポイント：PC とスマートフォンでの表示、スクロール時の表示演出、メニューの開閉、ページ間の移動、コーポレートサイトの「弁当事業の詳細を見る」から弁当サイトへの移動。</p>
</div></main>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="_preview-fk2026", help="プレビュー用フォルダ名（ebisenka.jp 直下に作る）")
    ap.add_argument("--auth", choices=["panel", "htaccess"], default="panel",
                    help="panel＝コントロールパネルの「アクセス制限」で鍵を掛ける（推奨・.htaccess を含めない）／"
                         "htaccess＝.htaccess と .htpasswd で鍵を掛ける")
    ap.add_argument("--htpasswd-path", default="/REPLACE/WITH/FULL/PATH/OUTSIDE/PUBLIC/.htpasswd_preview",
                    help="--auth htaccess の場合のみ：サーバー上の .htpasswd のフルパス（公開フォルダの外）")
    ap.add_argument("--skip-live-check", action="store_true", help="公開中コーポレートとの差分確認を省略（通常は使わない）")
    args = ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.dir):
        sys.exit("--dir は英数字・_・- のみ")

    # 正本（お名前.com で公開中のデータ）が開発用コピーの取り込み時点から変わっていないか確認
    if not args.skip_live_check:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import check_live
        print("公開中のコーポレートサイト（正本）を確認しています…")
        if check_live.check():
            sys.exit("公開中データが取り込み時点から変わっています。正本から修正版を作り直すまでパッケージは作りません。")

    base = "/" + args.dir
    out = OUT_ROOT / args.dir
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    build_bento(out / "bento", base + "/bento", base + "/corporate")
    build_corporate(out / "corporate", base + "/bento")
    (out / "index.html").write_text(INDEX.format(noindex=NOINDEX), encoding="utf-8")
    if args.auth == "htaccess":
        (out / ".htaccess").write_text(HTACCESS.format(dir=args.dir, htpasswd=args.htpasswd_path), encoding="utf-8")

    # panel 方式では、フォルダ内のどこにも .htaccess があってはならない（コントロールパネルの制限が無効になるため）
    stray = [p for p in out.rglob(".htaccess") if not (args.auth == "htaccess" and p == out / ".htaccess")]
    if stray:
        sys.exit(f"想定外の .htaccess があります：{stray}")

    archive = shutil.make_archive(str(OUT_ROOT / f"{args.dir}-{args.auth}"), "zip", root_dir=OUT_ROOT, base_dir=args.dir)
    files = [p for p in out.rglob("*") if p.is_file()]
    size = sum(p.stat().st_size for p in files)
    print(f"作成しました：{out}  （{len(files)} ファイル・{size / 1024 / 1024:.1f}MB、鍵：{args.auth}）")
    print(f"アップロード用 zip：{archive}")
    print(f"想定URL：https://ebisenka.jp{base}/")
    if args.auth == "htaccess" and "REPLACE" in args.htpasswd_path:
        print("※ .htaccess の AuthUserFile は仮の値です。サーバーのフルパス確認後に --htpasswd-path を指定して作り直してください")

if __name__ == "__main__":
    main()
