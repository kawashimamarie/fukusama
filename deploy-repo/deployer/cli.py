"""ワークフローから呼ぶ入口。

  python -m deployer prepare --site S --dist D --work W --mode check|auto|publish [--confirm ドメイン]
        読み取りだけ：接続先の確認・差分・事前バックアップ（W/backup/）・公開するかの判定（W/decision.json）
  python -m deployer apply   --site S --dist D --work W --backup V --sha SHA
        V（保存先から取り出し直したバックアップ）を照合 → 公開 → 公開後の検証 → 不合格なら自動で元に戻す
  python -m deployer restore --site S --backup V --confirm ドメイン
        指定した公開の事前バックアップで元に戻す（手動）

終了コード：0 成功・止める必要なし／1 公開せずに止めた・元に戻した／2 【至急】元に戻せなかった
接続情報は環境変数 FTPS_<サイト>_HOST・_USER・_PASSWORD からだけ読む（引数・ファイルでは受け付けない）。
"""
import argparse
import datetime
import json
import os
import secrets
import sys
from pathlib import Path

from . import config as C
from . import core
from .ftps import Ftps, FtpsError

JST = datetime.timezone(datetime.timedelta(hours=9))


def now():
    return datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def summary(lines):
    text = "\n".join(lines) + "\n"
    print(text)
    f = os.environ.get("GITHUB_STEP_SUMMARY")
    if f:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def output(**kv):
    f = os.environ.get("GITHUB_OUTPUT")
    if f:
        with open(f, "a", encoding="utf-8") as fh:
            for k, v in kv.items():
                fh.write(f"{k}={v}\n")


def _test_overrides(a):
    """ローカルの模擬テスト専用（GitHub Actions の上では使えない）"""
    if os.environ.get("DEPLOYER_TEST_MODE") == "1" and os.environ.get("GITHUB_ACTIONS") != "true":
        return {"cafile": a.test_cafile, "port": a.test_port or 21}, a.test_base_url
    if a.test_cafile or a.test_port or a.test_base_url:
        raise SystemExit("テスト用の引数は本番では使えません")
    return {}, None


def connect(site, readonly, extra):
    p = site.secret_prefix
    return Ftps(os.environ.get(p + "_HOST", ""), os.environ.get(p + "_USER", ""), os.environ.get(p + "_PASSWORD", ""),
                readonly=readonly, **extra)


def table_plan(pl):
    c = pl["counts"]
    lines = [f"- 追加 {c['add']} 件・置き換え {c['replace']} 件・変更なし {c['unchanged']} 件（{c['bytes']:,} バイト）",
             f"- 新しいフォルダ：{len(pl['new_dirs'])} 個" + (f"（{'、'.join(pl['new_dirs'][:8])}）" if pl["new_dirs"] else ""),
             f"- 接続先の確認：目印 {pl['destination']['markers']}／公開URLとの一致 {pl['destination']['identity'] or '（空のフォルダ。公開時に確認用ファイルで確かめる）'}",
             "- 上位・他サイトへのアクセス：" + "・".join(f"`{p}` {r}" for p, r in pl["destination"].get("escape", [])),
             "- 接続情報の形（値は表示しない）：" + ("／".join(pl["destination"].get("login_hints", [])) or "-")]
    if pl["htaccess"]:
        lines.append("- 福記美食の切り替え：.htaccess の先頭に転送設定を追加（元の .htaccess はバックアップ）")
    if pl["orphans"]:
        lines.append(f"- サーバーにあってビルドにないファイル（削除しない・報告のみ）：{len(pl['orphans'])} 件")
    if pl["form_changed"]:
        lines.append(f"- フォームに関わる変更：{'、'.join(pl['form_changed'])} → 公開後に実送信テストが必要")
    if pl["items"]:
        lines.append("")
        lines.append("| 操作 | ファイル |")
        lines.append("|---|---|")
        for i in pl["items"][:60]:
            lines.append(f"| {'追加' if i['op'] == 'add' else '置き換え'} | `{i['path']}` |")
        if len(pl["items"]) > 60:
            lines.append(f"| … | ほか {len(pl['items']) - 60} 件 |")
    return lines


def cmd_prepare(a):
    site = C.SITES[a.site]
    extra, base_override = _test_overrides(a)
    base_url = base_override or site.base_url
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    head = [f"## {site.domain}：確認・事前バックアップ（{now()}）", f"- コミット：`{a.sha or '-'}`・モード：{a.mode}"]
    if a.mode == "publish" and a.confirm != site.domain:
        decision = {"decision": "skip", "status": "rejected", "reason": f"確認入力がドメイン名（{site.domain}）と一致しません"}
        (work / "decision.json").write_text(json.dumps(decision, ensure_ascii=False))
        summary(head + [f"**止めました**：{decision['reason']}（サーバーには接続していません）"])
        output(decision="skip", status="rejected")
        return 1
    state = json.loads(Path(a.state).read_text()) if a.state and Path(a.state).is_file() else None
    token = a.token or secrets.token_hex(6)
    try:
        build = core.load_build(site, a.dist)
        ftp = connect(site, True, extra)
        try:
            escapes = [x for x in os.environ.get(site.secret_prefix + "_PARENT_ABS", "").split(";") if x.strip()]
            pl = core.plan(ftp, site, a.dist, build, state, work / "backup", token, base_url, escapes)
        finally:
            ftp.close()
    except (core.Stop, FtpsError) as e:
        decision = {"decision": "skip", "status": "aborted", "reason": str(e)}
        (work / "decision.json").write_text(json.dumps(decision, ensure_ascii=False))
        summary(head + [f"**止めました（書き込みなし）**：{e}"])
        output(decision="skip", status="aborted")
        return 1

    auto_sites = {s.strip() for s in (a.auto_sites or "").split(",") if s.strip()}
    if not pl["has_changes"]:
        decision = {"decision": "skip", "status": "no-change", "reason": "サーバーはビルドと同じです"}
    elif a.mode == "check":
        decision = {"decision": "skip", "status": "checked", "reason": "確認のみ"}
    elif a.mode == "auto":
        if site.id not in auto_sites:
            decision = {"decision": "skip", "status": "awaiting-approval",
                        "reason": "このサイトの自動公開は有効になっていません（変数 AUTO_PUBLISH_SITES）"}
        elif pl["approval_reasons"]:
            decision = {"decision": "skip", "status": "awaiting-approval", "reason": "承認が必要な変更があります"}
        else:
            decision = {"decision": "apply", "status": "ready", "reason": "自動公開"}
    else:
        decision = {"decision": "apply", "status": "ready", "reason": "ご本人の承認（手動実行・確認入力あり）"}
    decision["approval_reasons"] = pl["approval_reasons"]
    (work / "decision.json").write_text(json.dumps(decision, ensure_ascii=False))
    lines = head + table_plan(pl) + [""]
    if pl["approval_reasons"]:
        lines.append("**承認が必要な理由**：" + "／".join(pl["approval_reasons"]))
    lines.append(f"**判定**：{decision['status']}（{decision['reason']}）")
    summary(lines)
    output(decision=decision["decision"], status=decision["status"])
    return 0


def _result(work, data):
    (Path(work) / "result.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))


def cmd_apply(a):
    site = C.SITES[a.site]
    extra, base_override = _test_overrides(a)
    base_url = base_override or site.base_url
    work, vb = Path(a.work), Path(a.backup)
    pl = json.loads((vb / "plan.json").read_text())
    mine = json.loads((work / "backup" / "plan.json").read_text())
    res = {"site": site.id, "domain": site.domain, "sha": a.sha, "started": now(), "log": []}
    log = res["log"].append
    head = [f"## {site.domain}：公開（{now()}）", f"- コミット：`{a.sha}`"]
    try:
        if pl != mine:
            raise core.Stop("保存先から取り出したバックアップの計画が、確認時の計画と一致しません")
        core.verify_backup(pl, vb, site)
        build = core.load_build(site, a.dist)
        if build != pl["build_files"]:
            raise core.Stop("ビルドが確認時と一致しません")
    except core.Stop as e:
        res.update(status="aborted", reason=str(e))
        _result(work, res)
        summary(head + [f"**止めました（書き込みなし）**：{e}"])
        output(status="aborted")
        return 1
    try:
        ftp = connect(site, False, extra)
    except FtpsError as e:
        res.update(status="aborted", reason=str(e))
        _result(work, res)
        summary(head + [f"**止めました（書き込みなし）**：{e}"])
        output(status="aborted")
        return 1
    try:
        try:
            done = core.apply(ftp, site, pl, a.dist, vb, base_url, log)
        except core.Stop as e:  # 書き込む前の再確認・確認用ファイルで止まった
            res.update(status="aborted", reason=str(e))
            _result(work, res)
            summary(head + [f"**止めました（書き込みなし）**：{e}"])
            output(status="aborted")
            return 1
        except core.WriteFailed as e:
            return _rollback(ftp, site, pl, e.done, vb, base_url, res, work, head, f"公開の途中で失敗：{e}", log)
        errors, warnings = core.verify_live(site, pl, a.dist, base_url, pl["token"], cache_wait=a.cache_wait)
        if errors:
            return _rollback(ftp, site, pl, done, vb, base_url, res, work, head, "公開後の検証に不合格：" + "／".join(errors[:10]), log)
        res.update(status="published-with-warnings" if warnings else "published", warnings=warnings, finished=now(),
                   state={"site": site.id, "domain": site.domain, "published_sha": a.sha, "updated_at": now(),
                          "files": {k: v["sha256"] for k, v in pl["build_files"].items()}})
        _result(work, res)
        lines = head + [f"**公開しました**（{len(done)} 件の操作・公開後の検証に合格）"] + [f"- {x}" for x in res["log"]]
        if warnings:
            lines += ["", "**注意**："] + [f"- {w}" for w in warnings]
        if pl["form_changed"]:
            lines.append(f"- フォームに関わる変更があります（{'、'.join(pl['form_changed'])}）：実送信テストを行ってください（実際の受信は未確認）")
        summary(lines)
        output(status=res["status"])
        return 0
    finally:
        ftp.close()


def _rollback(ftp, site, pl, done, vb, base_url, res, work, head, why, log):
    problems = core.rollback(ftp, pl, done, vb, base_url, log)
    if problems:
        res.update(status="restore-failed", reason=why, problems=problems, finished=now())
        _result(work, res)
        summary(head + ["# 【至急】公開に失敗し、元に戻すこともできませんでした", f"- 原因：{why}"]
                + [f"- 戻せなかったもの：{p}" for p in problems]
                + ["- これ以上は自動で書き込みません。docs/17 の「手動での復旧」を行ってください"])
        output(status="restore-failed")
        return 2
    res.update(status="rolled-back", reason=why, finished=now())
    _result(work, res)
    summary(head + [f"**公開を取りやめ、公開前の状態に戻しました**", f"- 原因：{why}"] + [f"- {x}" for x in res["log"]])
    output(status="rolled-back")
    return 1


def cmd_restore(a):
    site = C.SITES[a.site]
    extra, base_override = _test_overrides(a)
    base_url = base_override or site.base_url
    vb = Path(a.backup)
    head = [f"## {site.domain}：復元（{now()}）"]
    if a.confirm != site.domain:
        summary(head + [f"**止めました**：確認入力がドメイン名（{site.domain}）と一致しません"])
        return 1
    pl = json.loads((vb / "plan.json").read_text())
    if pl["site"] != site.id:
        summary(head + ["**止めました**：別のサイトのバックアップです"])
        return 1
    log_lines = []
    try:
        core.verify_backup(pl, vb, site)
        ftp = connect(site, False, extra)
    except (core.Stop, FtpsError) as e:
        summary(head + [f"**止めました（書き込みなし）**：{e}"])
        return 1
    try:
        try:
            core.check_restorable(ftp, pl)
        except core.Stop as e:
            summary(head + [f"**止めました（書き込みなし）**：{e}"])
            return 1
        problems = core.rollback(ftp, pl, core.done_from_plan(pl), vb, base_url, log_lines.append)
    finally:
        ftp.close()
    if problems:
        summary(head + ["# 【至急】復元できなかったものがあります"] + [f"- {p}" for p in problems])
        return 2
    summary(head + ["**復元しました**（公開前の状態・公開URLで確認済み）"] + [f"- {x}" for x in log_lines])
    return 0


def cmd_verify_backup(a):
    """保存先から取り出したバックアップが、確認時の計画・中身と一致するか（本番に接続しない）"""
    site = C.SITES[a.site]
    vb, work = Path(a.backup), Path(a.work)
    head = [f"## {site.domain}：バックアップの保存と取り出しの確認（{now()}）"]
    try:
        pl = json.loads((vb / "plan.json").read_text())
        mine = json.loads((work / "backup" / "plan.json").read_text())
        if pl != mine:
            raise core.Stop("取り出した計画が、確認時の計画と一致しません")
        core.verify_backup(pl, vb, site)
    except (core.Stop, OSError, ValueError) as e:
        summary(head + [f"**不合格**：{e}"])
        return 1
    n = len(pl["backup"]["files"]) + (1 if pl["backup"]["htaccess"] else 0)
    size = sum(f.stat().st_size for f in (vb / "files").rglob("*") if f.is_file())
    if pl["backup"]["htaccess"]:
        size += (vb / "htaccess.orig").stat().st_size
    summary(head + [f"**合格**：公開前の中身 {n} ファイル（{size:,} バイト）と計画を保存し、取り出して SHA-256 で照合しました"
                    + ("（置き換えるファイルがないため、保存したのは計画だけ）" if n == 0 else "")])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="deployer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "apply", "restore", "verify-backup"):
        p = sub.add_parser(name)
        p.add_argument("--site", required=True, choices=sorted(C.SITES))
        p.add_argument("--test-cafile")
        p.add_argument("--test-port", type=int)
        p.add_argument("--test-base-url")
        if name in ("prepare", "apply"):
            p.add_argument("--dist", required=True)
            p.add_argument("--work", required=True)
            p.add_argument("--sha", default="")
        if name == "prepare":
            p.add_argument("--mode", required=True, choices=["check", "auto", "publish"])
            p.add_argument("--confirm", default="")
            p.add_argument("--auto-sites", default="")
            p.add_argument("--state")
            p.add_argument("--token")
        if name == "apply":
            p.add_argument("--backup", required=True)
            p.add_argument("--cache-wait", type=int, default=15)
        if name == "verify-backup":
            p.add_argument("--backup", required=True)
            p.add_argument("--work", required=True)
        if name == "restore":
            p.add_argument("--backup", required=True)
            p.add_argument("--confirm", default="")
    a = ap.parse_args(argv)
    return {"prepare": cmd_prepare, "apply": cmd_apply, "restore": cmd_restore,
            "verify-backup": cmd_verify_backup}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
