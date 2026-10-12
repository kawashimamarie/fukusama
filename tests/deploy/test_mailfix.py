"""mail.php の BOM の除去（専用の修正）の模擬テスト（ローカルの FTPS・HTTP。本番には接続しない）"""
import io
import shutil
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy-repo"))
sys.path.insert(0, str(Path(__file__).parent))

import harness as H  # noqa: E402
import test_deployer as T  # noqa: E402
from deployer import mailfix  # noqa: E402

SECRET = "Sm7p-s3cret-VALUE"
BODY = ("<?php\ndeclare(strict_types=1);\n"
        "if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') { header('Location: contact.html'); exit; }\n"
        f"$smtp_password = '{SECRET}';\n"
        "mb_send_mail('info@example.jp', 'subject', 'body');\n?>\n<p>ok</p>\n").encode()
ORIG = mailfix.BOM + BODY


def setUpModule():
    T.setUpModule()


@unittest.skipUnless(shutil.which("php"), "php がありません")
class MailFixTests(unittest.TestCase):
    def setUp(self):
        self.r = T.Rig("corporate")
        H.copytree(T.BUILD / "fukushin-e.co.jp", self.r.root)
        (self.r.root / "mail.php").write_bytes(ORIG)
        (self.r.root / ".htaccess").write_text("# existing")
        self.others = {k: v for k, v in H.tree(self.r.root).items() if k != "mail.php"}

    def tearDown(self):
        self.r.close()

    def run_mode(self, mode, confirm=""):
        buf = io.StringIO()
        with T.env(**self.r.envs()), redirect_stdout(buf):
            rc = mailfix.main(["--mode", mode, "--confirm", confirm] + self.r.test_args())
        out = buf.getvalue()
        self.assertNotIn(SECRET, out)
        self.assertNotIn("declare(", out)
        self.assertNotIn(str(self.r.root), out)
        return rc, out

    def others_unchanged(self):
        self.assertEqual({k: v for k, v in H.tree(self.r.root).items() if k != "mail.php"}, self.others)

    def test_check_writes_nothing(self):
        rc, out = self.run_mode("check")
        self.assertEqual(rc, 0, out)
        self.assertIn("修正できます", out)
        self.assertIn("No syntax errors detected in mail.php", out)
        self.assertEqual((self.r.root / "mail.php").read_bytes(), ORIG)
        self.others_unchanged()

    def test_apply_removes_only_the_bom(self):
        rc, out = self.run_mode("apply", "fukushin-e.co.jp")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.r.root / "mail.php").read_bytes(), BODY)
        self.assertIn("構文エラー／致命的エラーの表示 なし", out)
        self.assertEqual(T.leftovers(self.r.root), [])
        self.assertFalse([p for p in H.tree(self.r.root) if "mail.deploy-tmp-" in p])
        self.others_unchanged()
        # 戻す：先頭に BOM を付け直す
        rc, out = self.run_mode("restore", "fukushin-e.co.jp")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.r.root / "mail.php").read_bytes(), ORIG)
        self.others_unchanged()

    def test_requires_confirm(self):
        rc, _ = self.run_mode("apply", "wrong")
        self.assertEqual(rc, 1)
        self.assertEqual((self.r.root / "mail.php").read_bytes(), ORIG)

    def test_stops_without_bom(self):
        (self.r.root / "mail.php").write_bytes(BODY)
        rc, out = self.run_mode("apply", "fukushin-e.co.jp")
        self.assertEqual(rc, 1)
        self.assertIn("BOM がありません", out)
        self.assertEqual((self.r.root / "mail.php").read_bytes(), BODY)

    def test_stops_if_fixed_file_still_has_syntax_error(self):
        bad = mailfix.BOM + b"<?php\ndeclare(strict_types=1);\nif ( {\n"
        (self.r.root / "mail.php").write_bytes(bad)
        rc, out = self.run_mode("apply", "fukushin-e.co.jp")
        self.assertEqual(rc, 1)
        self.assertIn("構文チェックが合格しません", out)
        self.assertEqual((self.r.root / "mail.php").read_bytes(), bad)

    def test_rolls_back_when_live_check_fails(self):
        # 構文は通るが、実行すると致命的エラーになる（本番の GET で見つかる）→ 自動で元に戻す
        bad = mailfix.BOM + b"<?php\ndeclare(strict_types=1);\nundefined_fn_xyz();\n"
        (self.r.root / "mail.php").write_bytes(bad)
        orig_sandbox = mailfix.sandbox_get
        mailfix.sandbox_get = lambda w, d, php="php": {"status": 200, "redirect": False, "calls_disabled": False,
                                                        "fatal": False, "warning": False}
        try:
            rc, out = self.run_mode("apply", "fukushin-e.co.jp")
        finally:
            mailfix.sandbox_get = orig_sandbox
        self.assertEqual(rc, 1)
        self.assertIn("修正前の中身に戻しました", out)
        self.assertEqual((self.r.root / "mail.php").read_bytes(), bad)
        self.assertFalse([p for p in H.tree(self.r.root) if "mail.deploy-tmp-" in p])

    def test_no_live_get_when_get_would_send(self):
        body = b"<?php\ndeclare(strict_types=1);\nmb_send_mail('a@example.jp', 's', 'b');\n"
        (self.r.root / "mail.php").write_bytes(mailfix.BOM + body)
        rc, out = self.run_mode("check")
        self.assertEqual(rc, 0, out)
        self.assertIn("送信/外部接続の関数の呼び出し あり", out)
        self.assertNotIn("公開中の mail.php（修正前", out)

    def test_guard_blocks_other_paths(self):
        g = mailfix.Guarded(object())
        for p in ("index.html", ".htaccess", "mail.php.bak", "contact.php", "mail.deploy-tmp-zz.php"):
            with self.assertRaises(mailfix.Stop):
                g.put_new(p, b"x")
        with self.assertRaises(mailfix.Stop):
            g.delete("mail.php")


if __name__ == "__main__":
    unittest.main()
