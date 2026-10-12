#!/usr/bin/env python3
"""公開成果物（scripts/build_sites.py の出力）の検査。1つでも問題があれば終了コード 1。

  python3 scripts/qa/check_sites.py [out/]

- 公開の仕組みと同じ基準（deploy-repo/deployer/config.py）で、公開してはいけないファイルがないか
- HTML：lang・title・description・canonical、同じサイト内の参照（画像・CSS・JS・リンク）がすべて存在するか
- robots.txt・sitemap.xml（載っている URL が存在するか）
- 秘密情報らしき文字列・プレビューやテストの痕跡・使わない表現（毎日／最大 1,500 等）
- 弁当事業サイトのフォーム設定（受信先・送信元・テストモード）
"""
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy-repo"))
from deployer import config as C  # noqa: E402
from deployer import core  # noqa: E402

FORBIDDEN_TEXT = ["_preview-fk2026", "pv-badge", "localhost", "127.0.0.1", "noreply@ebisenka.jp", "preview-mail",
                  "PREVIEW｜非公開"]
CLAIMS = re.compile(r"毎日\s*1,?500|1日(最大)?\s*1,?500|最大\s*1,?500|常時\s*1,?500|製造能力|どんな大量注文")
SECRETS = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|github_pat_|AKIA[0-9A-Z]{16}|"
                     r"FTPS_[A-Z]+_PASSWORD\s*[=:]|password\s*[=:]\s*['\"][^'\"]{6,}")
NOINDEX_OK = {"404.html", "contact/thanks/index.html"}
# サーバー側にある既存のファイルを指す参照（ビルドには含めない・公開の仕組みが保護する）
EXTERNAL_OK = {"fukushin-e.co.jp": {"mail.php"}}


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs, self.meta, self.lang, self.title, self.canonical = [], {}, None, "", None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "title":
            self._in_title = True
        if tag == "meta" and a.get("name"):
            self.meta[a["name"].lower()] = a.get("content", "")
        if tag == "link" and a.get("rel") == "canonical":
            self.canonical = a.get("href")
        for k in ("src", "href", "action", "poster"):
            if a.get(k):
                self.refs.append((tag, a[k]))
        for k in ("srcset", "imagesrcset"):
            if a.get(k):
                self.refs += [(tag, s.strip().split(" ")[0]) for s in a[k].split(",") if s.strip()]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, d):
        if self._in_title:
            self.title += d


def url_to_file(site_dir, path):
    p = path.lstrip("/")
    cand = [p] if p and not p.endswith("/") else [p + "index.html"]
    if p and not p.endswith("/") and "." not in Path(p).name:
        cand.append(p + "/index.html")
    return next((c for c in cand if (site_dir / c).is_file()), None)


def check_site(site, site_dir, problems):
    def bad(msg):
        problems.append(f"{site.domain}: {msg}")

    try:
        files = core.load_build(site, site_dir)
    except core.Stop as e:
        bad(f"公開の仕組みの基準に不合格：{e}")
        return
    host = site.domain
    for rel in files:
        f = site_dir / rel
        if f.suffix in (".html", ".css", ".js", ".txt", ".xml", ".php") or f.name == ".htaccess":
            text = f.read_text("utf-8", "replace")
            for s in FORBIDDEN_TEXT:
                if s in text:
                    bad(f"{rel} に「{s}」")
            m = SECRETS.search(text)
            if m:
                bad(f"{rel} に秘密情報らしき文字列")
            if f.suffix == ".html":
                m = CLAIMS.search(re.sub(r"<[^>]+>", "", text))
                if m:
                    bad(f"{rel} に使わない表現「{m.group(0)}」")
        if f.suffix != ".html":
            continue
        pg = Page()
        pg.feed(text)
        if pg.lang != "ja":
            bad(f"{rel}：lang=\"ja\" がない")
        if not pg.title.strip():
            bad(f"{rel}：title がない")
        if not pg.meta.get("description"):
            bad(f"{rel}：meta description がない")
        noindex = "noindex" in pg.meta.get("robots", "")
        if noindex and rel not in NOINDEX_OK:
            bad(f"{rel}：noindex が入っている")
        if pg.canonical and urlparse(pg.canonical).netloc not in (host, "www." + host, ""):
            bad(f"{rel}：canonical が別のドメイン（{pg.canonical}）")
        if host != "fukushin-e.co.jp" and not pg.canonical and rel not in NOINDEX_OK:
            bad(f"{rel}：canonical がない")
        page_url = f"https://{host}/" + (rel[: -len("index.html")] if rel.endswith("index.html") else rel)
        for tag, ref in pg.refs:
            if ref.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
                continue
            u = urlparse(urljoin(page_url, ref))
            if u.netloc != host or u.scheme not in ("http", "https"):
                continue
            target = url_to_file(site_dir, u.path)
            if target is None and u.path.lstrip("/") not in EXTERNAL_OK.get(host, set()):
                bad(f"{rel}：参照先がない {ref}")
    # robots・sitemap
    if (site_dir / "sitemap.xml").is_file():
        for loc in re.findall(r"<loc>([^<]+)</loc>", (site_dir / "sitemap.xml").read_text()):
            u = urlparse(loc)
            if u.netloc != host or url_to_file(site_dir, u.path) is None:
                bad(f"sitemap.xml：存在しない URL {loc}")
        if f"Sitemap: https://{host}/sitemap.xml" not in (site_dir / "robots.txt").read_text():
            bad("robots.txt に sitemap の記載がない")
    elif site.id != "corporate":
        bad("sitemap.xml がない")


def check_form(site_dir, problems):
    cfg = (site_dir / "contact/config.php").read_text()
    for k, want in {"to": "info@fukushin-e.co.jp", "from": "info@fukushin-e.co.jp"}.items():
        if f"'{k}' => '{want}'" not in cfg:
            problems.append(f"ebisenka.jp: config.php の {k} が {want} ではない")
    for s in ("'test_mode' => false", "'base_path' => ''"):
        if s not in cfg:
            problems.append(f"ebisenka.jp: config.php が本番用の値ではない（{s}）")
    send = (site_dir / "contact/send.php").read_text()
    if "'Reply-To: ' . $data['email']" not in send:
        problems.append("ebisenka.jp: Reply-To がお客様のアドレスになっていない")
    if re.search(r"From:[^\n]*\$data\['email'\]", send):
        problems.append("ebisenka.jp: From にお客様のアドレスが入っている")


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "out")
    problems = []
    for site in C.SITES.values():
        d = out / site.domain
        if not d.is_dir():
            problems.append(f"{site.domain}: ビルドがない")
            continue
        check_site(site, d, problems)
    check_form(out / "ebisenka.jp", problems)
    if problems:
        print("【不合格】\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("公開成果物の検査：合格（" + "・".join(C.SITES[s].domain for s in C.SITES) + "）")


if __name__ == "__main__":
    main()
