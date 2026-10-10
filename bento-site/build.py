#!/usr/bin/env python3
"""ebisenka.jp（弁当事業サイト）の静的HTMLを生成する。

使い方: python3 build.py
  src/pages/ の各ページ（先頭に <!--meta {...} --> を持つHTML断片）を
  共通レイアウトで包み、dist/ に公開用ファイル一式を出力する。
外部ライブラリは使わない（Python 3.8+ 標準ライブラリのみ）。
"""
import html
import json
import re
import shutil
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
DIST = ROOT / "dist"

SITE = {
    "url": "https://ebisenka.jp",
    "name": "海老専家 弁当事業",
    "operator": "株式会社福進発展",
    "corporate_url": "https://fukushin-e.co.jp/",
    "honten_url": "https://ebisenka-honten.jp/",
    "ogp": "/assets/img/ogp.jpg",
}

NAV = [
    ("/wholesale/", "法人向け卸"),
    ("/bento/", "弁当紹介"),
    ("/kitchen/", "製造体制・実績"),
    ("/about/", "会社案内"),
]

ORG_LD = {
    "@context": "https://schema.org",
    "@type": "Organization",
    "@id": SITE["url"] + "/#organization",
    "name": "株式会社福進発展",
    "alternateName": "Fukushin Expansion Co.,Ltd.",
    "url": SITE["corporate_url"],
    "logo": SITE["url"] + "/assets/img/logo-ink.png",
    "foundingDate": "1995-04",
    "telephone": "+81-3-3269-1461",
    "address": {
        "@type": "PostalAddress",
        "postalCode": "162-0821",
        "addressRegion": "東京都",
        "addressLocality": "新宿区",
        "streetAddress": "津久戸町4-7 OSビル1F",
        "addressCountry": "JP",
    },
    "brand": {"@type": "Brand", "name": "海老専家"},
}


def minify_css(path):
    """コメント・余分な空白を除去する簡易ミニファイ（src/ 側は読みやすいまま保つ）。"""
    css = path.read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{};,>])\s*", r"\1", css)
    css = re.sub(r";}", "}", css)
    path.write_text(css.strip() + "\n", encoding="utf-8")


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
        "{{year}}": str(date.today().year),
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
    minify_css(DIST / "assets" / "css" / "site.css")

    layout = (SRC / "layout.html").read_text(encoding="utf-8")
    urls = []
    for page in sorted((SRC / "pages").rglob("*.html")):
        meta, body = parse_page(page)
        target = out_path(meta["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(meta, body, layout), encoding="utf-8")
        if not meta.get("noindex"):
            urls.append((meta["path"], meta.get("priority", "0.6")))
        print("built", meta["path"])

    today = date.today().isoformat()
    sm = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, prio in urls:
        sm.append(f"  <url><loc>{SITE['url']}{path}</loc><lastmod>{today}</lastmod><priority>{prio}</priority></url>")
    sm.append("</urlset>")
    (DIST / "sitemap.xml").write_text("\n".join(sm) + "\n", encoding="utf-8")
    print("sitemap:", len(urls), "urls")


if __name__ == "__main__":
    build()
