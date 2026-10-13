"""公開の記録（deploy-state）と対象の判定のテスト（GitHub には接続しない）"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy-repo"))
from deployer import gh  # noqa: E402


class GhTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cwd = os.getcwd()
        os.chdir(self.d)
        self.env = dict(os.environ)
        for k in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "FUKUSHIN_STATUS_TOKEN"):
            os.environ.pop(k, None)
        os.environ.update(STATE_DIR=str(self.d / "state-branch/state"), RUN_URL="u", NOW="t")

    def tearDown(self):
        os.chdir(self.cwd)
        os.environ.clear()
        os.environ.update(self.env)

    def result(self, site, decision=None, result=None):
        p = self.d / "results" / site
        p.mkdir(parents=True, exist_ok=True)
        for name, v in (("decision.json", decision), ("result.json", result)):
            f = p / name
            if v is None:
                f.unlink(missing_ok=True)
            else:
                f.write_text(json.dumps(v))

    def state(self, site):
        return json.loads((self.d / f"state-branch/state/{site}.json").read_text())

    def record(self, sha, mode="auto"):
        os.environ.update(SHA=sha, MODE=mode)
        gh.record()

    def test_publish_records_files_and_handled_commit(self):
        self.result("ebisenka", {"status": "ready"}, {"status": "published", "state": {"files": {"index.html": "h"}}})
        self.record("s1")
        st = self.state("ebisenka")
        self.assertEqual((st["handled_sha"], st["published_sha"], st["files"]), ("s1", "s1", {"index.html": "h"}))

    def test_check_mode_does_not_consume_commit(self):
        self.result("fukuki", {"status": "checked", "reason": "確認のみ"})
        self.record("s1", mode="check")
        st = self.state("fukuki")
        self.assertNotIn("handled_sha", st)
        self.assertEqual(st["last_check"]["status"], "checked")

    def test_abort_is_retried_up_to_limit(self):
        self.result("corporate", {"status": "aborted", "reason": "接続できない"})
        for i in range(gh.MAX_ABORT_RETRY):
            self.record("s1")
        self.assertEqual(self.state("corporate")["attempts"], gh.MAX_ABORT_RETRY)

    def test_restore_pauses_auto_publish(self):
        self.result("ebisenka", {"status": "ready"}, {"status": "published", "state": {"files": {"a": "b"}}})
        self.record("s1")
        self.result("ebisenka", None, {"status": "restored", "reason": "手動の復元"})
        self.record("s1", mode="restore")
        self.assertIsNone(self.state("ebisenka")["files"])

    def test_resolve_skips_handled_and_requires_ci(self):
        calls = {}
        sd = self.d / "remote"

        def fake_api(path, token=None, method="GET", body=None):
            if path.endswith("/commits/main"):
                return 200, {"sha": "s2"}
            if "/actions/workflows/" in path:
                return 200, {"workflow_runs": [{"event": "push", "status": "completed", "conclusion": calls.get("ci", "success")}]}
            if "/contents/state/" in path:
                site = path.split("/contents/state/")[1].split(".json")[0]
                f = sd / f"{site}.json"
                if not f.is_file():
                    return 404, None
                import base64
                return 200, {"content": base64.b64encode(f.read_bytes()).decode()}
            return 404, None

        self.addCleanup(setattr, gh, "api", gh.api)
        gh.api = fake_api
        sd.mkdir()
        (sd / "ebisenka.json").write_text(json.dumps({"handled_sha": "s2", "last_status": "published"}))
        (sd / "fukuki.json").write_text(json.dumps({"handled_sha": "s1", "last_status": "published"}))
        out = self.d / "out.txt"
        os.environ.update(GITHUB_OUTPUT=str(out), GITHUB_REPOSITORY="o/r", EVENT="schedule", AUTO_SITES="")
        gh.resolve()
        sites = json.loads([l for l in out.read_text().splitlines() if l.startswith("sites=")][-1][6:])
        self.assertEqual(sites, [])                                                 # 自動公開が無効なら定期の確認は接続しない
        os.environ["AUTO_SITES"] = "ebisenka,fukuki,corporate"
        gh.resolve()
        sites = json.loads([l for l in out.read_text().splitlines() if l.startswith("sites=")][-1][6:])
        self.assertEqual([s["site"] for s in sites], ["fukuki", "corporate"])     # ebisenka は処理済み
        calls["ci"] = "failure"
        gh.resolve()
        sites = json.loads([l for l in out.read_text().splitlines() if l.startswith("sites=")][-1][6:])
        self.assertEqual(sites, [])                                                 # CI 不合格なら何もしない
        os.environ.update(EVENT="workflow_dispatch", INPUT_SITE="ebisenka", INPUT_MODE="publish")
        self.assertEqual(gh.resolve(), 1)                                           # 手動でも CI 不合格は公開しない


    # ---------------------------------------------------------------- 自動の起動の種類・停止
    def _fake_remote(self, states, ci="success"):
        import base64
        sd = self.d / "remote2"
        sd.mkdir(exist_ok=True)
        for s, v in states.items():
            (sd / f"{s}.json").write_text(json.dumps(v))

        def fake_api(path, token=None, method="GET", body=None):
            if path.endswith("/commits/main"):
                return 200, {"sha": "b" * 40}
            if "/actions/workflows/" in path:
                return 200, {"workflow_runs": [{"event": "push", "status": "completed", "conclusion": ci}]}
            if "/contents/state/" in path:
                f = sd / (path.split("/contents/state/")[1].split(".json")[0] + ".json")
                return (200, {"content": base64.b64encode(f.read_bytes()).decode()}) if f.is_file() else (404, None)
            return 404, None
        self.addCleanup(setattr, gh, "api", gh.api)
        gh.api = fake_api

    def _sites(self):
        out = self.d / "out.txt"
        return [s["site"] for s in json.loads([l for l in out.read_text().splitlines() if l.startswith("sites=")][-1][6:])]

    def test_ci_dispatch_and_claude_auto_use_the_same_rules_and_skip_halted(self):
        self._fake_remote({"ebisenka": {"handled_sha": "a" * 40, "halted": {"since": "t", "reason": "復元に失敗"}},
                           "fukuki": {"handled_sha": "b" * 40, "last_status": "published"}})
        os.environ.update(GITHUB_OUTPUT=str(self.d / "out.txt"), GITHUB_REPOSITORY="o/r",
                          AUTO_SITES="ebisenka,fukuki")
        for ev, mode in (("repository_dispatch", ""), ("workflow_dispatch", "auto"), ("schedule", "")):
            os.environ.update(EVENT=ev, INPUT_MODE=mode, INPUT_SITE="all", DISPATCH_SHA="c" * 40)
            self.assertEqual(gh.resolve(), 0)
            # ebisenka：停止中／fukuki：処理済み／corporate：自動公開が無効 → どれも処理しない
            self.assertEqual(self._sites(), [], ev)
        os.environ["AUTO_SITES"] = "ebisenka,fukuki,corporate"
        os.environ.update(EVENT="repository_dispatch", DISPATCH_SHA="not-a-sha")
        gh.resolve()
        self.assertEqual(self._sites(), ["corporate"])
        self._fake_remote({}, ci="failure")
        gh.resolve()
        self.assertEqual(self._sites(), [])                                          # CI 不合格なら何もしない

    def test_restore_failure_halts_until_resumed(self):
        self.result("corporate", {"status": "ready"}, {"status": "restore-failed", "reason": "戻せない"})
        self.record("s1")
        st = self.state("corporate")
        self.assertEqual(st["halted"]["reason"], "戻せない")
        self.result("corporate", {"status": "ready"}, {"status": "published", "state": {"files": {"a": "b"}}})
        self.record("s2")
        self.assertIn("halted", self.state("corporate"))                           # 公開が通っても自動では解除しない
        os.environ.update(SITE="corporate", CONFIRM="wrong")
        with self.assertRaises(SystemExit):
            gh.resume()
        self.assertIn("halted", self.state("corporate"))
        os.environ["CONFIRM"] = "fukushin-e.co.jp"
        gh.resume()
        st = self.state("corporate")
        self.assertNotIn("halted", st)
        self.assertEqual(st["resumed"]["was"]["reason"], "戻せない")


class NotifyTests(unittest.TestCase):
    """通知（Issue）：対応が必要なときだけ・サイトごとに1件・同じ内容は送らない・機密を載せない"""

    def setUp(self):
        self.env = dict(os.environ)
        self.issues, self.states, self.calls, self.list_lag = {}, {}, [], 0
        gh._KNOWN.clear()
        self.addCleanup(gh._KNOWN.clear)
        for k in ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"):
            os.environ.pop(k, None)
        os.environ.update(GITHUB_TOKEN="t", GITHUB_REPOSITORY="o/r", RUN_URL="run1", MODE="auto",
                          SITES=json.dumps([{"site": "ebisenka"}, {"site": "corporate"}]), DEPLOY_RESULT="success")
        self.addCleanup(setattr, gh, "api", gh.api)
        self.addCleanup(setattr, gh, "read_state", gh.read_state)
        gh.read_state = lambda site, token: self.states.get(site)
        gh.api = self.fake

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def fake(self, path, token=None, method="GET", body=None):
        import re as _re
        self.calls.append((method, path))
        if method == "GET" and "/issues?state=open" in path:
            if "page=" in path and "page=1" not in path:
                return 200, []
            # 本物の GitHub と同じく、作った直後の Issue は一覧にまだ出ないことがある（lag 回の一覧の後に出る）
            out = []
            for n, i in self.issues.items():
                if i["state"] == "open" and i.get("lag", 0) <= 0:
                    out.append(dict(i, number=n))
                elif i["state"] == "open":
                    i["lag"] -= 1
            return 200, out
        m = _re.search(r"/issues/(\d+)/comments", path)
        if m:
            n = int(m.group(1))
            if method == "POST":
                self.issues[n]["comments"].append(body["body"])
                return 201, {}
            return 200, [{"body": c} for c in self.issues[n]["comments"]]
        m = _re.search(r"/issues/(\d+)$", path)
        if m and method == "PATCH":
            self.issues[int(m.group(1))]["state"] = body["state"]
            return 200, {}
        if m and method == "GET":
            n = int(m.group(1))
            return (200, dict(self.issues[n], number=n)) if n in self.issues else (404, None)
        if path.endswith("/issues") and method == "POST":
            n = len(self.issues) + 1
            self.issues[n] = {"title": body["title"], "body": body["body"], "state": "open", "comments": [],
                              "lag": self.list_lag}
            return 201, {"number": n}
        return 404, None

    def test_notifies_once_per_site_and_closes_when_resolved(self):
        os.environ["FTPS_EBISENKA_HOST"] = "ftp.secret-host.example"
        self.states = {"ebisenka": {"last_run": "run1", "last_status": "rolled-back", "handled_sha": "a" * 40,
                                    "last_reason": "公開後の検証に不合格 ftp.secret-host.example @someone <!-- x -->"},
                       "corporate": {"last_run": "run1", "last_status": "no-change", "handled_sha": "a" * 40}}
        gh.notify()
        self.assertEqual(len(self.issues), 1)
        body = self.issues[1]["body"]
        self.assertIn("自動で元に戻しました", self.issues[1]["title"])
        self.assertNotIn("ftp.secret-host.example", body)
        self.assertNotIn("@someone", body)
        self.assertNotIn("<!-- x", body)
        gh.notify()                                                                  # 同じ実行・同じ内容 → 送らない
        self.assertEqual((len(self.issues), self.issues[1]["comments"]), (1, []))
        self.states["ebisenka"].update(last_run="run2", last_status="awaiting-approval", handled_sha="b" * 40)
        os.environ["RUN_URL"] = "run2"
        gh.notify()                                                                  # 新しい内容 → 同じ Issue に追記
        self.assertEqual((len(self.issues), len(self.issues[1]["comments"])), (1, 1))
        self.assertIn("承認待ち", self.issues[1]["comments"][0])
        self.states["ebisenka"].update(last_run="run3", last_status="published", handled_sha="c" * 40)
        os.environ["RUN_URL"] = "run3"
        gh.notify()                                                                  # 解消 → 閉じる
        self.assertEqual(self.issues[1]["state"], "closed")

    def test_retried_abort_is_quiet_until_limit_and_run_failure_is_reported(self):
        self.states = {"ebisenka": {"last_run": "run1", "last_status": "aborted", "attempts": 1, "handled_sha": "a" * 40}}
        gh.notify()
        self.assertEqual(self.issues, {})
        self.states["ebisenka"]["attempts"] = gh.MAX_ABORT_RETRY
        gh.notify()
        self.assertEqual(len(self.issues), 1)
        # 記録の前に止まった実行（記録が古いまま）
        self.states = {"corporate": {"last_run": "old", "last_status": "published"}}
        os.environ.update(DEPLOY_RESULT="failure", RUN_URL="run9")
        gh.notify()
        self.assertTrue(any("記録の前に止まりました" in i["title"] for i in self.issues.values()))

    def test_halted_is_notified_once_and_check_mode_is_silent(self):
        self.states = {"ebisenka": {"last_run": "run1", "last_status": "halted", "halted": {"since": "t1", "reason": "x"}}}
        gh.notify()
        os.environ["RUN_URL"] = "run2"
        self.states["ebisenka"]["last_run"] = "run2"
        gh.notify()
        self.assertEqual((len(self.issues), self.issues[1]["comments"]), (1, []))
        os.environ["MODE"] = "check"
        self.states["ebisenka"].update(last_run="run3", last_status="rolled-back")
        gh.notify()
        self.assertEqual(self.issues[1]["comments"], [])

    def test_halted_site_keeps_its_issue_open_even_after_a_publish(self):
        self.states = {"ebisenka": {"last_run": "run1", "last_status": "restore-failed", "handled_sha": "a" * 40,
                                    "halted": {"since": "t1", "reason": "戻せない"}}}
        gh.notify()
        self.assertIn("元に戻せませんでした", self.issues[1]["title"])
        self.states["ebisenka"].update(last_run="run2", last_status="published", handled_sha="b" * 40)
        os.environ["RUN_URL"] = "run2"
        gh.notify()
        self.assertEqual(self.issues[1]["state"], "open")                         # 停止中は閉じない
        self.assertIn("停止中", self.issues[1]["comments"][0])
        self.states["ebisenka"].update(last_run="run3", last_status="restore-failed")
        os.environ["RUN_URL"] = "run3"
        gh.notify()                                                                  # 別の実行の復元失敗は、改めて知らせる
        self.assertEqual(len(self.issues[1]["comments"]), 2)

    def test_runs_failing_before_record_are_reported_once_per_commit(self):
        self.states = {"corporate": {"last_run": "old", "last_status": "published"}}
        os.environ.update(SITES=json.dumps([{"site": "corporate"}]), DEPLOY_RESULT="failure", SHA="d" * 40)
        for run in ("r1", "r2", "r3"):
            os.environ["RUN_URL"] = run
            gh.notify()
        self.assertEqual((len(self.issues), self.issues[1]["comments"]), (1, []))
        os.environ["DEPLOY_RESULT"] = "cancelled"                                   # 待ちの列で置き換わった
        os.environ.update(RUN_URL="r4", SHA="e" * 40)
        gh.notify()
        self.assertEqual(self.issues[1]["comments"], [])

    def test_same_process_finds_new_issue_even_if_list_lags(self):
        self.list_lag = 5
        self.states = {"ebisenka": {"last_run": "run1", "last_status": "rolled-back", "handled_sha": "a" * 40}}
        gh.notify()
        self.states["ebisenka"].update(last_run="run2", last_status="awaiting-approval", handled_sha="b" * 40)
        os.environ["RUN_URL"] = "run2"
        gh.notify()
        self.assertEqual((len(self.issues), len(self.issues[1]["comments"])), (1, 1))

    def test_selftest_flow_passes_against_fake_issue_api(self):
        from deployer import notify_selftest, config as C
        self.addCleanup(C.SITES.pop, "selftest", None)
        self.addCleanup(gh._KNOWN.clear)
        self.list_lag = 2
        self.issues[99] = {"title": "old", "body": gh.MARK.format(site="selftest"), "state": "open", "comments": []}
        import time as _time
        self.addCleanup(setattr, _time, "sleep", _time.sleep)
        _time.sleep = lambda s: None
        self.assertEqual(notify_selftest.main(), 0)
        self.assertEqual(self.issues[99]["state"], "closed")                    # 前回の残りを片付ける
        new = [i for n, i in self.issues.items() if n != 99]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["state"], "closed")
        self.assertIn("通知のテスト", new[0]["body"])


if __name__ == "__main__":
    unittest.main()
