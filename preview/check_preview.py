#!/usr/bin/env python3
"""プレビューの「鍵」が掛かっているかを外から確認する（読み取りのみ・パスワードは使わない）。

  python3 preview/check_preview.py https://ebisenka.jp/_preview-fk2026/

パスワード無しでアクセスして 401（認証が必要）が返れば合格。
200 が返る＝誰でも見られる状態なので、すぐに停止手順（docs/13）を実行する。
"""
import sys
import urllib.error
import urllib.request

PATHS = ["", "bento/", "bento/wholesale/", "bento/assets/img/ogp.jpg", "bento/contact/send.php",
         "corporate/", "corporate/business.html", "corporate/preview-mail.php"]


def status(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fukushin-preview-check"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return f"接続できず（{e}）"


def main():
    if len(sys.argv) != 2:
        sys.exit("使い方: python3 preview/check_preview.py https://ebisenka.jp/_preview-fk2026/")
    base = sys.argv[1].rstrip("/") + "/"
    exposed = []
    for p in PATHS:
        s = status(base + p)
        label = {401: "OK  鍵あり", 404: "OK  存在しない"}.get(s, "NG  見られる" if s == 200 else "要確認")
        print(f"  {label}  {s}  {base + p}")
        if s == 200:
            exposed.append(p)
    if exposed:
        print("\n【NG】パスワード無しで見られるページがあります。docs/13 の「認証が効かないときの即時停止」を実行してください。")
        sys.exit(1)
    print("\n【OK】パスワード無しでは見られません。")


if __name__ == "__main__":
    main()
