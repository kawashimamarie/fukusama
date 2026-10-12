#!/usr/bin/env python3
"""ebisenka-seavans.com（福記美食）の静的HTMLを生成する。

デザインの共通部分は bento-site/src/assets/css/site.css を読み込み、
福記美食固有の上書きは src/assets/css/fukuki.css に書く（ビルド時に1ファイルへ結合）。

使い方: python3 build.py [出力先フォルダ]
  src/pages/ の各ページ（先頭に <!--meta {...} --> を持つHTML断片）を
  共通レイアウトで包み、dist/ に公開用ファイル一式を出力する。
外部ライブラリは使わない（Python 3.8+ 標準ライブラリのみ）。
"""
import html
import json
import os
import re
import shutil
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
DIST = ROOT / "dist"

SITE = {
    "url": "https://ebisenka-seavans.com",
    "name": "福記美食",
    "operator": "株式会社福進発展",
    "corporate_url": "https://fukushin-e.co.jp/",
    "ogp": "/assets/img/ogp.jpg",
}

NAV = [
    ("/bento/", "お弁当の店頭販売"),
    ("/kitchen/", "セントラルキッチン"),
    ("/wholesale/", "法人向け製造・卸"),
    ("/access/", "店舗・アクセス"),
]

ORG_LD = {
    "@context": "https://schema.org",
    "@type": "FoodEstablishment",
    "@id": SITE["url"] + "/#store",
    "name": "福記美食",
    "description": "セントラルキッチンでの弁当製造と、お弁当の店頭販売を行う福記美食の拠点。法人向け弁当の製造・卸も行っています。",
    "url": SITE["url"] + "/",
    "logo": SITE["url"] + "/assets/img/fukuki-logo.png",
    "image": SITE["url"] + "/assets/img/ogp.jpg",
    "telephone": "+81-3-6812-8086",
    "address": {
        "@type": "PostalAddress",
        "addressRegion": "東京都",
        "addressLocality": "港区",
        "streetAddress": "芝浦1丁目2-2 シーバンス ア・モール 2F",
        "addressCountry": "JP",
    },
    "parentOrganization": {
        "@type": "Organization",
        "name": "株式会社福進発展",
        "url": "https://fukushin-e.co.jp/",
    },
}


# 日付（フッターの年・sitemap の lastmod）。同じソースから同じ成果物を作るため、
# scripts/build_sites.py が「このサイトを最後に変更したコミットの日付」を BUILD_DATE で渡す。
BUILD_DATE = os.environ.get("BUILD_DATE") or date.today().isoformat()

def minify_css(path):
    """コメント・余分な空白を除去する簡易ミニファイ（src/ 側は読みやすいまま保つ）。"""
    css = path.read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{};,>])\s*", r"\1", css)
    css = re.sub(r";}", "}", css)
    path.write_text(css.strip() + "\n", encoding="utf-8")


IMG_DIR = SRC / "assets" / "img"
IMG_RE = re.compile(r'\[\[img (?P<name>[\w-]+) alt="(?P<alt>[^"]*)" sizes="(?P<sizes>[^"]*)"(?P<eager> eager)?(?: class="(?P<cls>[^"]*)")?\]\]')


def expand_images(body):
    """[[img 名前 alt="..." sizes="..." (eager)]] を srcset 付きの <img> に展開する。
    名前-<幅>.webp の全サイズを srcset に入れ、width/height は最大サイズから取る。"""
    from PIL import Image  # 画像寸法の取得にのみ使用

    def repl(m):
        pat = re.compile(re.escape(m["name"]) + r"-\d+$")
        files = sorted((f for f in IMG_DIR.glob(m["name"] + "-*.webp") if pat.match(f.stem)),
                       key=lambda f: int(f.stem.rsplit("-", 1)[1]))
        if not files:
            raise SystemExit("image not found: " + m["name"])
        largest = files[-1]
        w, h = Image.open(largest).size
        srcset = ", ".join(f"/assets/img/{f.name} {int(f.stem.rsplit('-', 1)[1])}w" for f in files)
        mid = files[min(1, len(files) - 1)]
        attrs = [f'src="/assets/img/{mid.name}"', f'srcset="{srcset}"', f'sizes="{m["sizes"]}"',
                 f'width="{w}"', f'height="{h}"', f'alt="{html.escape(m["alt"])}"']
        attrs.append('fetchpriority="high"' if m["eager"] else 'loading="lazy" decoding="async"')
        if m["cls"]:
            attrs.append(f'class="{m["cls"]}"')
        return "<img " + " ".join(attrs) + ">"

    return IMG_RE.sub(repl, body)


def parse_page(path):
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"\s*<!--meta\s*(\{.*?\})\s*-->\s*", raw, re.S)
    if not m:
        raise SystemExit(f"meta comment missing: {path}")
    return json.loads(m.group(1)), raw[m.end():]


def out_path(url_path):
    if url_path.endswith("/"):
        return DIST / url_path.lstrip("/") / "index.html"
    return DIST / url_path.lstrip("/")


def breadcrumb_html(crumbs):
    if not crumbs:
        return ""
    items = ['<li><a href="/">トップ</a></li>']
    for i, (url, label) in enumerate(crumbs):
        if i == len(crumbs) - 1:
            items.append(f'<li aria-current="page">{html.escape(label)}</li>')
        else:
            items.append(f'<li><a href="{url}">{html.escape(label)}</a></li>')
    return '<nav class="breadcrumb wrap" aria-label="パンくずリスト"><ol>' + "".join(items) + "</ol></nav>"


def breadcrumb_ld(crumbs):
    elems = [{"@type": "ListItem", "position": 1, "name": "トップ", "item": SITE["url"] + "/"}]
    for i, (url, label) in enumerate(crumbs, start=2):
        elems.append({"@type": "ListItem", "position": i, "name": label, "item": SITE["url"] + url})
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": elems}


def ld_script(obj):
    return '<script type="application/ld+json">' + json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "</script>"


def render(meta, body, layout):
    url_path = meta["path"]
    canonical = SITE["url"] + url_path
    crumbs = [tuple(c) for c in meta.get("breadcrumb", [])]
    lds = [ORG_LD]
    if url_path == "/":
        lds.append({
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": SITE["name"],
            "url": SITE["url"] + "/",
            "inLanguage": "ja",
            "publisher": {"@id": ORG_LD["@id"]},
        })
    if crumbs:
        lds.append(breadcrumb_ld(crumbs))
    for extra in meta.get("ld", []):
        lds.append(extra)

    nav_items = []
    for href, label in NAV:
        current = ' aria-current="page"' if url_path.startswith(href) else ""
        nav_items.append(f'<li><a href="{href}"{current}>{label}</a></li>')

    robots = '<meta name="robots" content="noindex">' if meta.get("noindex") else ""
    replacements = {
        "{{title}}": html.escape(meta["title"]),
        "{{description}}": html.escape(meta["description"]),
        "{{canonical}}": canonical,
        "{{og_type}}": "website" if url_path == "/" else "article",
        "{{og_image}}": SITE["url"] + meta.get("ogp", SITE["ogp"]),
        "{{robots}}": robots,
        "{{ld}}": "\n  ".join(ld_script(x) for x in lds),
        "{{nav}}": "".join(nav_items),
        "{{breadcrumb}}": breadcrumb_html(crumbs),
        "{{body_class}}": meta.get("body_class", ""),
        "{{year}}": BUILD_DATE[:4],
        "{{content}}": body,
    }
    out = layout
    for k, v in replacements.items():
        out = out.replace(k, v)
    return out


def build():
    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(SRC / "assets", DIST / "assets")
    # robots.txt / .htaccess / contact/send.php など、そのまま公開するファイル
    shutil.copytree(SRC / "static", DIST, dirs_exist_ok=True)
    # 共通デザイン（弁当事業サイトの site.css）＋福記美食の上書き（fukuki.css）を結合
    common = (ROOT.parent / "bento-site" / "src" / "assets" / "css" / "site.css").read_text(encoding="utf-8")
    own = (SRC / "assets" / "css" / "fukuki.css").read_text(encoding="utf-8")
    (DIST / "assets" / "css" / "site.css").write_text(common + "\n" + own, encoding="utf-8")
    (DIST / "assets" / "css" / "fukuki.css").unlink()
    minify_css(DIST / "assets" / "css" / "site.css")
    (DIST / "assets" / "js").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT.parent / "bento-site" / "src" / "assets" / "js" / "site.js", DIST / "assets" / "js" / "site.js")

    layout = (SRC / "layout.html").read_text(encoding="utf-8")
    urls = []
    for page in sorted((SRC / "pages").rglob("*.html")):
        meta, body = parse_page(page)
        body = body.replace("[[bg-kitchen]]", re.sub(r"<!--.*?-->\s*", "", (SRC / "partials" / "bg-kitchen.html").read_text(encoding="utf-8"), flags=re.S).strip())
        body = expand_images(body)
        target = out_path(meta["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(meta, body, layout), encoding="utf-8")
        if not meta.get("noindex"):
            urls.append((meta["path"], meta.get("priority", "0.6")))
        print("built", meta["path"])

    today = BUILD_DATE
    sm = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, prio in urls:
        sm.append(f"  <url><loc>{SITE['url']}{path}</loc><lastmod>{today}</lastmod><priority>{prio}</priority></url>")
    sm.append("</urlset>")
    (DIST / "sitemap.xml").write_text("\n".join(sm) + "\n", encoding="utf-8")
    print("sitemap:", len(urls), "urls")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:  # 出力先を変える場合（プレビュー用パッケージ作成時など）。省略時は dist/
        DIST = Path(sys.argv[1]).resolve()
    build()
