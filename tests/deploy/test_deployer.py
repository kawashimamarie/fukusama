"""公開の仕組みの模擬テスト（ローカルの FTPS・HTTP サーバー。本番には接続しない）

  python3 -m unittest discover -s tests/deploy -v
必要：pyftpdlib・pyOpenSSL（tests/deploy/requirements.txt）・openssl
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy-repo"))
sys.path.insert(0, str(Path(__file__).parent))

import harness as H  # noqa: E402
from deployer import cli, core, config as C  # noqa: E402
from deployer.ftps import Ftps, FtpsError, check_path  # noqa: E402

BUILD = None
CA = None


def setUpModule():
    global BUILD, CA
    BUILD = H.tmpdir() / "out"
    subprocess.run([sys.executable, str(ROOT / "scripts/build_sites.py"), str(BUILD)], check=True, capture_output=True)
    CA = H.make_ca(H.tmpdir())


@contextmanager
def env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    os.environ.update({k: v for k, v in kv.items() if v is not None})
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Rig:
    """1サイト分：サーバーのフォルダ・FTPS・HTTP"""

    def __init__(self, site_id, *, wordpress=False, cert="srv"):
        self.site = C.SITES[site_id]
        self.dir = H.tmpdir()
        self.root = self.dir / "server"
        self.root.mkdir()
        self.ftp = H.FtpsServer(self.root, CA, cert=cert)
        self.web = H.WebServer(self.root, wordpress=wordpress)
        self.dist = self.dir / "dist"
        shutil.copytree(BUILD / self.site.domain, self.dist)
        self.state = None
        self.n = 0

    def close(self):
        self.ftp.stop()
        self.web.stop()

    def envs(self):
        p = self.site.secret_prefix
        return {p + "_HOST": "localhost", p + "_USER": self.ftp.user, p + "_PASSWORD": self.ftp.password,
                "DEPLOYER_TEST_MODE": "1", "GITHUB_ACTIONS": None, "GITHUB_STEP_SUMMARY": None, "GITHUB_OUTPUT": None}

    def test_args(self):
        return ["--test-cafile", str(CA / "ca.pem"), "--test-port", str(self.ftp.port), "--test-base-url", self.web.base_url]

    def run(self, mode, *, confirm="", auto="", break_backup=False, between=None):
        """prepare →（バックアップの保存と取り出し）→ apply。戻り値：(prepareの判定, applyの終了コード, result)"""
        self.n += 1
        work = self.dir / f"work{self.n}"
        state_file = self.dir / f"state{self.n}.json"
        if self.state:
            state_file.write_text(json.dumps(self.state))
        with env(**self.envs()):
            rc = cli.main(["prepare", "--site", self.site.id, "--dist", str(self.dist), "--work", str(work),
                           "--mode", mode, "--confirm", confirm, "--auto-sites", auto, "--sha", "a" * 40,
                           "--state", str(state_file), "--token", f"t{self.n}"] + self.test_args())
            decision = json.loads((work / "decision.json").read_text())
            if decision["decision"] != "apply":
                return decision, rc, None
            vb = self.dir / f"verified{self.n}"   # 保存先（Actions の成果物）から取り出し直したもの
            shutil.copytree(work / "backup", vb)
            if break_backup:
                for f in (vb / "files").rglob("*"):
                    if f.is_file():
                        f.write_bytes(b"broken")
                        break
            if between:
                between()
            rc = cli.main(["apply", "--site", self.site.id, "--dist", str(self.dist), "--work", str(work),
                           "--backup", str(vb), "--sha", "a" * 40, "--cache-wait", "0"] + self.test_args())
            result = json.loads((work / "result.json").read_text())
            self.last_backup = vb
            if result.get("state"):
                self.state = result["state"]
            return decision, rc, result

    def restore(self, backup, confirm):
        with env(**self.envs()):
            return cli.main(["restore", "--site", self.site.id, "--backup", str(backup), "--confirm", confirm] + self.test_args())


def leftovers(root):
    return [p for p in H.tree(root) if ".deploy-tmp-" in p or "deploy-probe-" in p]


class EbisenkaTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig("ebisenka")

    def tearDown(self):
        self.r.close()

    def test_initial_publish_needs_explicit_approval(self):
        d, rc, _ = self.r.run("auto", auto="ebisenka")
        self.assertEqual(d["status"], "awaiting-approval")          # 初回は自動では公開しない
        self.assertEqual(H.tree(self.r.root), {})
        d, rc, _ = self.r.run("publish", confirm="wrong")
        self.assertEqual(d["status"], "rejected")                    # 確認入力の誤り
        d, rc, res = self.r.run("publish", confirm="ebisenka.jp")
        self.assertEqual((rc, res["status"]), (0, "published"))
        self.assertEqual(H.tree(self.r.root), H.tree(self.r.dist))   # サーバー＝ビルド
        self.assertEqual(leftovers(self.r.root), [])

    def test_auto_update_only_changed_file_then_nochange(self):
        self.r.run("publish", confirm="ebisenka.jp")
        p = self.r.dist / "about/index.html"
        p.write_text(p.read_text().replace("会社案内", "会社案内（更新）", 1))
        d, rc, res = self.r.run("auto", auto="ebisenka,corporate")
        self.assertEqual((d["status"], res["status"]), ("ready", "published"))
        self.assertEqual([x for x in res["log"]], ["置き換え：about/index.html"])
        self.assertEqual(H.tree(self.r.root), H.tree(self.r.dist))
        d, _, _ = self.r.run("auto", auto="ebisenka")
        self.assertEqual(d["status"], "no-change")

    def test_auto_disabled_site_waits(self):
        self.r.run("publish", confirm="ebisenka.jp")
        (self.r.dist / "robots.txt").write_text("User-agent: *\n")
        d, _, _ = self.r.run("auto", auto="corporate")
        self.assertEqual(d["status"], "awaiting-approval")

    def test_htaccess_change_needs_approval(self):
        self.r.run("publish", confirm="ebisenka.jp")
        with open(self.r.dist / ".htaccess", "a") as f:
            f.write("# change\n")
        d, _, _ = self.r.run("auto", auto="ebisenka")
        self.assertEqual(d["status"], "awaiting-approval")
        self.assertTrue(any(".htaccess" in x for x in d["approval_reasons"]))

    def test_verify_failure_rolls_back_to_previous(self):
        self.r.run("publish", confirm="ebisenka.jp")
        before = H.tree(self.r.root)
        for f in ("wholesale/index.html", "assets/css/site.css"):
            p = self.r.dist / f
            p.write_bytes(p.read_bytes() + b"\n<!-- v2 -->\n")
        (self.r.dist / "new-page.html").write_text("<!doctype html><title>x</title>")
        self.r.web.fail.add("/wholesale/")                           # 公開後の検証で不合格にする
        d, rc, res = self.r.run("auto", auto="ebisenka")
        self.assertEqual((rc, res["status"]), (1, "rolled-back"))
        self.assertEqual(H.tree(self.r.root), before)                # 公開前とまったく同じ（追加分も消えている）
        self.assertEqual(leftovers(self.r.root), [])

    def test_write_failure_midway_rolls_back(self):
        self.r.run("publish", confirm="ebisenka.jp")
        before = H.tree(self.r.root)
        for f in ("bento/index.html", "kitchen/index.html"):
            p = self.r.dist / f
            p.write_bytes(p.read_bytes() + b"\n<!-- v2 -->\n")
        self.r.ftp.fail_rename.add("kitchen/index.html")             # 2件目の置き換えで失敗させる
        d, rc, res = self.r.run("auto", auto="ebisenka")
        self.assertEqual(res["status"], "rolled-back")
        self.assertEqual({k: v for k, v in H.tree(self.r.root).items() if ".deploy-tmp-" not in k}, before)

    def test_rollback_failure_is_reported_as_urgent(self):
        self.r.run("publish", confirm="ebisenka.jp")
        p = self.r.dist / "privacy/index.html"
        p.write_bytes(p.read_bytes() + b"\n<!-- v2 -->\n")
        # 計画の後に Web 側が壊れる：公開後の検証も、戻した後の確認も不合格（トップが 500 のまま）
        d, rc, res = self.r.run("auto", auto="ebisenka", between=lambda: self.r.web.fail.update({"/privacy/", "/"}))
        self.assertEqual((rc, res["status"]), (2, "restore-failed"))
        self.assertTrue(res["problems"])

    def test_server_changed_after_plan_aborts_without_writing(self):
        self.r.run("publish", confirm="ebisenka.jp")
        p = self.r.dist / "bento/index.html"
        p.write_bytes(p.read_bytes() + b"<!-- v2 -->")
        def someone_edits():
            (self.r.root / "bento/index.html").write_bytes(b"edited by hand")
        d, rc, res = self.r.run("auto", auto="ebisenka", between=someone_edits)
        self.assertEqual(res["status"], "aborted")
        self.assertEqual((self.r.root / "bento/index.html").read_bytes(), b"edited by hand")

    def test_drift_since_last_publish_needs_approval(self):
        self.r.run("publish", confirm="ebisenka.jp")
        (self.r.root / "kitchen/index.html").write_bytes(b"edited by hand")
        d, _, _ = self.r.run("auto", auto="ebisenka")
        self.assertEqual(d["status"], "awaiting-approval")
        self.assertTrue(any("直接変更" in x for x in d["approval_reasons"]))

    def test_broken_backup_aborts(self):
        self.r.run("publish", confirm="ebisenka.jp")
        p = self.r.dist / "bento/index.html"
        p.write_bytes(p.read_bytes() + b"<!-- v2 -->")
        before = H.tree(self.r.root)
        d, rc, res = self.r.run("auto", auto="ebisenka", break_backup=True)
        self.assertEqual(res["status"], "aborted")
        self.assertEqual(H.tree(self.r.root), before)

    def test_stale_cache_is_warning_not_rollback(self):
        self.r.run("publish", confirm="ebisenka.jp")
        p = self.r.dist / "about/index.html"
        p.write_bytes(p.read_bytes() + b"<!-- v2 -->")
        self.r.web.stale["/about/"] = b"old cached page"
        d, rc, res = self.r.run("auto", auto="ebisenka")
        self.assertEqual(res["status"], "published-with-warnings")
        self.assertIn("キャッシュ", res["warnings"][0])

    def test_leftover_temp_file_stops(self):
        self.r.run("publish", confirm="ebisenka.jp")
        (self.r.root / "index.html.deploy-tmp-old").write_bytes(b"x")
        (self.r.dist / "robots.txt").write_text("User-agent: *\n")
        d, rc, _ = self.r.run("auto", auto="ebisenka")
        self.assertEqual(d["status"], "aborted")

    def test_forbidden_file_in_build_stops(self):
        (self.r.dist / "secret.php").write_text("<?php")
        d, rc, _ = self.r.run("publish", confirm="ebisenka.jp")
        self.assertEqual(d["status"], "aborted")
        self.assertEqual(H.tree(self.r.root), {})


class DestinationTests(unittest.TestCase):
    def test_wrong_account_scope_stops(self):
        r = Rig("corporate")
        try:
            H.copytree(BUILD / "fukushin-e.co.jp", r.root)
            (r.root / "ebisenka.jp").mkdir()                         # 他ドメインのフォルダが見える＝接続先が広すぎる
            d, _, _ = r.run("publish", confirm="fukushin-e.co.jp")
            self.assertEqual(d["status"], "aborted")
            self.assertIn("ドメイン", d["reason"])
        finally:
            r.close()

    def test_wrong_site_markers_stop(self):
        r = Rig("corporate")
        try:
            H.copytree(BUILD / "ebisenka.jp", r.root)                # 別サイトの中身
            d, _, _ = r.run("publish", confirm="fukushin-e.co.jp")
            self.assertEqual(d["status"], "aborted")
        finally:
            r.close()

    def test_untrusted_certificate_never_sends_password(self):
        r = Rig("ebisenka", cert="bad")
        try:
            with self.assertRaises(FtpsError) as cm:
                Ftps("localhost", r.ftp.user, r.ftp.password, port=r.ftp.port, cafile=str(CA / "ca.pem"))
            self.assertIn("証明書", str(cm.exception))
        finally:
            r.close()

    def test_paths_and_readonly(self):
        for bad in ("../x", "/etc/passwd", "a/../b", "a b", "a//b", ""):
            with self.assertRaises(FtpsError):
                check_path(bad)
        r = Rig("ebisenka")
        try:
            f = Ftps("localhost", r.ftp.user, r.ftp.password, port=r.ftp.port, cafile=str(CA / "ca.pem"))
            with self.assertRaises(FtpsError):
                f.put_new("x.txt", b"x")
            f.close()
            self.assertFalse((r.root / "x.txt").exists())
        finally:
            r.close()

    def test_test_overrides_refused_on_actions(self):
        r = Rig("ebisenka")
        try:
            with env(GITHUB_ACTIONS="true", DEPLOYER_TEST_MODE="1"):
                with self.assertRaises(SystemExit):
                    cli.main(["prepare", "--site", "ebisenka", "--dist", str(r.dist), "--work", str(r.dir / "w"),
                              "--mode", "check"] + r.test_args())
        finally:
            r.close()


class CorporateTests(unittest.TestCase):
    def test_first_publish_touches_only_changed_files_and_keeps_mail_php(self):
        r = Rig("corporate")
        try:
            base = H.tmpdir() / "base"
            subprocess.run(["git", "-C", str(ROOT), "archive", "a6ba69e", "corporate"], check=True,
                           stdout=open(base.with_suffix(".tar"), "wb"))
            shutil.unpack_archive(str(base.with_suffix(".tar")), str(base))
            H.copytree(base / "corporate", r.root)                    # 公開中のサイト（取り込み時点）
            (r.root / "mail.php").write_text("<?php // existing form")
            (r.root / ".htaccess").write_text("# existing")
            protected = {k: v for k, v in H.tree(r.root).items() if k in ("mail.php", ".htaccess")}
            d, rc, res = r.run("publish", confirm="fukushin-e.co.jp")
            self.assertEqual(res["status"], "published")
            touched = sorted(x.split("：", 1)[1].replace("（最後）", "") for x in res["log"])
            self.assertEqual(touched, sorted(["assets/css/style.css", "assets/images/fukuki-bishoku-logo.webp",
                                              "business.html", "contact.html", "index.html", "restaurant.html"]))
            self.assertEqual({k: v for k, v in H.tree(r.root).items() if k in protected}, protected)
            (r.dist / "mail.php").write_text("<?php // overwrite attempt")
            d, _, _ = r.run("auto", auto="corporate")
            self.assertEqual(d["status"], "aborted")                  # mail.php はビルドに含められない
        finally:
            r.close()


    def test_tampered_backup_cannot_touch_protected_files(self):
        r = Rig("corporate")
        try:
            H.copytree(BUILD / "fukushin-e.co.jp", r.root)
            (r.root / "mail.php").write_text("<?php // existing form")
            b = r.dir / "fake-backup"
            (b / "files").mkdir(parents=True)
            (b / "files/mail.php").write_text("<?php // malicious")
            (b / "plan.json").write_text(json.dumps({"site": "corporate", "token": "x", "new_dirs": [], "htaccess": None,
                "items": [{"path": "mail.php", "op": "replace", "old": core.sha256(b"<?php // malicious"),
                           "new": core.sha256(b"<?php // existing form"), "size": 1}],
                "backup": {"files": {"mail.php": core.sha256(b"<?php // malicious")}, "htaccess": None},
                "destination": {}, "counts": {}}))
            self.assertEqual(r.restore(b, "fukushin-e.co.jp"), 1)
            self.assertEqual((r.root / "mail.php").read_text(), "<?php // existing form")
        finally:
            r.close()


class FukukiSwitchTests(unittest.TestCase):
    WP_HTACCESS = "# BEGIN WordPress\nRewriteEngine On\nRewriteRule . /index.php [L]\n# END WordPress\n"

    def make_wp(self, root):
        for f, body in {"index.php": "<?php // wp", "wp-config.php": "<?php // secret", ".htaccess": self.WP_HTACCESS,
                        "wp-includes/js/wp-emoji-loader.min.js": "/* emoji */", "wp-admin/index.php": "<?php",
                        "wp-content/uploads/2026/10/photo.jpg": "JPEGDATA", "wp-content/themes/t/style.css": "/* t */"}.items():
            p = root / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)

    def test_switch_from_wordpress_and_restore(self):
        r = Rig("fukuki", wordpress=True)
        try:
            self.make_wp(r.root)
            wp_before = H.tree(r.root)
            d, _, _ = r.run("auto", auto="fukuki")
            self.assertEqual(d["status"], "awaiting-approval")       # 切り替えは承認が必要
            self.assertTrue(any("切り替え" in x for x in d["approval_reasons"]))
            d, rc, res = r.run("publish", confirm="ebisenka-seavans.com")
            self.assertEqual(res["status"], "published")
            self.assertEqual(res["log"][-1], "追加：index.html（最後）")       # トップは最後に切り替わる
            self.assertIn("切り替え設定", res["log"][-2])
            after = H.tree(r.root)
            for k, v in wp_before.items():                           # WordPress のファイルはすべてそのまま
                if k != ".htaccess":
                    self.assertEqual(after[k], v, k)
            ht = (r.root / ".htaccess").read_text()
            self.assertTrue(ht.startswith(C.FUKUKI_HTACCESS_BLOCK) and ht.endswith(self.WP_HTACCESS))
            # 手動の復元（その公開の事前バックアップ）で、WordPress の状態に完全に戻る
            self.assertEqual(r.restore(r.last_backup, "ebisenka-seavans.com"), 0)
            self.assertEqual(H.tree(r.root), wp_before)
        finally:
            r.close()

    def test_switch_failure_rolls_back_to_wordpress(self):
        r = Rig("fukuki", wordpress=True)
        try:
            self.make_wp(r.root)
            wp_before = H.tree(r.root)
            r.web.fail.add("/kitchen/")
            d, rc, res = r.run("publish", confirm="ebisenka-seavans.com")
            self.assertEqual(res["status"], "rolled-back")
            self.assertEqual(H.tree(r.root), wp_before)              # .htaccess も元どおり・追加分は削除
        finally:
            r.close()

    def test_cache_plugin_rules_stop_the_switch(self):
        r = Rig("fukuki", wordpress=True)
        try:
            self.make_wp(r.root)
            (r.root / ".htaccess").write_text("# BEGIN WPSuperCache\nRewriteEngine On\n# END WPSuperCache\n" + self.WP_HTACCESS)
            before = H.tree(r.root)
            d, _, _ = r.run("publish", confirm="ebisenka-seavans.com")
            self.assertEqual(d["status"], "aborted")
            self.assertIn("キャッシュ", d["reason"])
            self.assertEqual(H.tree(r.root), before)
        finally:
            r.close()

    def test_restore_refuses_when_newer_publish_exists(self):
        r = Rig("fukuki", wordpress=True)
        try:
            self.make_wp(r.root)
            r.run("publish", confirm="ebisenka-seavans.com")
            first = r.last_backup
            p = r.dist / "access/index.html"
            p.write_bytes(p.read_bytes() + b"<!-- v2 -->")
            d, rc, res = r.run("auto", auto="fukuki")
            self.assertEqual(res["status"], "published")
            p2 = r.dist / "index.html"
            p2.write_bytes(p2.read_bytes() + b"<!-- v3 -->")
            r.run("auto", auto="fukuki")
            before = H.tree(r.root)
            self.assertEqual(r.restore(first, "ebisenka-seavans.com"), 1)  # 後の公開で変わったファイルがある
            self.assertEqual(H.tree(r.root), before)
        finally:
            r.close()


if __name__ == "__main__":
    unittest.main()
