"""GitHub まわり（本番の FTPS 情報は使わない）。

  python -m deployer.gh resolve   公開するコミット（fukusama の main の最新）と、処理するサイトを決める
  python -m deployer.gh record    結果を deploy-state ブランチの state/<サイト>.json に記録し、
                                  （任意）fukusama のコミットに結果の状態（fukushin-deploy/<サイト>）を付ける
  python -m deployer.gh notify    この実行の結果のうち、対応が必要なもの（公開失敗・自動復元・復元失敗・承認待ち・
                                  キャッシュ不一致・停止中）を、サイトごとに1件の Issue で知らせる（重複しない）
  python -m deployer.gh resume    復元の失敗で止めた自動公開を、ご本人の明示的な操作で再開する（記録の更新だけ）
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import config as C

SOURCE_REPO = "kawashimamarie/fukusama"
SOURCE_BRANCH = "main"
CI_WORKFLOW = "site-ci.yml"
STATE_BRANCH = "deploy-state"
MAX_ABORT_RETRY = 3   # 一時的な不調（接続できない等）で止まったときに、同じコミットで自動でやり直す回数


def api(path, token=None, method="GET", body=None):
    req = urllib.request.Request("https://api.github.com" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                                          "User-Agent": "fukushin-deploy"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def out(**kv):
    f = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{k}={v}" for k, v in kv.items()]
    print("\n".join(lines))
    if f:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def summary(text):
    print(text)
    f = os.environ.get("GITHUB_STEP_SUMMARY")
    if f:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def main_head(token):
    st, j = api(f"/repos/{SOURCE_REPO}/commits/{SOURCE_BRANCH}", token)
    if st != 200:
        raise SystemExit(f"fukusama の main を読めません（HTTP {st}）")
    return j["sha"]


def ci_state(sha, token):
    st, j = api(f"/repos/{SOURCE_REPO}/actions/workflows/{CI_WORKFLOW}/runs?head_sha={sha}&per_page=5", token)
    if st != 200 or not j or not j.get("workflow_runs"):
        return "none"
    runs = [r for r in j["workflow_runs"] if r.get("event") == "push"] or j["workflow_runs"]
    r = runs[0]
    if r["status"] != "completed":
        return "running"
    return "success" if r["conclusion"] == "success" else "failure"


def read_state(site, token):
    repo = os.environ["GITHUB_REPOSITORY"]
    st, j = api(f"/repos/{repo}/contents/state/{site}.json?ref={STATE_BRANCH}", token)
    if st != 200 or not j:
        return None
    return json.loads(base64.b64decode(j["content"]))


def resolve():
    token = os.environ.get("GITHUB_TOKEN")
    event = os.environ.get("EVENT", "schedule")
    in_site = os.environ.get("INPUT_SITE", "all")
    in_mode = os.environ.get("INPUT_MODE", "check")
    sha = main_head(token)
    ci = ci_state(sha, token)
    sd = Path("state")
    sd.mkdir(exist_ok=True)
    states = {}
    for s in C.SITES:
        states[s] = read_state(s, token)
        if states[s]:
            (sd / f"{s}.json").write_text(json.dumps(states[s], ensure_ascii=False))
    # 自動の判定で動くもの：定期実行・fukusama の CI 合格の知らせ（repository_dispatch）・手動の mode=auto（Claude からの起動）。
    # どれも同じ判定（main の最新・CI 合格・AUTO_PUBLISH_SITES のサイトだけ・処理済みのコミットは飛ばす・停止中は飛ばす）
    auto_event = event in ("schedule", "repository_dispatch") or (event == "workflow_dispatch" and in_mode == "auto")
    mode = "auto" if auto_event else in_mode
    lines = [f"## 公開の対象（fukusama main：`{sha[:12]}`・CI：{ci}・モード：{mode}・起動：{event}）"]
    if event == "repository_dispatch":
        sent = os.environ.get("DISPATCH_SHA", "")
        if not re.fullmatch(r"[0-9a-f]{40}", sent):
            lines.append("- 知らせのコミット番号が読めません（main の最新で判定します）")
        elif sent != sha:
            lines.append(f"- 知らせのコミット `{sent[:12]}` より main が進んでいます（main の最新で判定します）")
    if auto_event:
        if ci != "success":
            sites = []
            lines.append(f"- CI（{CI_WORKFLOW}）が成功していないため、今回は何もしません（{ci}）")
        else:
            sites = []
            auto = {x.strip() for x in os.environ.get("AUTO_SITES", "").split(",") if x.strip()}
            if not auto:
                lines.append("- 自動公開が有効なサイトがないため、定期の確認では接続しません（変数 AUTO_PUBLISH_SITES が空）")
            for s in C.SITES:
                if s not in auto:
                    continue
                stt = states[s] or {}
                if stt.get("halted"):
                    lines.append(f"- {C.SITES[s].domain}：**自動公開を停止中**（{stt['halted'].get('reason', '')[:120]}）。"
                                 "原因を解消し、「自動公開の再開」で解除するまで処理しません")
                    continue
                if stt.get("handled_sha") == sha and not (stt.get("last_status") == "aborted"
                                                          and stt.get("attempts", 0) < MAX_ABORT_RETRY):
                    lines.append(f"- {C.SITES[s].domain}：このコミットは処理済み（{stt.get('last_status')}）")
                    continue
                sites.append(s)
    else:
        sites = list(C.SITES) if in_site == "all" else [in_site]
        if mode in ("publish", "auto") and ci != "success":
            lines.append(f"- **CI が成功していないコミットは公開できません**（{ci}）")
            summary("\n".join(lines))
            out(sha=sha, mode=mode, sites="[]", ci=ci)
            return 1
    matrix = [{"site": s, "domain": C.SITES[s].domain, "prefix": C.SITES[s].secret_prefix} for s in sites]
    for m in matrix:
        lines.append(f"- {m['domain']}：処理します")
    summary("\n".join(lines))
    out(sha=sha, mode=mode, ci=ci, sites=json.dumps(matrix, ensure_ascii=False))
    return 0


STATUS_STATE = {"published": "success", "no-change": "success", "checked": "success",
                "published-cache-stale": "error", "awaiting-approval": "pending", "superseded": "pending",
                "halted": "error", "rejected": "error", "aborted": "error",
                "rolled-back": "failure", "restore-failed": "failure", "restored": "success"}


def record():
    """results/<site>/{decision.json,result.json} と state/ から、新しい state/<site>.json を作る"""
    sha = os.environ["SHA"]
    run_url = os.environ.get("RUN_URL", "")
    now = os.environ.get("NOW", "")
    results, state_dir = Path("results"), Path(os.environ.get("STATE_DIR", "state-branch/state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    status_token = os.environ.get("FUKUSHIN_STATUS_TOKEN")
    lines = ["## 結果"]
    changed = False
    for site_dir in sorted(p for p in results.glob("*") if p.is_dir()):
        site = site_dir.name
        if site not in C.SITES:
            continue
        dec = json.loads((site_dir / "decision.json").read_text()) if (site_dir / "decision.json").is_file() else {}
        res = json.loads((site_dir / "result.json").read_text()) if (site_dir / "result.json").is_file() else {}
        status = res.get("status") or dec.get("status") or "aborted"
        f = state_dir / f"{site}.json"
        st = json.loads(f.read_text()) if f.is_file() else {"site": site, "domain": C.SITES[site].domain}
        attempts = st.get("attempts", 0) + 1 if st.get("handled_sha") == sha and st.get("last_status") == "aborted" else 1
        reason = res.get("reason") or dec.get("reason", "")
        if os.environ.get("MODE") == "check":
            # 確認のみ：公開の記録（処理済みのコミット）は変えない
            st.update(last_check={"sha": sha, "status": status, "run": run_url, "at": now, "reason": reason})
        else:
            st.update(handled_sha=sha, last_status=status, last_run=run_url, updated_at=now, attempts=attempts,
                      last_reason=reason)
        if res.get("state"):
            st.update(published_sha=sha, files=res["state"]["files"], published_at=now)
        if status == "restored":
            st["files"] = None
        if status == "restore-failed" and not st.get("halted"):
            # 自動の復元・手動の復元に失敗したサイトは、自動公開を止める（ご本人が「自動公開の再開」で解除するまで）
            st["halted"] = {"since": now, "reason": reason[:300], "run": run_url, "sha": sha}
        f.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n")
        changed = True
        lines.append(f"- {C.SITES[site].domain}：**{status}**（{reason}）")
        if status_token:
            api(f"/repos/{SOURCE_REPO}/statuses/{sha}", status_token, "POST", {
                "state": STATUS_STATE.get(status, "error"), "context": f"fukushin-deploy/{site}",
                "description": f"{status}：{reason}"[:139], "target_url": run_url})
    summary("\n".join(lines))
    out(changed="true" if changed else "false")
    return 0


# ---------------------------------------------------------------- 通知（Issue）
NOTIFY_LABEL = {
    "rolled-back": "公開に失敗し、自動で元に戻しました",
    "restore-failed": "【至急】元に戻せませんでした（自動公開を停止しました）",
    "awaiting-approval": "承認待ち（自動では公開しない変更があります）",
    "published-cache-stale": "公開しましたが、公開URLの表示が一致しません（キャッシュ）",
    "aborted": "公開できませんでした（書き込みなし）",
    "halted": "自動公開を停止中です",
    "run-failed": "公開の実行が記録の前に止まりました",
}
NOTIFY_TODO = {
    "rolled-back": "サイトは公開前の状態です。実行の要約で原因を確かめ、修正してから main に入れ直してください。",
    "restore-failed": "サイトが途中の状態の可能性があります。実行の要約の「戻せなかったもの」を確かめ、docs/17 の手動での復旧を行ってください。"
                      "原因を解消したら、Actions の「自動公開の再開」（確認入力にドメイン名）で解除します。",
    "awaiting-approval": "内容を確かめ、公開してよければ Actions の「公開（福進発展3サイト）」を mode=publish・confirm にドメイン名で実行してください。",
    "published-cache-stale": "サーバーのファイルは正しく置き換わっています。お名前.com のコントロールパネルでキャッシュを削除し、公開URLを確かめてください。",
    "aborted": "書き込みはしていません。一時的な不調なら次の実行で再試行されます（同じコミットで最大3回）。",
    "halted": "原因を解消してから、Actions の「自動公開の再開」で解除してください。解除するまで自動公開しません。",
    "run-failed": "実行のログを確かめてください。結果の記録がないため、サーバーの状態は「確認のみ」で確かめてください。",
}
RESOLVED = {"published", "no-change"}
MARK = "<!-- fukushin-deploy-notify site={site} -->"
KEY = "<!-- fukushin-deploy-key {key} -->"


def _clean(text, limit=600):
    """通知に出す文：接続情報・トークンを伏せ、HTML のコメントや @ の呼びかけを無効にし、長さを抑える"""
    text = C.redact(str(text or ""))
    text = text.replace("<!--", "‹!--").replace("-->", "--›").replace("@", "＠").replace("\r", " ")
    return text[:limit]


def notify_kind(st, run_url, deploy_result, mode):
    """この実行で、このサイトに通知が要るか：(種類, 重複を防ぐ鍵) / 解消なら ("resolved", None) / 何もなし None"""
    if st.get("last_run") != run_url:
        # 記録の前に止まった実行：同じコミットでは1回だけ（毎時・CI の知らせのたびに増やさない）
        return ("run-failed", f"run-failed:{os.environ.get('SHA') or run_url}") if deploy_result == "failure" else None
    status = st.get("last_status")
    sha = st.get("handled_sha", "")
    if st.get("halted"):
        # 停止中は、公開が通っても通知を閉じない（解除は「自動公開の再開」だけ）
        if status == "restore-failed":
            return ("restore-failed", f"restore-failed:{run_url}")
        return ("halted", "halted:" + st["halted"].get("since", ""))
    if status in RESOLVED:
        return ("resolved", None)
    if status == "aborted":
        if mode == "auto" and st.get("attempts", 0) < MAX_ABORT_RETRY:
            return None                     # 自動の再試行の途中は知らせない
        return ("aborted", f"aborted:{sha}")
    if status in NOTIFY_LABEL:
        return (status, f"{status}:{sha}")
    return None


_KNOWN = {}   # この実行の中で作った・見つけた Issue の番号（一覧への反映が数秒遅れることがあるため、番号で直接読む）


def _find_issue(repo, token, site):
    mark = MARK.format(site=site)
    n = _KNOWN.get(site)
    if n:
        st, it = api(f"/repos/{repo}/issues/{n}", token)
        if st == 200 and it and it.get("state") == "open" and mark in (it.get("body") or ""):
            return it
        _KNOWN.pop(site, None)
    for page in range(1, 6):
        st, items = api(f"/repos/{repo}/issues?state=open&per_page=100&page={page}", token)
        if st != 200 or not items:
            return None
        for it in items:
            if "pull_request" not in it and mark in (it.get("body") or ""):
                # 一覧は数秒遅れることがあるため、番号で読み直して「開いている」ことを確かめる
                s2, cur = api(f"/repos/{repo}/issues/{it['number']}", token)
                if s2 == 200 and cur and cur.get("state") == "open":
                    _KNOWN[site] = it["number"]
                    return cur
    return None


def _issue_keys(repo, token, issue):
    keys = set(re.findall(r"<!-- fukushin-deploy-key (\S+) -->", issue.get("body") or ""))
    st, comments = api(f"/repos/{repo}/issues/{issue['number']}/comments?per_page=100", token)
    for c in (comments or []) if st == 200 else []:
        keys.update(re.findall(r"<!-- fukushin-deploy-key (\S+) -->", c.get("body") or ""))
    return keys


def notify():
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    run_url = os.environ.get("RUN_URL", "")
    mode = os.environ.get("MODE", "")
    deploy_result = os.environ.get("DEPLOY_RESULT", "")
    sites = [m["site"] for m in json.loads(os.environ.get("SITES") or "[]")]
    lines = ["## 通知"]
    if mode == "check":
        lines.append("- 確認のみの実行のため、通知しません")
        summary("\n".join(lines))
        return 0
    for site in sites:
        if site not in C.SITES:
            continue
        dom = C.SITES[site].domain
        st = read_state(site, token) or {}
        k = notify_kind(st, run_url, deploy_result, mode)
        issue = _find_issue(repo, token, site)
        if k is None:
            lines.append(f"- {dom}：通知なし")
            continue
        kind, key = k
        if kind == "resolved":
            if issue:
                api(f"/repos/{repo}/issues/{issue['number']}/comments", token, "POST",
                    {"body": f"{dom}：{st.get('last_status')} になりました（{run_url}）。この通知を閉じます。\n\n---\n_fukushin-deploy_"})
                api(f"/repos/{repo}/issues/{issue['number']}", token, "PATCH", {"state": "closed", "state_reason": "completed"})
                _KNOWN.pop(site, None)
                lines.append(f"- {dom}：解消したため Issue #{issue['number']} を閉じました")
            else:
                lines.append(f"- {dom}：通知なし（正常）")
            continue
        sha = st.get("handled_sha", "") or ""
        reason = (st.get("halted") or {}).get("reason") if kind == "halted" else st.get("last_reason")
        body = "\n".join([
            f"**{NOTIFY_LABEL[kind]}**", "",
            f"- サイト：{dom}",
            f"- コミット：`{sha[:12] or '-'}`（fukusama main）",
            f"- 実行：{run_url}",
            f"- 理由：{_clean(reason) or '（実行の要約を確かめてください）'}",
            f"- 次にすること：{NOTIFY_TODO[kind]}", "",
            "ファイルの中身・接続情報は載せていません。詳しくは実行の要約を確かめてください。", "",
            KEY.format(key=key)])
        if issue:
            if key in _issue_keys(repo, token, issue):
                lines.append(f"- {dom}：通知済み（Issue #{issue['number']}・重複のため送りません）")
                continue
            s, _ = api(f"/repos/{repo}/issues/{issue['number']}/comments", token, "POST", {"body": body})
            lines.append(f"- {dom}：Issue #{issue['number']} に追記（{kind}・HTTP {s}）")
        else:
            s, j = api(f"/repos/{repo}/issues", token, "POST", {
                "title": f"[公開] {dom}：{NOTIFY_LABEL[kind]}",
                "body": MARK.format(site=site) + "\n" + body})
            if j and j.get("number"):
                _KNOWN[site] = j["number"]
            lines.append(f"- {dom}：Issue を作成（{kind}・HTTP {s}{'・#' + str(j['number']) if j else ''}）")
    summary("\n".join(lines))
    return 0


def resume():
    """state/<site>.json の停止を解除する（STATE_DIR の中を書き換える。push はワークフローが行う）"""
    site, confirm = os.environ.get("SITE", ""), os.environ.get("CONFIRM", "")
    if site not in C.SITES:
        raise SystemExit("サイトの指定が正しくありません")
    if confirm != C.SITES[site].domain:
        raise SystemExit(f"確認入力がドメイン名（{C.SITES[site].domain}）と一致しません（何もしません）")
    f = Path(os.environ.get("STATE_DIR", "state-branch/state")) / f"{site}.json"
    st = json.loads(f.read_text()) if f.is_file() else {}
    if not st.get("halted"):
        summary(f"## {C.SITES[site].domain}：自動公開は停止していません（何もしません）")
        out(changed="false")
        return 0
    st["resumed"] = {"at": os.environ.get("NOW", ""), "run": os.environ.get("RUN_URL", ""), "was": st.pop("halted")}
    f.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n")
    summary(f"## {C.SITES[site].domain}：自動公開の停止を解除しました\n- 次の変更から、通常の自動公開の判定に戻ります"
            "（前回の公開の後にサーバーが変わっていれば、承認待ちになります）")
    out(changed="true")
    return 0


def getstate():
    """state/<site>.json を deploy-state ブランチから取り出す（なければ何も作らない）"""
    site = sys.argv[2]
    st = read_state(site, os.environ.get("GITHUB_TOKEN"))
    Path("state").mkdir(exist_ok=True)
    if st:
        Path(f"state/{site}.json").write_text(json.dumps(st, ensure_ascii=False))
    print(f"{site}：公開の記録 {'あり（前回の公開 ' + str(st.get('published_sha', '-'))[:12] + '）' if st else 'なし'}")
    return 0


if __name__ == "__main__":
    sys.exit({"resolve": resolve, "record": record, "getstate": getstate, "notify": notify,
              "resume": resume}[sys.argv[1]]())
