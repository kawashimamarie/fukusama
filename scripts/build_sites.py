#!/usr/bin/env python3
"""3サイトの公開成果物を、ソースから同じ内容で作り直す（サーバーには何も送らない）。

  python3 scripts/build_sites.py [出力先（既定 out/）] [--site ebisenka|fukuki|corporate ...]

出力：
  out/ebisenka.jp/            海老専家 法人向け弁当事業（bento-site/ から生成）
  out/ebisenka-seavans.com/   福記美食（fukuki-site/ から生成）
  out/fukushin-e.co.jp/       コーポレート（corporate/ の公開ファイル一式）
  out/build-manifest.json     サイトごとのファイル一覧・SHA-256・成果物全体のハッシュ・元のコミット

- 日付（フッターの年・sitemap の lastmod）は「そのサイトのソースを最後に変えたコミットの日付（日本時間）」。
  同じコミットからは何度作っても同じバイト列になる（自動公開の差分判定が、変更のないサイトを公開しないため）
- 公開の可否・アップロード先は、このリポジトリではなく、デプロイ専用リポジトリの設定が決める
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# サイトごとのソース（日付の判定に使うパス。生成済みの dist/ は含めない）
SITES = {
    "ebisenka": {
        "domain": "ebisenka.jp",
        "builder": "bento-site",
        "sources": ["bento-site", ":(exclude)bento-site/dist"],
    },
    "fukuki": {
        "domain": "ebisenka-seavans.com",
        "builder": "fukuki-site",
        # 共通デザイン（CSS・JS）は弁当事業サイトのものを使う
        "sources": ["fukuki-site", ":(exclude)fukuki-site/dist", ":(exclude)fukuki-site/live-backup-20261010",
                    "bento-site/src/assets/css/site.css", "bento-site/src/assets/js/site.js"],
    },
    "corporate": {
        "domain": "fukushin-e.co.jp",
        "copy": "corporate",
        "sources": ["corporate"],
    },
}

# コーポレートのソースに置かれていても公開しないもの（念のため）
COPY_EXCLUDE = {".DS_Store", "Thumbs.db"}


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True,
                          env={**os.environ, "TZ": "Asia/Tokyo"}).stdout.strip()


def site_date(site):
    """そのサイトのソースを最後に変更したコミットの日付（日本時間 YYYY-MM-DD）。
    作業ツリーに未コミットの変更があっても、日付はコミットから決める（CI と同じ結果にするため）。"""
    d = git("log", "-1", "--format=%cd", "--date=format-local:%Y-%m-%d", "--", *SITES[site]["sources"])
    if not d:
        raise SystemExit(f"{site}: ソースのコミット履歴が見つかりません（浅い clone の場合は fetch-depth: 0 にしてください）")
    return d


def sha256_file(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree_files(base):
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())


def build_site(site, dest):
    cfg = SITES[site]
    if dest.exists():
        shutil.rmtree(dest)
    if "builder" in cfg:
        env = {**os.environ, "BUILD_DATE": site_date(site)}
        subprocess.run([sys.executable, "build.py", str(dest)], cwd=ROOT / cfg["builder"], check=True,
                       env=env, stdout=subprocess.DEVNULL)
    else:
        src = ROOT / cfg["copy"]
        for rel in tree_files(src):
            if Path(rel).name in COPY_EXCLUDE:
                continue
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src / rel, dest / rel)
    files = {rel: sha256_file(dest / rel) for rel in tree_files(dest)}
    tree = hashlib.sha256("".join(f"{k}\0{v}\n" for k, v in files.items()).encode()).hexdigest()
    return {"domain": cfg["domain"], "date": site_date(site), "fileCount": len(files),
            "treeSha256": tree, "files": files}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default=str(ROOT / "out"))
    ap.add_argument("--site", action="append", choices=sorted(SITES))
    a = ap.parse_args()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": 1, "sourceCommit": git("rev-parse", "HEAD"), "sites": {}}
    for site in a.site or list(SITES):
        info = build_site(site, out / SITES[site]["domain"])
        manifest["sites"][site] = info
        print(f"{site:9s} {info['domain']:22s} {info['fileCount']:3d} files  date={info['date']}  tree={info['treeSha256'][:16]}")
    (out / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
