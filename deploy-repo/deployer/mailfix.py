"""コーポレートの mail.php の先頭の BOM（EF BB BF の3バイト）だけを除く、専用の修正（2026-10-12 ご本人の承認）。

  python -m deployer.mailfix --mode check                       # 本番に書き込まない（取得・検査・照合だけ）
  python -m deployer.mailfix --mode apply --confirm fukushin-e.co.jp
  python -m deployer.mailfix --mode restore --confirm fukushin-e.co.jp   # 先頭に BOM を戻す（修正前に戻す）

- 触れるのは corporate の mail.php と、その一時ファイル（mail.deploy-tmp-<乱数>.php）だけ。ほかのファイルには書き込めない
- 変更は「先頭の3バイトを除く」だけ。それ以外の差分があれば止まる
- 中身（全文・抜粋・ハッシュ）はログ・実行の要約・成果物に出さない。出すのは大きさ・行番号・はい/いいえだけ
- 修正前の中身は、この実行の中だけ（メモリと本人だけが読める一時フォルダー）に持ち、照合と自動の復元に使い、終わったら消す。
  修正前の中身は「修正後の先頭に BOM の3バイトを付けたもの」とバイト単位で同じなので、後から戻すときは restore で BOM を付け直す
- mail.php を実行するのは、この実行環境の中の模擬サーバーだけ（メール送信・外部接続の関数を無効にして GET を1回）。
  その結果、GET でメール送信や外部接続に進まないことを確かめたときだけ、本番の mail.php に GET を1回送ってエラーが消えたかを見る。
  POST（送信）はしない
"""
import argparse
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config as C
from . import core, web
from .ftps import Ftps, FtpsError

SITE = "corporate"
TARGET = "mail.php"
BOM = b"\xef\xbb\xbf"
TMP = re.compile(r"^mail\.deploy-tmp-[0-9a-f]{12}\.php$")
STRICT_ERR = "strict_types declaration must be the very first statement"
BAD_LIVE = ("Fatal error", "Parse error", STRICT_ERR)
LEAK = ("/home/", "public_html", "Warning:", "Notice:", "Deprecated:")
SANDBOX_DISABLED = ("mail,mb_send_mail,fsockopen,pfsockopen,stream_socket_client,socket_create,socket_connect,"
                    "curl_init,curl_exec,curl_multi_exec,exec,shell_exec,system,passthru,popen,proc_open")


class Stop(Exception):
    pass


class Guarded:
    """書き込みを mail.php と、その一時ファイルだけに限る（それ以外の名前は送る前に止める）"""

    def __init__(self, ftp):
        self.ftp = ftp

    def _ok(self, path):
        if path != TARGET and not TMP.match(path):
            raise Stop(f"mail.php 以外への書き込みは許可されていません（{path}）")

    def put_new(self, path, data):
        self._ok(path)
        self.ftp.put_new(path, data)

    def rename(self, src, dst):
        self._ok(src)
        self._ok(dst)
        self.ftp.rename(src, dst)

    def delete(self, path):
        if not TMP.match(path):
            raise Stop(f"一時ファイル以外は削除できません（{path}）")
        self.ftp.delete(path)

    def get(self, path):
        return self.ftp.get(path)

    def listdir(self, path=""):
        return self.ftp.listdir(path)


# ---------------------------------------------------------------- 実行環境の中だけの検査
class Work:
    """本人だけが読める一時フォルダー。終わったら必ず消す"""

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mailfix-", dir=os.environ.get("RUNNER_TEMP") or None))
        os.chmod(self.dir, 0o700)
        return self

    def write(self, name, data):
        d = self.dir / secrets.token_hex(4)
        d.mkdir(mode=0o700)
        p = d / name
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return p

    def __exit__(self, *a):
        shutil.rmtree(self.dir, ignore_errors=True)


def lint(work, data, php="php"):
    """php -l の結果（終了コード・伏せた行）。ファイルの場所は mail.php に置き換え、引用符の中は伏せる"""
    from .inspect import sanitize
    p = work.write("mail.php", data)
    r = subprocess.run([php, "-d", "display_errors=stderr", "-d", "log_errors=0", "-d", "html_errors=0", "-l", str(p)],
                       capture_output=True, text=True, errors="replace", timeout=60)
    return r.returncode, sanitize(r.stdout + "\n" + r.stderr, p)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sandbox_get(work, data, php="php"):
    """実行環境の中の模擬サーバー（PHP 内蔵）で、修正後の mail.php に GET を1回。
    メール送信・外部接続・コマンド実行の関数は無効にする（呼ばれると「未定義の関数」のエラーになる）。
    返すのは、状態コード・転送の有無・はい/いいえだけ"""
    p = work.write("mail.php", data)
    port = _free_port()
    proc = subprocess.Popen([php, "-d", "display_errors=1", "-d", "html_errors=0", "-d", "log_errors=0",
                             "-d", "disable_functions=" + SANDBOX_DISABLED, "-d", "sendmail_path=/bin/false",
                             "-d", "allow_url_fopen=0", "-d", "mbstring.language=Japanese",
                             "-S", f"127.0.0.1:{port}", "-t", str(p.parent)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(p.parent))
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        st, headers, body = web.fetch(f"http://127.0.0.1:{port}/mail.php", retries=1, timeout=20)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    text = body.decode("utf-8", "replace")
    return {"status": st, "redirect": "location" in headers,
            "calls_disabled": "Call to undefined function" in text,
            "fatal": "Fatal error" in text or "Parse error" in text,
            "warning": any(k in text for k in ("Warning:", "Notice:", "Deprecated:"))}


def live_get(base_url, token):
    """本番の mail.php に GET を1回（サーバーのキャッシュを通さない）。中身は出さず、はい/いいえだけ"""
    st, headers, body = web.fetch(web.bust(base_url.rstrip("/") + "/" + TARGET, token), retries=2)
    text = body.decode("utf-8", "replace")
    return {"status": st, "redirect": "location" in headers,
            "fatal": any(k in text for k in BAD_LIVE),
            "strict_types_error": STRICT_ERR in text,
            "shows_server_path_or_warning": any(k in text for k in LEAK)}


# ---------------------------------------------------------------- 本番の確認（読み取り）
def connect(site, readonly, extra):
    p = site.secret_prefix
    return Ftps(os.environ.get(p + "_HOST", ""), os.environ.get(p + "_USER", ""), os.environ.get(p + "_PASSWORD", ""),
                readonly=readonly, **extra)


def fetch_twice(ftp):
    a = ftp.get(TARGET)
    b = ftp.get(TARGET)
    if a is None:
        raise Stop("mail.php がサーバーにありません")
    if a != b:
        raise Stop("mail.php を2回読んだ中身が一致しません（読み取りが不安定・または更新中）")
    return a


def check_fix(orig, fixed):
    """差分が「先頭の BOM 3バイトの削除」だけであること"""
    if not orig.startswith(BOM):
        raise Stop("mail.php の先頭に BOM がありません（想定と違うため止めます）")
    if len(orig) - len(fixed) != 3 or orig[:3] != BOM or orig[3:] != fixed or BOM + fixed != orig:
        raise Stop("修正の差分が先頭3バイトの削除だけになっていません")
    if fixed.startswith(BOM):
        raise Stop("BOM が二重に付いています（想定と違うため止めます）")
    if not fixed.startswith(b"<?php"):
        raise Stop("BOM を除いた後の先頭が <?php ではありません")


def leftovers(ftp):
    return sorted(n for n in (ftp.listdir("") or {}) if n.startswith("mail.deploy-tmp-"))


def put(g, data, token, log):
    """一時ファイル（.php・推測できない名前）→ 照合 → 名前の変更で置き換え → 照合"""
    tmp = f"mail.deploy-tmp-{token}.php"
    g.put_new(tmp, data)
    if g.get(tmp) != data:
        g.delete(tmp)
        raise Stop("送った一時ファイルの中身が一致しません（mail.php は変えていません）")
    g.rename(tmp, TARGET)
    log.append("mail.php を置き換え")
    if g.get(TARGET) != data:
        raise Stop("置き換え後の mail.php の中身が一致しません")


# ---------------------------------------------------------------- 本体
def run(mode, confirm, extra, base_url, token, cache_wait=10):
    site = C.SITES[SITE]
    lines = [f"## fukushin-e.co.jp：mail.php の BOM の除去（モード：{mode}）"]
    if mode in ("apply", "restore") and confirm != site.domain:
        return 1, lines + [f"**止めました**：確認入力がドメイン名（{site.domain}）と一致しません（サーバーには接続していません）"]
    escapes = [x for x in os.environ.get(site.secret_prefix + "_PARENT_ABS", "").split(";") if x.strip()]
    log = []
    with Work() as work:
        # 1. 接続先の確認と取得（読み取り専用）
        try:
            ftp = connect(site, True, extra)
            try:
                dest = core.check_destination(ftp, site, base_url, token, escapes)
                cur = fetch_twice(ftp)
                left = leftovers(ftp)
            finally:
                ftp.close()
        except (core.Stop, Stop, FtpsError) as e:
            return 1, lines + [f"**止めました（書き込みなし）**：{e}"]
        esc = "・".join(f"{p} {r}" for p, r in dest.get("escape", []))
        lines += [f"- 接続先：公開フォルダに限定（{esc}）・目印 {dest['markers']}・公開URLとの一致 {dest['identity']}",
                  f"- mail.php：{len(cur)} バイト・先頭の BOM {'あり' if cur.startswith(BOM) else 'なし'}"]
        if left:
            return 1, lines + [f"**止めました（書き込みなし）**：前回の一時ファイルが残っています（{len(left)} 件）"]

        if mode == "restore":
            return _restore(site, cur, extra, base_url, token, work, lines, log)

        # 2. 修正後の中身を作り、実行環境の中で検査する
        orig, fixed = cur, cur[3:]
        try:
            check_fix(orig, fixed)
        except Stop as e:
            return 1, lines + [f"**止めました（書き込みなし）**：{e}"]
        rc0, l0 = lint(work, orig)
        rc1, l1 = lint(work, fixed)
        lines += ["- 差分：先頭の3バイト（BOM）の削除だけ（バイト単位で照合・ほかの中身は同じ）",
                  f"- 構文チェック（修正前）：終了コード {rc0}／" + "／".join(l0),
                  f"- 構文チェック（修正後）：終了コード {rc1}／" + "／".join(l1)]
        if rc1 != 0 or l1 != ["No syntax errors detected in mail.php"]:
            return 1, lines + ["**止めました（書き込みなし）**：修正後の構文チェックが合格しません"]
        backup = work.write("mail.php.orig", orig)      # この実行の中だけのバックアップ
        if backup.read_bytes() != orig or BOM + fixed != backup.read_bytes():
            return 1, lines + ["**止めました（書き込みなし）**：バックアップの照合に失敗しました"]
        lines.append("- バックアップ：修正前の mail.php をこの実行の中に保持し、読み直して照合・"
                     "「修正後＋先頭の BOM」と完全に一致（＝復元できる）")
        sb = sandbox_get(work, fixed)
        safe_get = sb["status"] and not sb["calls_disabled"] and not sb["fatal"]
        lines.append(f"- 模擬サーバーでの GET（メール送信・外部接続は無効）：HTTP {sb['status']}・転送 {'あり' if sb['redirect'] else 'なし'}"
                     f"・送信/外部接続の関数の呼び出し {'あり' if sb['calls_disabled'] else 'なし'}・致命的エラー {'あり' if sb['fatal'] else 'なし'}"
                     f"・警告 {'あり' if sb['warning'] else 'なし'}")
        before = live_get(base_url, token + "a") if safe_get else None
        if before:
            lines.append(f"- 公開中の mail.php（修正前・GET）：HTTP {before['status']}・構文エラーの表示 "
                         f"{'あり' if before['strict_types_error'] else 'なし'}")
        if mode == "check":
            return 0, lines + ["**判定**：修正できます（確認のみ・書き込みなし）"]

        # 3. 書き込み（mail.php だけ）
        try:
            ftp = connect(site, False, extra)
        except FtpsError as e:
            return 1, lines + [f"**止めました（書き込みなし）**：{e}"]
        g = Guarded(ftp)
        try:
            try:
                if fetch_twice(g) != orig:
                    raise Stop("確認の後に mail.php が変わりました")
                put(g, fixed, token, log)
            except Exception as e:    # noqa: BLE001  FTP の応答エラーなども含めて、必ず復元へ
                why = str(e) if isinstance(e, (Stop, FtpsError)) else type(e).__name__
                return _rollback(g, orig, token, lines + [f"**書き込みで止まりました**：{why}"], log)
            # 4. 公開後の確認
            if leftovers(g):
                return _rollback(g, orig, token, lines + ["**一時ファイルが残っています**"], log)
            lines.append("- 書き込み：mail.php の1件だけ（一時ファイル → 照合 → 名前の変更 → 照合）・一時ファイルの残りなし")
            if not safe_get:
                lines.append("- 公開URLの確認：模擬サーバーでの GET が安全と確かめられなかったため、本番への GET はしていません"
                             "（サーバーの中身＝修正後・構文チェック合格で確認）")
                return 0, lines + ["**判定**：修正しました（公開URLでの確認は保留）"]
            after = None
            for i in range(3):
                time.sleep(cache_wait if i else 0)
                after = live_get(base_url, token + f"b{i}")
                if after["status"] and not after["fatal"]:
                    break
            lines.append(f"- 公開中の mail.php（修正後・GET・送信なし）：HTTP {after['status']}・転送 {'あり' if after['redirect'] else 'なし'}"
                         f"・構文エラー／致命的エラーの表示 {'あり' if after['fatal'] else 'なし'}"
                         f"・サーバーのパスや警告の表示 {'あり' if after['shows_server_path_or_warning'] else 'なし'}")
            if not after["status"] or after["status"] >= 500 or after["fatal"]:
                return _rollback(g, orig, token, lines + ["**公開後の確認で問題が見つかりました**"], log)
            for page in ("/", "/contact.html"):
                st, _, _ = web.fetch(web.bust(base_url.rstrip("/") + page, token + "c"))
                lines.append(f"- {page}：HTTP {st}")
                if st != 200:
                    return _rollback(g, orig, token, lines + [f"**{page} が表示されません**"], log)
            return 0, lines + ["**判定**：修正しました（mail.php の構文エラーは解消）"]
        finally:
            ftp.close()


def _rollback(g, orig, token, lines, log):
    """修正前の中身に戻す（書き込みが始まっていなければ何もしない）"""
    for n in leftovers(g):
        try:
            g.delete(n)
        except Exception:
            pass
    if not log:
        return 1, lines + ["- mail.php は変えていません"]
    try:
        cur = g.get(TARGET)
        if cur != orig:
            put(g, orig, secrets.token_hex(6), [])
        ok = g.get(TARGET) == orig
    except Exception as e:    # noqa: BLE001
        return 2, lines + [f"**自動の復元に失敗しました**：{type(e).__name__}。restore モードで BOM を付け直してください"]
    return 1, lines + ["- 自動の復元：" + ("修正前の中身に戻しました（バイト単位で一致）" if ok else "**一致しません**")]


def _restore(site, cur, extra, base_url, token, work, lines, log):
    """先頭に BOM を付け直す（修正前に戻す）。付け直した中身が、修正前と同じ構文エラーを再現することを確かめてから書く"""
    if cur.startswith(BOM):
        return 0, lines + ["**判定**：すでに先頭に BOM があります（何もしません）"]
    orig = BOM + cur
    rc, l = lint(work, orig)
    if not any(STRICT_ERR in x and x.endswith("on line 2") for x in l):
        return 1, lines + ["**止めました（書き込みなし）**：BOM を付け直した中身が、修正前と同じ状態になりません"]
    ftp = connect(site, False, extra)
    g = Guarded(ftp)
    try:
        if fetch_twice(g) != cur:
            return 1, lines + ["**止めました（書き込みなし）**：確認の後に mail.php が変わりました"]
        put(g, orig, token, log)
        return 0, lines + ["**判定**：先頭に BOM を付け直しました（修正前の中身に戻りました）"]
    except Exception as e:    # noqa: BLE001
        for n in leftovers(g):
            g.delete(n)
        return 1, lines + [f"**止めました**：{e if isinstance(e, (Stop, FtpsError)) else type(e).__name__}"]
    finally:
        ftp.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="deployer.mailfix")
    ap.add_argument("--mode", required=True, choices=["check", "apply", "restore"])
    ap.add_argument("--confirm", default="")
    ap.add_argument("--test-cafile")
    ap.add_argument("--test-port", type=int)
    ap.add_argument("--test-base-url")
    a = ap.parse_args(argv)
    if os.environ.get("DEPLOYER_TEST_MODE") == "1" and os.environ.get("GITHUB_ACTIONS") != "true":
        extra, base_url, wait = {"cafile": a.test_cafile, "port": a.test_port or 21}, a.test_base_url, 0
    elif a.test_cafile or a.test_port or a.test_base_url:
        raise SystemExit("テスト用の引数は本番では使えません")
    else:
        extra, base_url, wait = {}, C.SITES[SITE].base_url, 10
    rc, lines = run(a.mode, a.confirm, extra, base_url, secrets.token_hex(6), wait)
    text = "\n".join(lines)
    print(text)
    s = os.environ.get("GITHUB_STEP_SUMMARY")
    if s:
        with open(s, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
