#!/usr/bin/env python3
"""公開中のコーポレートサイト（正本）と、GitHub の開発用コピーの差分を確認する（読み取りのみ）。

  python3 preview/check_live.py

正本はお名前.com 上で公開中のデータ。GitHub の corporate/ は開発用コピーで、
取り込み時点（コミット a6ba69e）の公開データに修正を加えたもの。
公開中データが取り込み時点から変わっていれば、修正版を作り直す必要があるため警告する。
終了コード：0＝変化なし、1＝公開中データに変化あり（または取得失敗）
"""
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = "a6ba69e"  # 公開データを取り込んだコミット
LIVE = "https://fukushin-e.co.jp/"
FILES = ["index.html", "business.html", "company.html", "contact.html", "food-import.html",
         "liquor.html", "recruit.html", "restaurant.html", "assets/css/style.css", "assets/js/main.js"]


def baseline(path):
    return subprocess.run(["git", "-C", str(ROOT), "show", f"{BASELINE}:corporate/{path}"],
                          check=True, capture_output=True).stdout


def live(path):
    req = urllib.request.Request(LIVE + path, headers={"User-Agent": "fukushin-live-check"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def check():
    changed = []
    for f in FILES:
        try:
            same = live(f) == baseline(f)
        except Exception as e:  # 取得できない場合も「確認できなかった」として扱う
            print(f"  確認できず  {f}（{e}）")
            changed.append(f)
            continue
        print(f"  {'変化なし' if same else '変化あり'}  {f}")
        if not same:
            changed.append(f)
    return changed


if __name__ == "__main__":
    print(f"公開中のコーポレートサイト（{LIVE}）と取り込み時点（{BASELINE}）の比較")
    diff = check()
    if diff:
        print("\n※ 公開中データ（正本）が取り込み時点から変わっています。修正版を正本から作り直してから作業してください。")
        sys.exit(1)
    print("\n公開中データは取り込み時点から変わっていません。")
