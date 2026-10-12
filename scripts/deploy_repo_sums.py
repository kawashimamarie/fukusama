#!/usr/bin/env python3
"""deploy-repo/deployer/SHA256SUMS を作り直す（--check で、最新かどうかだけ確かめる）。

デプロイ専用リポジトリのワークフローは、毎回の実行の最初にこの一覧で公開の仕組み（deployer/*.py）を照合し、
1ファイルでも違えば（貼り間違い・欠け）何もせずに止まる。
"""
import hashlib
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "deploy-repo"
SUMS = BASE / "deployer" / "SHA256SUMS"


def render():
    files = sorted(p for p in (BASE / "deployer").glob("*.py"))
    return "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(BASE).as_posix()}\n" for p in files)


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        if not SUMS.is_file() or SUMS.read_text() != text:
            sys.exit("deploy-repo/deployer/SHA256SUMS が古いです：python3 scripts/deploy_repo_sums.py で作り直してください")
        print("SHA256SUMS は最新です")
    else:
        SUMS.write_text(text)
        print(text, end="")
