"""公開先と安全装置の設定（デプロイ専用リポジトリにだけ置く。変更はご本人の承認が必要な「仕組みの変更」）。

ここに書いた内容が、公開できる範囲のすべて：
- 3サイトの公開先（サイトごとに、接続先ディレクトリを限定した別々の FTP アカウント。ログイン直後の場所が公開フォルダ）
- 書き込んではいけないファイル（WordPress・mail.php・.htaccess など）
- 変わると承認が必要なファイル・件数の上限
- 公開後の検証（ページ・転送・見えてはいけないURL・PHP の非送信テスト）
fukusama リポジトリ（Claude Code が作業する公開リポジトリ）のビルド結果は「データ」としてしか扱わない。
"""
import os
import re
from dataclasses import dataclass, field

# 1回の公開でこれを超えたら、自動では公開せず承認を求める
MAX_AUTO_FILES = 80
MAX_AUTO_BYTES = 30 * 1024 * 1024

# 変わると承認が必要な「フォームに関わるファイル」の目印（.html・.php・.js の、変更前か変更後に含まれていれば対象）。
# approval_paths に書いていないページでも、フォームや送信先を含むなら自動では公開しない
FORM_MARKERS = (b"<form", b"mail.php", b"send.php")
FORM_SCAN_EXT = (".html", ".php", ".js")

# 公開してよいファイルの種類（これ以外がビルドに含まれていたら止まる）
ALLOWED_EXT = {
    ".html", ".css", ".js", ".webp", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".txt", ".xml", ".woff2", ".pdf", ".mp4",
}
MAX_FILE_BYTES = 15 * 1024 * 1024

# 福記美食：WordPress を残したまま静的サイトに切り替えるための .htaccess 先頭の設定。
# 公開の仕組み（このファイル）だけが書き込む。fukusama のビルドからは .htaccess を受け付けない。
FUKUKI_HTACCESS_BLOCK = """# ===== 福記美食 リニューアル版への切り替え（ここから）=====
# 既存の「# BEGIN WordPress」より上に置く。この囲みを削除し、追加したファイルを外せば、元の WordPress の表示に戻る。
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


@dataclass(frozen=True)
class Probe:
    """公開後に確かめる HTTP の動き（メールは送らない）"""
    method: str
    path: str
    expect_status: tuple
    headers: tuple = ()
    body: str = ""
    note: str = ""


@dataclass(frozen=True)
class Site:
    id: str
    domain: str
    base_url: str
    secret_prefix: str                 # FTPS_<...>_HOST / _USER / _PASSWORD
    # ログイン直後の一覧が、どれか1組の目印をすべて含むこと（[] は「空のフォルダ」を意味する＝初回だけ）
    root_markers: tuple
    # 書き込み・削除をしないファイル（サーバー上にあっても触れない。ビルドに含まれていたら止まる）
    protected: tuple
    # ビルドに含めてよい「ドットファイル・PHP」（それ以外のドットファイル・PHP は止まる）
    allowed_special: tuple = ()
    # 変わると承認が必要なファイル（.htaccess など）
    approval_paths: tuple = ()
    # 接続先が公開中のサイトそのものかを確かめるファイル（FTPS と公開URLで同じ中身なら合格。最初に見つかったもの）
    identity_files: tuple = ()
    pages: tuple = ()                  # 公開後に 200 を確かめるページ
    redirects: tuple = ()              # (パス, 状態, 転送先) 公開後に確かめる
    hidden: tuple = ()                 # 中身が見えてはいけないURL（403・404・空）
    probes: tuple = ()
    htaccess_block: str = ""           # 福記美食だけ
    form_paths: tuple = ()             # 変わったら「実送信テストが必要」と報告する
    # 公開フォルダの直下に最初からある、サーバー側のもの（目印の判定では数えない。ビルドに含めない・触れない）
    root_ignore: tuple = ()
    notes: tuple = field(default_factory=tuple)


DOT_AND_PHP = (".*", "*/.*", "*.php", "*.phtml", "*.cgi", "*.pl", "*.py", "*.sh", ".user.ini", "*/.user.ini")

SITES = {
    "ebisenka": Site(
        id="ebisenka",
        domain="ebisenka.jp",
        base_url="https://ebisenka.jp",
        secret_prefix="FTPS_EBISENKA",
        root_markers=(("index.html", "wholesale/", "contact/"), ()),
        protected=DOT_AND_PHP,
        allowed_special=(".htaccess", "contact/send.php", "contact/config.php"),
        # 自動では公開せず、承認（手動の publish）を待つ：安全設定・メール送信・送信先・フォーム・個人情報
        approval_paths=(".htaccess", "contact/send.php", "contact/config.php", "contact/index.html",
                        "contact/thanks/index.html", "privacy/index.html"),
        identity_files=("index.html",),
        pages=("/", "/wholesale/", "/bento/", "/kitchen/", "/about/", "/contact/", "/contact/thanks/",
               "/privacy/", "/robots.txt", "/sitemap.xml"),
        redirects=(),
        hidden=("/contact/config.php", "/.htaccess"),
        probes=(
            # 送信処理が PHP として動き、他サイトからの送信を拒否すること（メールは送らない）
            Probe("POST", "/contact/send.php", (403,), headers=(("Origin", "https://invalid.example"),),
                  body="email=check%40example.com", note="フォーム：他サイトからの送信を拒否（非送信）"),
        ),
        form_paths=("contact/send.php", "contact/config.php", "contact/index.html"),
        # お名前.com が作るエラーページ用のフォルダ（2026-10-12 の確認のみで、公開フォルダの中はこれだけ）
        root_ignore=("error/",),
    ),
    "fukuki": Site(
        id="fukuki",
        domain="ebisenka-seavans.com",
        base_url="https://ebisenka-seavans.com",
        secret_prefix="FTPS_FUKUKI",
        # 切り替え前（WordPress）・切り替え後（静的サイト。WordPress のファイルは残る）
        root_markers=(("wp-content/", "wp-includes/", "wp-admin/"), ("index.html", "bento/", "kitchen/")),
        protected=DOT_AND_PHP + ("wp-*", "wp-*/*", "xmlrpc.php", "license.txt", "readme.html", "cgi-bin", "cgi-bin/*"),
        allowed_special=(),
        identity_files=("index.html", "wp-includes/js/wp-emoji-loader.min.js"),
        pages=("/", "/bento/", "/kitchen/", "/wholesale/", "/access/", "/contact/", "/robots.txt", "/sitemap.xml"),
        redirects=(("/lunch/", 301, "/bento/"), ("/food/", 301, "/bento/"), ("/commitment/", 301, "/kitchen/"),
                   ("/info/", 301, "/access/"), ("/party/", 301, "/"), ("/news/", 301, "/")),
        hidden=("/wp-config.php",),
        htaccess_block=FUKUKI_HTACCESS_BLOCK,
    ),
    "corporate": Site(
        id="corporate",
        domain="fukushin-e.co.jp",
        base_url="https://fukushin-e.co.jp",
        secret_prefix="FTPS_CORPORATE",
        root_markers=(("index.html", "business.html", "company.html", "recruit.html", "assets/"),),
        # 既存のお問い合わせフォーム（mail.php）・.htaccess・PHP には触れない
        protected=DOT_AND_PHP + ("mail*", "cgi-bin", "cgi-bin/*"),
        identity_files=("index.html", "assets/css/style.css"),
        approval_paths=("contact.html",),     # お問い合わせフォーム（送信先は mail.php・自動公開の対象外）
        pages=("/", "/business.html", "/restaurant.html", "/liquor.html", "/food-import.html",
               "/company.html", "/recruit.html", "/contact.html"),
        form_paths=("contact.html",),
    ),
}


# ---------------------------------------------------------------- 記録・通知に出す文の伏せ字
_SECRET_ENV = re.compile(r"^FTPS_[A-Z]+_(HOST|USER|PASSWORD|PARENT_ABS)$")
_TOKEN_LIKE = re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
_URL_CRED = re.compile(r"(://)[^/@\s:]+(:[^/@\s]*)?@")


def redact(text):
    """接続情報（環境変数の値）・トークンらしき文字列・URL の中の認証情報を伏せる"""
    if not isinstance(text, str):
        return text
    for k, v in os.environ.items():
        if _SECRET_ENV.match(k):
            for part in [v] + [x for x in v.split(";") if x.strip()]:
                if len(part.strip()) >= 4:
                    text = text.replace(part.strip(), "***")
    text = _TOKEN_LIKE.sub("***", text)
    return _URL_CRED.sub(r"\1***@", text)


def redact_all(obj):
    if isinstance(obj, dict):
        return {k: redact_all(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_all(v) for v in obj]
    return redact(obj)
