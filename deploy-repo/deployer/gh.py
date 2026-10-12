"""GitHub まわり（本番の FTPS 情報は使わない）。

  python -m deployer.gh resolve   公開するコミット（fukusama の main の最新）と、処理するサイトを決める
  python -m deployer.gh record    結果を deploy-state ブランチの state/<サイト>.json に記録し、
                                  （任意）fukusama のコミットに結果の状態（fukushin-deploy/<サイト>）を付ける
"""
import base64
import json
import os
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
    mode = "auto" if event == "schedule" else in_mode
    lines = [f"## 公開の対象（fukusama main：`{sha[:12]}`・CI：{ci}・モード：{mode}）"]
    if event == "schedule":
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


STATUS_STATE = {"published": "success", "published-with-warnings": "success", "no-change": "success",
                "checked": "success", "awaiting-approval": "pending", "rejected": "error", "aborted": "error",
                "rolled-back": "failure", "restore-failed": "failure"}


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
    sys.exit({"resolve": resolve, "record": record, "getstate": getstate}[sys.argv[1]]())
