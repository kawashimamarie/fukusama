"""明示的 FTPS（AUTH TLS・証明書とホスト名の検証あり・TLS 1.2 以上）の最小限のクライアント。

使う命令：PWD・MLSD/LIST・RETR・STOR・RNFR/RNTO・DELE・MKD だけ。
フォルダの削除（RMD）・権限の変更（SITE）・追記（APPE）は使わない（メソッドを用意していない）。
読み取り専用の接続（readonly=True）では、書き込む命令を呼ぶと例外で止まる。
パスワードは接続にだけ使い、画面・ログ・ファイルに出さない。
"""
import ftplib
import hashlib
import io
import posixpath
import re
import ssl

SAFE_PATH = re.compile(r"^[A-Za-z0-9._\-/]+$")


class FtpsError(Exception):
    pass


def _close_data(conn):
    """データ接続を閉じる。空の一覧などで、サーバーが TLS の終了手順の前に切断しても失敗にしない
    （転送の成否は制御接続の応答 226 で判定し、ファイルの中身は取得後に SHA-256 で照合する）"""
    try:
        if isinstance(conn, ssl.SSLSocket):
            conn.unwrap()
    except (ssl.SSLError, OSError):
        pass
    finally:
        conn.close()


class _TLS(ftplib.FTP_TLS):
    """データ接続で制御接続の TLS セッションを再利用する（多くの FTP サーバーが要求する）"""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def retrlines(self, cmd, callback=None):
        callback = callback or print
        self.sendcmd("TYPE A")
        conn = self.transfercmd(cmd)
        try:
            with conn.makefile("r", encoding=self.encoding) as fp:
                while True:
                    line = fp.readline(self.maxline + 1)
                    if len(line) > self.maxline:
                        raise ftplib.Error("got more than %d bytes" % self.maxline)
                    if not line:
                        break
                    callback(line.rstrip("\r\n"))
        except ssl.SSLError as e:
            if "SHUTDOWN" not in str(e).upper() and "EOF" not in str(e).upper():
                raise
        finally:
            _close_data(conn)
        return self.voidresp()

    def retrbinary(self, cmd, callback, blocksize=8192, rest=None):
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                data = conn.recv(blocksize)
                if not data:
                    break
                callback(data)
        except ssl.SSLError as e:
            if "SHUTDOWN" not in str(e).upper() and "EOF" not in str(e).upper():
                raise
        finally:
            _close_data(conn)
        return self.voidresp()

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
        finally:
            _close_data(conn)
        return self.voidresp()


def check_path(path):
    """サイトの中の相対パスだけを受け付ける（.. ・絶対パス・空白・制御文字を拒否）"""
    if not path or path.startswith("/") or not SAFE_PATH.match(path):
        raise FtpsError(f"使えないパスです：{path!r}")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise FtpsError(f"使えないパスです：{path!r}")
    return path


class Ftps:
    def __init__(self, host, user, password, *, readonly=True, port=21, timeout=40, cafile=None):
        if not host or not user or not password:
            raise FtpsError("FTPS の接続情報（HOST・USER・PASSWORD）が設定されていません")
        self.readonly = readonly
        ctx = ssl.create_default_context(cafile=cafile)  # 証明書とホスト名を必ず検証する
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ftp = _TLS(context=ctx, timeout=timeout)
        try:
            self.ftp.connect(host, port)
            self.ftp.auth()                   # 認証の前に TLS へ（検証できなければここで止まり、パスワードは送らない）
            self.ftp.login(user, password)
            self.ftp.prot_p()                 # データ接続も暗号化
            self.ftp.set_pasv(True)
        except ssl.SSLError as e:
            raise FtpsError(f"TLS の接続またはサーバー証明書の検証に失敗しました（{e.reason or type(e).__name__}）") from None
        except ftplib.error_perm as e:
            raise FtpsError(f"ログインできませんでした（{str(e)[:3]}）") from None
        except OSError as e:
            raise FtpsError(f"FTPS サーバーに接続できませんでした（{type(e).__name__}）") from None
        self.mlsd_ok = True

    # ---------- 読み取り ----------
    def pwd(self):
        return self.ftp.pwd()

    def listdir(self, path=""):
        """{名前: {"type": file|dir|link|other, "size": int}}。フォルダがなければ None"""
        if path:
            check_path(path)
        if self.mlsd_ok:
            try:
                out = {}
                for name, facts in self.ftp.mlsd(path or ".", facts=["type", "size"]):
                    t = facts.get("type", "")
                    if t in ("cdir", "pdir") or name in (".", ".."):
                        continue
                    kind = "dir" if t == "dir" else "file" if t == "file" else "link" if "slink" in t or t == "OS.unix=symlink" else "other"
                    out[name] = {"type": kind, "size": int(facts.get("size", 0) or 0)}
                return out
            except ftplib.error_perm as e:
                if str(e).startswith("550"):
                    return None
                if str(e)[:3] in ("500", "501", "502", "504"):
                    self.mlsd_ok = False
                else:
                    raise
        lines = []
        try:
            self.ftp.retrlines("LIST -a " + (path or "."), lines.append)
        except ftplib.error_perm as e:
            if str(e).startswith("550"):
                return None
            raise
        out = {}
        for ln in lines:
            m = re.match(r"^([\-dlbcps])\S{9}\S*\s+\S+\s+\S+\s+\S+\s+(\d+)\s+\S+\s+\S+\s+\S+\s+(.+)$", ln)
            if not m:
                if ln.startswith("total"):
                    continue
                raise FtpsError("一覧の行を読めません（安全のため止めます）")
            kind = {"-": "file", "d": "dir", "l": "link"}.get(m.group(1), "other")
            name = m.group(3).split(" -> ")[0] if kind == "link" else m.group(3)
            if name in (".", ".."):
                continue
            out[name] = {"type": kind, "size": int(m.group(2))}
        return out

    def get(self, path):
        """ファイルの中身（bytes）。なければ None"""
        check_path(path)
        buf = io.BytesIO()
        try:
            self.ftp.retrbinary("RETR " + path, buf.write)
        except ftplib.error_perm as e:
            if str(e).startswith("550"):
                return None
            raise
        return buf.getvalue()

    def sha256(self, path):
        data = self.get(path)
        return None if data is None else hashlib.sha256(data).hexdigest()

    # ---------- 書き込み（readonly=False のときだけ） ----------
    def _w(self):
        if self.readonly:
            raise FtpsError("読み取り専用の接続で書き込もうとしました（安全のため止めます）")

    def put_new(self, path, data):
        """新しい名前のファイルとして送る（同じ名前があれば送らない）"""
        self._w()
        check_path(path)
        if self.get(path) is not None:
            raise FtpsError(f"同じ名前のファイルがすでにあります：{path}")
        self.ftp.storbinary("STOR " + path, io.BytesIO(data))

    def rename(self, src, dst):
        self._w()
        check_path(src)
        check_path(dst)
        self.ftp.rename(src, dst)

    def delete(self, path):
        self._w()
        check_path(path)
        self.ftp.delete(path)

    def mkdir(self, path):
        self._w()
        check_path(path)
        self.ftp.mkd(path)

    def close(self):
        try:
            self.ftp.quit()
        except Exception:
            try:
                self.ftp.close()
            except Exception:
                pass


def parent_dirs(path):
    """'a/b/c.html' -> ['a', 'a/b']"""
    parts = posixpath.dirname(path).split("/") if "/" in path else []
    return ["/".join(parts[:i + 1]) for i in range(len(parts))]
