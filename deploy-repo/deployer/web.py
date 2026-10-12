"""公開URLの確認（読み取りのみ）。転送は追わない・一時的な不調は数回まで取り直す。"""
import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "fukushin-deploy-verify/1"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def fetch(url, *, method="GET", headers=(), body=None, retries=3, timeout=20):
    """(status, headers(dict), body bytes)。接続できなければ (0, {}, b'')"""
    last = None
    for i in range(retries):
        req = urllib.request.Request(url, method=method, data=body.encode() if isinstance(body, str) else body,
                                     headers={"User-Agent": UA, "Cache-Control": "no-cache", **dict(headers)})
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with _opener.open(req, timeout=timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            if e.code >= 500 and i + 1 < retries:
                last = e
                time.sleep(3 * (i + 1))
                continue
            return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() if e.fp else b""
        except (urllib.error.URLError, OSError) as e:
            last = e
            time.sleep(3 * (i + 1))
    return 0, {"error": type(last).__name__ if last else "unknown"}, b""


def bust(url, token):
    """サーバーキャッシュ（nginx）を通さずに元の内容を読むための目印を付ける"""
    return url + ("&" if "?" in url else "?") + "deploy-check=" + token


def sha(b):
    return hashlib.sha256(b).hexdigest()


def path_to_url(base_url, path):
    """'bento/index.html' -> base/bento/index.html（そのまま。ディレクトリ形式は pages で別に確かめる）"""
    return base_url.rstrip("/") + "/" + urllib.parse.quote(path)


def page_file(path):
    """'/bento/' -> 'bento/index.html'、'/' -> 'index.html'、'/a.html' -> 'a.html'"""
    p = path.lstrip("/")
    if p == "" or p.endswith("/"):
        return p + "index.html"
    return p


REF = re.compile(r"""(?:src|href)\s*=\s*["']([^"'#]+)["']|srcset\s*=\s*["']([^"']+)["']""", re.I)


def same_site_refs(html_bytes, page_url, base_url):
    """ページの中の、同じサイトへのリンク・画像・CSS・JS の参照（重複なし）"""
    text = html_bytes.decode("utf-8", "replace")
    host = urllib.parse.urlparse(base_url).netloc
    out = set()
    for m in REF.finditer(text):
        vals = [m.group(1)] if m.group(1) else [s.strip().split(" ")[0] for s in m.group(2).split(",")]
        for v in vals:
            v = v.strip()
            if not v or v.startswith(("mailto:", "tel:", "javascript:", "data:")):
                continue
            u = urllib.parse.urljoin(page_url, v)
            pu = urllib.parse.urlparse(u)
            if pu.scheme in ("http", "https") and pu.netloc == host:
                out.add(urllib.parse.urlunparse(pu._replace(query="", fragment="")))
    return sorted(out)
