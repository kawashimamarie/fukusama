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
        os.environ.update(GITHUB_OUTPUT=str(out), GITHUB_REPOSITORY="o/r", EVENT="schedule")
        gh.resolve()
        sites = json.loads([l for l in out.read_text().splitlines() if l.startswith("sites=")][-1][6:])
        self.assertEqual([s["site"] for s in sites], ["fukuki", "corporate"])     # ebisenka は処理済み
        calls["ci"] = "failure"
        gh.resolve()
        sites = json.loads([l for l in out.read_text().splitlines() if l.startswith("sites=")][-1][6:])
        self.assertEqual(sites, [])                                                 # CI 不合格なら何もしない
        os.environ.update(EVENT="workflow_dispatch", INPUT_SITE="ebisenka", INPUT_MODE="publish")
        self.assertEqual(gh.resolve(), 1)                                           # 手動でも CI 不合格は公開しない


if __name__ == "__main__":
    unittest.main()
