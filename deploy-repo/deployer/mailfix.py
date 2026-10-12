"""コーポレートの mail.php の専用の修正（決まった1か所の決まった変更だけ）。
  --fix bom             先頭の BOM（EF BB BF の3バイト）を除く（2026-10-12 ご本人の承認・実施済み）
  --fix display_errors  declare(strict_types=1); の直後に ini_set('display_errors', '0'); の1行を足す（2026-10-12 ご本人の承認・案1）

  python -m deployer.mailfix --fix display_errors --mode check     # 本番に書き込まない（取得・検査・照合だけ）
  python -m deployer.mailfix --fix display_errors --mode apply --confirm fukushin-e.co.jp
  python -m deployer.mailfix --fix display_errors --mode restore --confirm fukushin-e.co.jp   # その修正だけを取り消す

- 触れるのは corporate の mail.php と、その一時ファイル（mail.deploy-tmp-<乱数>.php）だけ。ほかのファイルには書き込めない
- 差分は、修正の種類ごとに決めた変更だけ（バイト単位と行単位で照合）。それ以外の差分があれば止まる
- 中身（全文・抜粋・ハッシュ）はログ・実行の要約・成果物に出さない。出すのは大きさ・行番号・はい/いいえだけ
- 修正前の中身は、この実行の中だけ（メモリと本人だけが読める一時フォルダー）に持ち、照合と自動の復元に使い、終わったら消す。
  修正前の中身は、修正後から「決まった変更を取り消したもの」とバイト単位で同じなので、後から戻すときは restore を使う
- mail.php を実行するのは、この実行環境の中の模擬サーバーだけ（メール送信・外部接続・コマンドの関数を無効にする）。
  GET でメール送信や外部接続に進まないことを確かめたときだけ、本番の mail.php に GET を送って確かめる。本番に POST（送信）はしない
"""
import argparse
import difflib
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


PROBE = "mailfix-display-probe"
PROBE_PHP = ("<?php register_shutdown_function(function () { trigger_error('" + PROBE + "', E_USER_WARNING); });\n").encode()


def sandbox(work, data, method="GET", probe=False, php="php"):
    """実行環境の中の模擬サーバー（PHP 内蔵・エラーは画面に表示する設定＝本番の修正前と同じ）で mail.php に1回だけ要求を送る。
    メール送信・外部接続・コマンド実行の関数は無効にする（呼ばれると「未定義の関数」のエラーになる）。
    probe=True のときは、終了時に必ず警告を1つ出す仕掛けを前に読み込ませ、その警告が画面に出るか（＝エラーの内容や
    パスがレスポンスに出るか）を調べる。返すのは、状態コード・転送の有無・はい/いいえだけ"""
    p = work.write("mail.php", data)
    log = work.write("php-error.log", b"")      # 画面に出さない設定でもエラーを数えられるように、記録にも残す（中身は出さない）
    args = [php, "-d", "display_errors=1", "-d", "html_errors=0", "-d", "log_errors=1", "-d", "error_log=" + str(log),
            "-d", "error_reporting=32767",
            "-d", "disable_functions=" + SANDBOX_DISABLED, "-d", "sendmail_path=/bin/false",
            "-d", "allow_url_fopen=0", "-d", "mbstring.language=Japanese"]
    if probe:
        args += ["-d", "auto_prepend_file=" + str(work.write("probe.php", PROBE_PHP))]
    port = _free_port()
    proc = subprocess.Popen(args + ["-S", f"127.0.0.1:{port}", "-t", str(p.parent)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(p.parent))
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        st, headers, body = web.fetch(f"http://127.0.0.1:{port}/mail.php", method=method,
                                      body="" if method == "POST" else None, retries=1, timeout=20)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    text = body.decode("utf-8", "replace")
    logged = log.read_text(encoding="utf-8", errors="replace")
    return {"status": st, "redirect": "location" in headers,
            "calls_disabled": "Call to undefined function" in text + logged,
            "fatal": any(k in text + logged for k in ("Fatal error", "Parse error")),
            "warning": any(k in text for k in ("Warning:", "Notice:", "Deprecated:", "Fatal error", "Parse error")),
            "probe_shown": PROBE in text or str(work.dir) in text}


def sandbox_get(work, data, php="php"):
    return sandbox(work, data, "GET", False, php)


def live_get(base_url, token):
    """本番の mail.php に GET を1回（サーバーのキャッシュを通さない）。中身は出さず、はい/いいえだけ"""
    st, headers, body = web.fetch(web.bust(base_url.rstrip("/") + "/" + TARGET, token), retries=2)
    text = body.decode("utf-8", "replace")
    return {"status": st, "location": headers.get("location", ""), "redirect": "location" in headers,
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


# ---------------------------------------------------------------- 修正の種類（どれも「決まった1か所の決まった変更」だけ）
class BomFix:
    """先頭の BOM（3バイト）を除く（2026-10-12 実施済み）"""
    name = "bom"
    title = "先頭の BOM の除去"
    diff_text = "先頭の3バイト（BOM）の削除だけ（バイト単位で照合・ほかの中身は同じ）"

    def make(self, orig):
        if not orig.startswith(BOM):
            raise Stop("mail.php の先頭に BOM がありません（想定と違うため止めます）")
        fixed = orig[3:]
        if len(orig) - len(fixed) != 3 or orig[:3] != BOM or orig[3:] != fixed or BOM + fixed != orig:
            raise Stop("修正の差分が先頭3バイトの削除だけになっていません")
        if fixed.startswith(BOM):
            raise Stop("BOM が二重に付いています（想定と違うため止めます）")
        if not fixed.startswith(b"<?php"):
            raise Stop("BOM を除いた後の先頭が <?php ではありません")
        return fixed

    def revert(self, fixed):
        if fixed.startswith(BOM):
            raise Stop("すでに先頭に BOM があります")
        return BOM + fixed

    def revert_ok(self, rc, lint_lines):
        """戻した中身が、修正前と同じ状態（2行目の strict_types のエラー）になること"""
        return any(STRICT_ERR in x and x.endswith("on line 2") for x in lint_lines)


class DisplayErrorsFix:
    """declare(strict_types=1); の行の直後に、エラーを画面に出さない1行を足す（2026-10-12 ご本人の承認・案1）"""
    name = "display_errors"
    title = "エラー表示の無効化（ini_set 1行の追加）"
    LINE = b"ini_set('display_errors', '0');\n"
    DECL = re.compile(rb"declare\(strict_types=1\);")
    diff_text = "declare(strict_types=1); の次の行に ini_set('display_errors', '0'); の1行を追加しただけ（ほかの行は同じ）"

    def _at(self, data):
        """足す位置（declare の行の次の行の先頭）。declare は1つだけ・2行目・行末まで何もないこと"""
        if data.startswith(BOM):
            raise Stop("先頭に BOM があります（先に BOM の除去が必要です）")
        if not data.startswith(b"<?php\n"):
            raise Stop("1行目が <?php だけの行ではありません（想定と違うため止めます）")
        found = list(self.DECL.finditer(data))
        if len(found) != 1:
            raise Stop(f"declare(strict_types=1); が {len(found)} か所あります（1か所のはず）")
        m = found[0]
        if data[:m.start()].count(b"\n") + 1 != 2 or data[m.start() - 1:m.start()] != b"\n":
            raise Stop("declare(strict_types=1); が2行目の行頭にありません（想定と違うため止めます）")
        if data[m.end():m.end() + 1] != b"\n":
            raise Stop("declare(strict_types=1); の後ろに同じ行で続きがあります（想定と違うため止めます）")
        return m.end() + 1

    def make(self, orig):
        if b"display_errors" in orig:
            raise Stop("mail.php にはすでに display_errors の指定があります（二重に足さないため止めます）")
        i = self._at(orig)
        fixed = orig[:i] + self.LINE + orig[i:]
        n = len(self.LINE)
        if (len(fixed) - len(orig) != n or fixed[:i] != orig[:i] or fixed[i:i + n] != self.LINE
                or fixed[i + n:] != orig[i:]):
            raise Stop("修正の差分が1行の追加だけになっていません")
        a, b = orig.split(b"\n"), fixed.split(b"\n")
        added = [x for x in difflib.ndiff([x.decode("latin-1") for x in a], [x.decode("latin-1") for x in b])
                 if x[:2] in ("+ ", "- ")]
        if added != ["+ " + self.LINE.decode().rstrip("\n")]:
            raise Stop("行単位の差分が1行の追加だけになっていません")
        return fixed

    def revert(self, fixed):
        i = self._at(fixed)
        n = len(self.LINE)
        if fixed[i:i + n] != self.LINE or fixed.count(b"display_errors") != 1:
            raise Stop("追加した1行が決まった位置にありません（想定と違うため戻しません）")
        return fixed[:i] + fixed[i + n:]

    def revert_ok(self, rc, lint_lines):
        return rc == 0 and lint_lines == ["No syntax errors detected in mail.php"]


FIXES = {f.name: f for f in (BomFix(), DisplayErrorsFix())}


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


def yn(v):
    return "あり" if v else "なし"


# ---------------------------------------------------------------- 本体
def run(mode, confirm, extra, base_url, token, cache_wait=10, fix="display_errors"):
    site = C.SITES[SITE]
    fx = FIXES[fix]
    lines = [f"## fukushin-e.co.jp：mail.php の{fx.title}（モード：{mode}）"]
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
                  f"- mail.php：{len(cur)} バイト・先頭の BOM {yn(cur.startswith(BOM))}"
                  f"・display_errors の指定 {yn(b'display_errors' in cur)}"]
        if left:
            return 1, lines + [f"**止めました（書き込みなし）**：前回の一時ファイルが残っています（{len(left)} 件）"]

        if mode == "restore":
            return _restore(site, fx, cur, extra, token, work, lines, log)

        # 2. 修正後の中身を作り、実行環境の中で検査する
        orig = cur
        try:
            fixed = fx.make(orig)
        except Stop as e:
            return 1, lines + [f"**止めました（書き込みなし）**：{e}"]
        rc0, l0 = lint(work, orig)
        rc1, l1 = lint(work, fixed)
        lines += [f"- 差分：{fx.diff_text}",
                  f"- 構文チェック（修正前）：終了コード {rc0}／" + "／".join(l0),
                  f"- 構文チェック（修正後）：終了コード {rc1}／" + "／".join(l1)]
        if rc1 != 0 or l1 != ["No syntax errors detected in mail.php"]:
            return 1, lines + ["**止めました（書き込みなし）**：修正後の構文チェックが合格しません"]
        backup = work.write("mail.php.orig", orig)      # この実行の中だけのバックアップ
        try:
            back = fx.revert(fixed)
        except Stop as e:
            return 1, lines + [f"**止めました（書き込みなし）**：戻し方の確認に失敗しました（{e}）"]
        if backup.read_bytes() != orig or back != orig:
            return 1, lines + ["**止めました（書き込みなし）**：バックアップの照合に失敗しました"]
        lines.append("- バックアップ：修正前の mail.php をこの実行の中に保持し、読み直して照合・"
                     "修正後から戻した中身（restore と同じ手順）と完全に一致（＝復元できる）")
        sb = sandbox_get(work, fixed)
        safe_get = sb["status"] and not sb["calls_disabled"] and not sb["fatal"]
        lines.append(f"- 模擬サーバーでの GET（メール送信・外部接続は無効）：HTTP {sb['status']}・転送 {yn(sb['redirect'])}"
                     f"・送信/外部接続の関数の呼び出し {yn(sb['calls_disabled'])}・致命的エラー {yn(sb['fatal'])}"
                     f"・警告 {yn(sb['warning'])}")
        if fx.name == "display_errors":
            # エラーの内容・パスがレスポンスに出ないこと：修正前は出る／修正後は出ない、を同じ条件で比べる
            rows, ok = [], True
            for method in ("GET", "POST"):
                b0 = sandbox(work, orig, method, probe=True)
                b1 = sandbox(work, fixed, method, probe=True)
                # 致命的エラーのときの状態コードは、表示の設定で変わる（表示あり＝200／なし＝500）ため、そのときは比べない
                key = lambda b: (b["redirect"], b["calls_disabled"], b["fatal"]) + (() if b["fatal"] else (b["status"],))  # noqa: E731
                same = key(b0) == key(b1)
                rows.append(f"{method}（空の送信）：修正前 HTTP {b0['status']}・エラー表示 {yn(b0['probe_shown'])}"
                            f"／修正後 HTTP {b1['status']}・エラー表示 {yn(b1['probe_shown'] or b1['warning'])}"
                            f"・送信/外部接続の関数の呼び出し {yn(b1['calls_disabled'])}・動きの違い {'なし' if same else 'あり'}"
                            if method == "POST" else
                            f"{method}：修正前 HTTP {b0['status']}・エラー表示 {yn(b0['probe_shown'])}"
                            f"／修正後 HTTP {b1['status']}・エラー表示 {yn(b1['probe_shown'] or b1['warning'])}"
                            f"・動きの違い {'なし' if same else 'あり'}")
                # 修正前は表示される（＝この検査で見分けられる）／修正後は何も表示されない／動き（状態・転送・送信の関数の呼び出し）は同じ
                ok = ok and b0["probe_shown"] and not b1["probe_shown"] and not b1["warning"] and same
                if method == "GET":
                    ok = ok and not b1["fatal"]
            lines.append("- エラーの内容・パスの表示（模擬サーバー・エラーを表示する設定・終了時に必ず警告を1つ出す仕掛け）：" + "／".join(rows))
            if not ok:
                return 1, lines + ["**止めました（書き込みなし）**：修正後もエラーが表示されるか、修正前と動きが変わります"]
        before = live_get(base_url, token + "a") if safe_get else None
        if before:
            lines.append(f"- 公開中の mail.php（修正前・GET）：HTTP {before['status']}・転送 {yn(before['redirect'])}"
                         f"・エラーやパスの表示 {yn(before['fatal'] or before['shows_server_path_or_warning'])}")
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
                return _rollback(g, orig, lines + [f"**書き込みで止まりました**：{why}"], log)
            # 4. 公開後の確認
            if leftovers(g):
                return _rollback(g, orig, lines + ["**一時ファイルが残っています**"], log)
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
            lines.append(f"- 公開中の mail.php（修正後・GET・送信なし）：HTTP {after['status']}・転送 {yn(after['redirect'])}"
                         f"・構文エラー／致命的エラーの表示 {yn(after['fatal'])}"
                         f"・サーバーのパスや警告の表示 {yn(after['shows_server_path_or_warning'])}")
            if not after["status"] or after["status"] >= 500 or after["fatal"] or after["shows_server_path_or_warning"]:
                return _rollback(g, orig, lines + ["**公開後の確認で問題が見つかりました**"], log)
            if before and (before["status"], before["location"]) != (after["status"], after["location"]):
                return _rollback(g, orig, lines + ["**修正の前後で mail.php の応答（状態・転送先）が変わりました**"], log)
            for page in ("/", "/contact.html"):
                st, _, _ = web.fetch(web.bust(base_url.rstrip("/") + page, token + "c"))
                lines.append(f"- {page}：HTTP {st}")
                if st != 200:
                    return _rollback(g, orig, lines + [f"**{page} が表示されません**"], log)
            return 0, lines + [f"**判定**：修正しました（{fx.title}）"]
        finally:
            ftp.close()


def _rollback(g, orig, lines, log):
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
        return 2, lines + [f"**自動の復元に失敗しました**：{type(e).__name__}。restore モードで戻してください"]
    return 1, lines + ["- 自動の復元：" + ("修正前の中身に戻しました（バイト単位で一致）" if ok else "**一致しません**")]


def _restore(site, fx, cur, extra, token, work, lines, log):
    """修正前に戻す。戻した中身が、修正前と同じ状態になることを実行環境の中で確かめてから書く"""
    try:
        orig = fx.revert(cur)
    except Stop as e:
        return 1, lines + [f"**止めました（書き込みなし）**：{e}"]
    rc, l = lint(work, orig)
    if not fx.revert_ok(rc, l):
        return 1, lines + ["**止めました（書き込みなし）**：戻した中身が、修正前と同じ状態になりません"]
    ftp = connect(site, False, extra)
    g = Guarded(ftp)
    try:
        if fetch_twice(g) != cur:
            return 1, lines + ["**止めました（書き込みなし）**：確認の後に mail.php が変わりました"]
        put(g, orig, token, log)
        return 0, lines + [f"**判定**：修正前の中身に戻しました（{fx.title}を取り消し）"]
    except Exception as e:    # noqa: BLE001
        for n in leftovers(g):
            g.delete(n)
        return 1, lines + [f"**止めました**：{e if isinstance(e, (Stop, FtpsError)) else type(e).__name__}"]
    finally:
        ftp.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="deployer.mailfix")
    ap.add_argument("--mode", required=True, choices=["check", "apply", "restore"])
    ap.add_argument("--fix", default="display_errors", choices=sorted(FIXES))
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
    rc, lines = run(a.mode, a.confirm, extra, base_url, secrets.token_hex(6), wait, a.fix)
    text = "\n".join(lines)
    print(text)
    s = os.environ.get("GITHUB_STEP_SUMMARY")
    if s:
        with open(s, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
