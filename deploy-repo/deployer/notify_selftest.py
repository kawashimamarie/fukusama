"""通知（Issue）の自己テスト：本番サーバーには接続しない。FTPS の情報は使わない。

  python -m deployer.notify_selftest

本物の Issue の API と、このリポジトリの GITHUB_TOKEN（issues: write）で、通知の流れを順に確かめる：
  1. 失敗（自動復元）→ Issue を作る　2. 同じ内容 → 送らない　3. 別の内容（承認待ち）→ 同じ Issue に追記
  4. 正常に公開 → Issue を閉じる
公開の記録（deploy-state）は読まず、テスト用の記録を使う。サイトは「テスト用」の印で区別し、本物の通知とは混ざらない。
"""
import os
import sys

from . import config as C
from . import gh

SITE = "selftest"


def main():
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    C.SITES[SITE] = C.Site(id=SITE, domain="（通知のテスト）", base_url="https://example.invalid", secret_prefix="FTPS_SELFTEST",
                           root_markers=(), protected=())
    run = os.environ.get("RUN_URL", "")
    states = {}
    gh.read_state = lambda site, tok: states.get(site)
    os.environ.update(SITES='[{"site": "selftest"}]', MODE="auto", DEPLOY_RESULT="success")
    steps = [
        ("rolled-back", "a" * 40, "1. 失敗（自動復元）→ 作成"),
        ("rolled-back", "a" * 40, "2. 同じ内容 → 送らない"),
        ("awaiting-approval", "b" * 40, "3. 承認待ち → 追記"),
        ("published", "c" * 40, "4. 正常 → 閉じる"),
    ]
    results = []
    for i, (status, sha, label) in enumerate(steps):
        os.environ["RUN_URL"] = f"{run}#selftest-{i}" if status != "rolled-back" else f"{run}#selftest-0"
        states[SITE] = {"last_run": os.environ["RUN_URL"], "last_status": status, "handled_sha": sha,
                        "last_reason": "【通知のテスト】本番には接続していません"}
        gh.notify()
        it = gh._find_issue(repo, token, SITE)
        n_comments = None
        if it:
            st, cs = gh.api(f"/repos/{repo}/issues/{it['number']}/comments?per_page=100", token)
            n_comments = len(cs or [])
        results.append((label, it["number"] if it else None, n_comments))
    ok = (results[0][1] and results[0][2] == 0 and results[1][1] == results[0][1] and results[1][2] == 0
          and results[2][1] == results[0][1] and results[2][2] == 1 and results[3][1] is None)
    lines = ["## 通知の自己テスト（本番に接続しない）"] + [
        f"- {lab}：開いている Issue {'#' + str(n) if n else 'なし'}・追記 {c if c is not None else '-'} 件" for lab, n, c in results]
    lines.append("**合格**" if ok else "**不合格**")
    gh.summary("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
