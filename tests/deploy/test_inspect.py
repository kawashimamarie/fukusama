"""読み取り専用の調査ツール：許可したファイルだけ・出力にソースの断片や値が出ないこと・一時ファイルが残らないこと"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy-repo"))
from deployer import inspect  # noqa: E402

SECRET = "s3cr3t-Pa55"


class InspectTests(unittest.TestCase):
    def test_only_allowlisted_files(self):
        self.assertEqual(inspect.ALLOWED, {("corporate", "mail.php")})

    def test_sanitize_hides_path_and_quoted_fragments(self):
        p = "/tmp/runner/inspect-x/mail.php"
        out = (f'PHP Parse error:  syntax error, unexpected identifier "{SECRET}" in {p} on line 7\n'
               f"Errors parsing {p}\nsome other line with {SECRET}\n")
        lines = inspect.sanitize(out, p)
        joined = "\n".join(lines)
        self.assertNotIn(SECRET, joined)
        self.assertNotIn("/tmp/", joined)
        self.assertIn("on line 7", joined)
        self.assertIn("1 行は伏せました", joined)

    def test_facts_have_no_content(self):
        data = b"\xef\xbb\xbf<?php\ndeclare(strict_types=1);\n$pw = '" + SECRET.encode() + b"';\n"
        f = inspect.facts(data)
        self.assertTrue(f["bom"])
        self.assertEqual(f["bytes_before_open_tag"], 3)
        self.assertEqual(f["strict_types_line"], 2)
        self.assertNotIn(SECRET, repr(f))

    @unittest.skipUnless(shutil.which("php"), "php がありません")
    def test_check_reports_strict_types_error_without_source(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        os.environ["RUNNER_TEMP"] = base
        self.addCleanup(os.environ.pop, "RUNNER_TEMP", None)
        data = b"\xef\xbb\xbf<?php\ndeclare(strict_types=1);\n$pw = '" + SECRET.encode() + b"';\n"
        r = inspect.check(data)
        text = inspect.report(inspect.facts(data), r)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(base, text)
        self.assertIn("strict_types declaration must be the very first statement", text)
        self.assertIn("on line 2", text)
        self.assertEqual(r["tokens_until_declare"][0], "T_INLINE_HTML(len=3)@1")
        self.assertEqual(os.listdir(base), [])     # 一時ファイルは消えている

    @unittest.skipUnless(shutil.which("php"), "php がありません")
    def test_check_ok_file(self):
        r = inspect.check(b"<?php\ndeclare(strict_types=1);\necho 1;\n")
        self.assertEqual(r["lint_exit"], 0)
        self.assertEqual(r["lint"], ["No syntax errors detected in mail.php"])


if __name__ == "__main__":
    unittest.main()
