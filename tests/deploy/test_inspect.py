"""読み取り専用の調査ツール：秘密らしき値を伏せること・許可したファイル以外は取り出せないこと"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy-repo"))
from deployer import inspect  # noqa: E402


class InspectTests(unittest.TestCase):
    def test_redacts_secret_like_values_only(self):
        s = ("<?php\n$to = 'info@fukushin-e.co.jp';\n$smtp_password = \"s3cr3t!\";\n"
             "$c = ['smtp_pass' => 'abc', 'from' => 'x@y.z'];\ndefine('MAIL_PASSWORD', 'pw');\n")
        r, found = inspect.redact(s)
        for secret in ("s3cr3t!", "'abc'", "'pw'"):
            self.assertNotIn(secret, r)
        self.assertIn("info@fukushin-e.co.jp", r)
        self.assertIn("'from' => 'x@y.z'", r)
        self.assertEqual(len(found), 3)

    def test_only_allowlisted_files(self):
        self.assertEqual(inspect.ALLOWED, {("corporate", "mail.php")})


if __name__ == "__main__":
    unittest.main()
