"""通知（Issue）の自己テスト：本番サーバーには接続しない。FTPS の情報は使わない。

  python -m deployer.notify_selftest

本物の Issue の API と、このリポジトリの GITHUB_TOKEN（issues: write）で、通知の流れを順に確かめる：
  1. 失敗（自動復元）→ Issue を作る　2. 同じ内容 → 送らない　3. 別の内容（承認待ち）→ 同じ Issue に追記
  4. 正常に公開 → Issue を閉じる
公開の記録（deploy-state）は読まず、テスト用の記録を使う。サイトは「テスト用」の印で区別し、本物の通知とは混ざらない。
"""
import os
import sys
import time

from . import config as C
from . import gh

SITE = "selftest"


def main():
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    C.SITES[SITE] = C.Site(id=SITE, domain="（通知のテスト）", base_url="https://example.invalid", secret_prefix="FTPS_SELFTEST",
                           root_markers=(), protected=())
    run = os.environ.get("RUN_URL", "")
    states = {}
    mark = gh.MARK.format(site=SITE)

    def open_test_issues():
        st, items = gh.api(f"/repos/{repo}/issues?state=open&per_page=100", token)
        return [i for i in (items or []) if "pull_request" not in i and mark in (i.get("body") or "")]

    # 前回のテストの残り（テスト用の印のある Issue）を片付ける（閉じた後、一覧に反映されるまで待つ）
    cleaned = 0
    for it in open_test_issues():
        gh.api(f"/repos/{repo}/issues/{it['number']}", token, "PATCH", {"state": "closed", "state_reason": "not_planned"})
        cleaned += 1

    def settle(expect_open):
        """別の実行どうしの間隔を再現する：一覧（一覧での検索）が期待の状態になるまで待つ（最大60秒）"""
        gh._KNOWN.clear()
        for _ in range(30):
            if bool(open_test_issues()) == expect_open:
                return True
            time.sleep(2)
        return False
    gh.read_state = lambda site, tok: states.get(site)
    os.environ.update(SITES='[{"site": "selftest"}]', MODE="auto", DEPLOY_RESULT="success")
    steps = [
        ("rolled-back", "a" * 40, "1. 失敗（自動復元）→ 作成"),
        ("rolled-back", "a" * 40, "2. 同じ内容 → 送らない"),
        ("awaiting-approval", "b" * 40, "3. 承認待ち → 追記"),
        ("published", "c" * 40, "4. 正常 → 閉じる"),
    ]
    results = []
    if cleaned and not settle(False):
        gh.summary("## 通知の自己テスト\n**不合格**：前回の残りを閉じた後、一覧に反映されませんでした")
        return 1
    for i, (status, sha, label) in enumerate(steps):
        os.environ["RUN_URL"] = f"{run}#selftest-{i}" if status != "rolled-back" else f"{run}#selftest-0"
        states[SITE] = {"last_run": os.environ["RUN_URL"], "last_status": status, "handled_sha": sha,
                        "last_reason": "【通知のテスト】本番には接続していません"}
        gh.notify()
        settled = settle(status != "published")
        it = gh._find_issue(repo, token, SITE)
        n_comments = None
        if it:
            st, cs = gh.api(f"/repos/{repo}/issues/{it['number']}/comments?per_page=100", token)
            n_comments = len(cs or [])
        results.append((label, it["number"] if it else None, n_comments, settled))
    ok = (results[0][1] and results[0][2] == 0 and results[1][1] == results[0][1] and results[1][2] == 0
          and results[2][1] == results[0][1] and results[2][2] == 1 and results[3][1] is None
          and all(r[3] for r in results) and not open_test_issues())
    lines = ["## 通知の自己テスト（本番に接続しない）", f"- 前回のテストの残りを閉じた件数：{cleaned}"] + [
        f"- {lab}：開いている Issue {'#' + str(n) if n else 'なし'}・追記 {c if c is not None else '-'} 件"
        f"・一覧への反映 {'確認' if s else '未確認'}" for lab, n, c, s in results]
    lines.append("**合格**" if ok else "**不合格**")
    gh.summary("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
