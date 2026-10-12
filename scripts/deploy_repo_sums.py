#!/usr/bin/env python3
"""deploy-repo/deployer/SHA256SUMS と、ワークフローに固定するそのハッシュ（DEPLOYER_SUMS_SHA256）を作り直す。
--check では、どちらも最新かどうかだけを確かめる。

デプロイ専用リポジトリのワークフローは、毎回の実行の最初に
  1. deployer/SHA256SUMS が、ワークフローに固定したハッシュと一致すること
  2. deployer/*.py が、SHA256SUMS と一致すること
を確かめ、違えば何もせずに止まる。ワークフローはご本人しか変更できない（GITHUB_TOKEN ではワークフローを
変更できない）ため、deployer/ だけが書き換えられても、本番の接続情報を使うコードは動かない。
"""
import hashlib
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "deploy-repo"
SUMS = BASE / "deployer" / "SHA256SUMS"
WORKFLOWS = [BASE / ".github/workflows/deploy.yml", BASE / ".github/workflows/restore.yml", BASE / ".github/workflows/inspect.yml",
             BASE / ".github/workflows/mailfix.yml"]
PIN = re.compile(r"^(  DEPLOYER_SUMS_SHA256: )(\S+)$", re.M)


def render():
    files = sorted(p for p in (BASE / "deployer").glob("*.py"))
    return "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(BASE).as_posix()}\n" for p in files)


def main():
    text = render()
    pin = hashlib.sha256(text.encode()).hexdigest()
    if "--check" in sys.argv:
        bad = []
        if not SUMS.is_file() or SUMS.read_text() != text:
            bad.append("deploy-repo/deployer/SHA256SUMS")
        for w in WORKFLOWS:
            m = PIN.search(w.read_text())
            if not m or m.group(2) != pin:
                bad.append(str(w.relative_to(BASE.parent)))
        if bad:
            sys.exit("古いものがあります（python3 scripts/deploy_repo_sums.py で作り直してください）：" + "、".join(bad))
        print(f"SHA256SUMS とワークフローの固定値は最新です（{pin[:16]}…）")
        return
    SUMS.write_text(text)
    for w in WORKFLOWS:
        t = w.read_text()
        if not PIN.search(t):
            sys.exit(f"{w} に DEPLOYER_SUMS_SHA256 の行がありません")
        w.write_text(PIN.sub(lambda m: m.group(1) + pin, t))
    print(text, end="")
    print(f"ワークフローに固定するハッシュ：{pin}")


if __name__ == "__main__":
    main()
