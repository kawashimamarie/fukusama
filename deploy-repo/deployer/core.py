"""公開の本体：接続先の確認 → 計画（差分）→ 事前バックアップ → 公開 → 公開後の検証 → 異常時の自動復元。

原則
- 書き込むのは、ビルドに含まれるファイル（管理対象）と、福記美食の .htaccess の先頭の切り替え設定だけ。
- サーバー上の既存ファイルは削除しない。削除するのは「この実行で新しく作ったファイル」を元に戻すときと、
  自分の一時ファイル・確認用ファイルだけ（どれも名前と中身を確かめてから）。
- 置き換えは「一時ファイル → 中身の照合 → 名前の変更 → もう一度照合」。
- 迷ったら書き込まずに止まる。
"""
import fnmatch
import hashlib
import json
import posixpath
import secrets
import time
import urllib.parse
from pathlib import Path

from . import config as C
from . import web
from .ftps import FtpsError, check_path, parent_dirs


ASSET_EXT = {".css", ".js", ".webp", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff2", ".pdf", ".mp4"}


class Stop(Exception):
    """安全のため書き込まずに止める（または、書き込み後なら自動で元に戻す）"""


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


# ---------------------------------------------------------------- ビルドの検査
def load_build(site, dist):
    """ビルドの全ファイル {path: {"sha256", "size"}}。公開してはいけないものが1つでもあれば止まる"""
    dist = Path(dist)
    if not dist.is_dir():
        raise Stop(f"ビルドがありません：{dist}")
    files, total = {}, 0
    for p in sorted(dist.rglob("*")):
        rel = p.relative_to(dist).as_posix()
        if p.is_symlink():
            raise Stop(f"シンボリックリンクは公開できません：{rel}")
        if p.is_dir():
            continue
        try:
            check_path(rel)
        except FtpsError as e:
            raise Stop(str(e)) from None
        special = rel in site.allowed_special
        if matches(rel, site.protected) and not special:
            raise Stop(f"公開してはいけないファイルがビルドに含まれています：{rel}")
        if not special and Path(rel).suffix.lower() not in C.ALLOWED_EXT:
            raise Stop(f"公開できない種類のファイルです：{rel}")
        if ".deploy-tmp-" in rel or rel.startswith("deploy-probe-"):
            raise Stop(f"公開の仕組みが使う名前のファイルです：{rel}")
        size = p.stat().st_size
        if size > C.MAX_FILE_BYTES:
            raise Stop(f"ファイルが大きすぎます：{rel}（{size:,} バイト）")
        total += size
        files[rel] = {"sha256": sha256(p.read_bytes()), "size": size}
    if "index.html" not in files:
        raise Stop("ビルドにトップページ（index.html）がありません")
    return files


# ---------------------------------------------------------------- 接続先の確認（読み取り）
def _names(ftp, path):
    return sorted(n for n in (posixpath.basename(x.rstrip("/")) for x in ftp.ftp.nlst(path)) if n not in (".", ".."))


def check_destination(ftp, site, base_url, token):
    info = {}
    pwd = ftp.pwd()
    if pwd != "/":
        raise Stop(f"ログイン直後の場所が公開フォルダではありません（{pwd!r}）。接続先ディレクトリを限定した FTP アカウントが必要です")
    root = ftp.listdir("")
    if root is None:
        raise Stop("公開フォルダの一覧を取得できません")
    for name, e in root.items():
        if name == "public_html" or (e["type"] == "dir" and "." in name.strip(".")):
            raise Stop("公開フォルダの中に、ほかのドメインらしいフォルダがあります（接続先が広すぎます）")
        if e["type"] in ("link", "other"):
            raise Stop("公開フォルダの直下に、通常のファイル・フォルダ以外のもの（リンク等）があります")
    names = {n + ("/" if e["type"] == "dir" else "") for n, e in root.items()}
    group = None
    for g in site.root_markers:
        if (not g and not root) or (g and all(m in names for m in g)):
            group = list(g)
            break
    if group is None:
        raise Stop("公開フォルダの中身が、このサイトの目印と一致しません（接続先の誤りの可能性）")
    info["markers"] = group or "空のフォルダ（初回公開）"
    info["empty"] = not root
    # 外へ出られないこと（ルートより上が見えない）
    here = _names(ftp, ".")
    for up in ("..", "../.."):
        try:
            there = _names(ftp, up)
        except Exception:
            continue  # 拒否される＝外へ出られない
        if there != here:
            raise Stop("公開フォルダの外が見えます（接続先ディレクトリが限定されていません）")
    # 公開中のサイトそのものか（FTPS で読んだ中身と公開URLの中身が同じ）
    info["identity"] = None
    for f in site.identity_files:
        data = ftp.get(f)
        if data is None:
            continue
        st, _, body = web.fetch(web.bust(web.path_to_url(base_url, f), token))
        if st != 200 or sha256(body) != sha256(data):
            raise Stop(f"FTPS のファイルと公開URLの内容が一致しません（{f}・HTTP {st}）。接続先が違う可能性があります")
        info["identity"] = f
        break
    if info["identity"] is None and not info["empty"]:
        raise Stop("接続先が公開中のサイトであることを確かめられません（確認用のファイルが見つかりません）")
    # 公開前のトップページ（元に戻したかの確認用）。毎回変わるページ（WordPress など）は状態だけを比べる
    st, _, body = web.fetch(web.bust(base_url.rstrip("/") + "/", token))
    st2, _, body2 = web.fetch(web.bust(base_url.rstrip("/") + "/", token + "b"))
    stable = st == st2 == 200 and body == body2
    info["root_before"] = {"status": st, "sha256": sha256(body) if stable else None}
    return info


# ---------------------------------------------------------------- 計画（読み取り）と事前バックアップ
def plan(ftp, site, dist, build_files, state, backup_dir, token, base_url):
    backup_dir = Path(backup_dir)
    (backup_dir / "files").mkdir(parents=True, exist_ok=True)
    dest = check_destination(ftp, site, base_url, token)

    listings = {}

    def listing(d):
        if d not in listings:
            listings[d] = ftp.listdir(d)
        return listings[d]

    dirs = sorted({d for f in build_files for d in parent_dirs(f)}, key=lambda d: (d.count("/"), d))
    new_dirs = []
    for d in dirs:
        parent, name = posixpath.split(d)
        lst = listing(parent)
        e = (lst or {}).get(name)
        if e is None:
            new_dirs.append(d)
        elif e["type"] != "dir":
            raise Stop(f"フォルダを作る場所に、フォルダ以外のものがあります：{d}")

    items, orphans = [], []
    expected = (state or {}).get("files") or {}
    drift = []
    for f, meta in build_files.items():
        parent, name = posixpath.split(f)
        lst = listing(parent) if parent not in new_dirs else {}
        e = (lst or {}).get(name)
        if e is None:
            items.append({"path": f, "op": "add", "new": meta["sha256"], "size": meta["size"]})
            if f in expected:
                drift.append(f"{f}（公開済みのはずが、サーバーにありません）")
            continue
        if e["type"] != "file":
            raise Stop(f"ファイルを置く場所に、ファイル以外のものがあります：{f}")
        data = ftp.get(f)
        if data is None:
            raise Stop(f"サーバーのファイルを読めません：{f}")
        old = sha256(data)
        if old == meta["sha256"]:
            continue
        (backup_dir / "files" / f).parent.mkdir(parents=True, exist_ok=True)
        (backup_dir / "files" / f).write_bytes(data)
        items.append({"path": f, "op": "replace", "old": old, "new": meta["sha256"], "size": meta["size"]})
        if expected and expected.get(f) != old:
            drift.append(f"{f}（前回の公開の後に、サーバー上で直接変更されています）")
    # 管理していないファイル（報告だけ。削除しない）
    for d, lst in listings.items():
        for name, e in (lst or {}).items():
            p = f"{d}/{name}" if d else name
            if e["type"] == "file" and p not in build_files and not matches(p, site.protected) and ".deploy-tmp-" not in p:
                orphans.append(p)
            if ".deploy-tmp-" in p or name.startswith("deploy-probe-"):
                raise Stop(f"前回の公開の一時ファイルが残っています：{p}（中身を確かめて手で片付けてから再実行）")

    htaccess = None
    if site.htaccess_block:
        cur = ftp.get(".htaccess")
        cur_text = (cur or b"").decode("utf-8")
        if site.htaccess_block in cur_text:
            pass
        elif "リニューアル版への切り替え（ここから）" in cur_text:
            raise Stop(".htaccess に、内容の違う切り替え設定があります（手で確かめてください）")
        else:
            if cur is not None:
                (backup_dir / "htaccess.orig").write_bytes(cur)
            new = (site.htaccess_block + "\n" + cur_text).encode("utf-8")
            htaccess = {"existed": cur is not None, "old": sha256(cur) if cur is not None else None, "new": sha256(new)}
            (backup_dir / "htaccess.new").write_bytes(new)

    changed = [i for i in items]
    total_bytes = sum(i["size"] for i in changed)
    reasons = []
    if not state or not state.get("files"):
        reasons.append("初回公開、または手動の復元の後（このサイトの公開記録がありません）")
    if htaccess:
        reasons.append("福記美食の切り替え（.htaccess の先頭に設定を追加）")
    for i in changed:
        if i["path"] in site.approval_paths:
            reasons.append(f"安全に関わるファイルの変更：{i['path']}")
    if len(changed) > C.MAX_AUTO_FILES or total_bytes > C.MAX_AUTO_BYTES:
        reasons.append(f"変更が多い（{len(changed)} ファイル・{total_bytes:,} バイト）")
    for d in drift:
        reasons.append(f"サーバー上の想定外の変更：{d}")
    form_changed = [i["path"] for i in changed if i["path"] in site.form_paths]

    p = {
        "schema": 1, "site": site.id, "domain": site.domain, "token": token,
        "destination": dest, "new_dirs": new_dirs, "items": items, "htaccess": htaccess,
        "orphans": sorted(orphans), "approval_reasons": reasons, "form_changed": form_changed,
        "counts": {"add": sum(i["op"] == "add" for i in items), "replace": sum(i["op"] == "replace" for i in items),
                   "unchanged": len(build_files) - len(items), "bytes": total_bytes},
        "build_files": build_files,
        "backup": {"files": {i["path"]: i["old"] for i in items if i["op"] == "replace"},
                   "htaccess": htaccess["old"] if htaccess and htaccess["existed"] else None},
    }
    p["has_changes"] = bool(items or htaccess)
    (backup_dir / "plan.json").write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def verify_backup(plan_, backup_dir, site=None):
    """保存先から取り出したバックアップが、計画と同じ中身でそろっていること。
    site を渡すと、計画の中のパスが公開できる範囲（保護対象でない）だけであることも確かめる"""
    b = Path(backup_dir)
    if site is not None:
        if plan_.get("site") != site.id:
            raise Stop("別のサイトのバックアップです")
        for i in plan_["items"]:
            check_path(i["path"])
            if matches(i["path"], site.protected) and i["path"] not in site.allowed_special:
                raise Stop(f"バックアップの計画に、公開できない場所が含まれています：{i['path']}")
        if plan_["htaccess"] and not site.htaccess_block:
            raise Stop("このサイトのバックアップに .htaccess の切り替えは含まれないはずです")
    for path, h in plan_["backup"]["files"].items():
        f = b / "files" / path
        if not f.is_file() or sha256(f.read_bytes()) != h:
            raise Stop(f"バックアップが壊れているか足りません：{path}")
    if plan_["backup"]["htaccess"]:
        f = b / "htaccess.orig"
        if not f.is_file() or sha256(f.read_bytes()) != plan_["backup"]["htaccess"]:
            raise Stop("バックアップの .htaccess が壊れています")
    if plan_["htaccess"]:
        f = b / "htaccess.new"
        if not f.is_file() or sha256(f.read_bytes()) != plan_["htaccess"]["new"]:
            raise Stop("切り替え用の .htaccess が壊れています")


# ---------------------------------------------------------------- 書き込み
def put_verified(ftp, path, data, token):
    """一時ファイル → 照合 → 名前の変更 → 照合"""
    h = sha256(data)
    tmp = f"{path}.deploy-tmp-{token}"
    ftp.put_new(tmp, data)
    got = ftp.sha256(tmp)
    if got != h:
        ftp.delete(tmp)
        raise Stop(f"送ったファイルの中身が一致しません：{path}")
    ftp.rename(tmp, path)
    if ftp.sha256(path) != h:
        raise Stop(f"置き換え後の中身が一致しません：{path}")


def _order(item):
    p = item["path"]
    if p == "index.html":
        return (5, p)
    if p == ".htaccess":
        return (3, p)
    if p.endswith(".html"):
        return (2, p)
    if p.endswith(".php"):
        return (1, p)
    return (0, p)


def recheck(ftp, plan_):
    """書き込む直前に、サーバーが計画を作ったときのままかを確かめる（変わっていれば何も書かない）"""
    for i in plan_["items"]:
        cur = ftp.sha256(i["path"])
        want = i["old"] if i["op"] == "replace" else None
        if cur != want:
            raise Stop(f"計画の後にサーバーのファイルが変わりました：{i['path']}")
    if plan_["htaccess"]:
        if ftp.sha256(".htaccess") != plan_["htaccess"]["old"]:
            raise Stop("計画の後に .htaccess が変わりました")
    for d in plan_["new_dirs"]:
        parent, name = posixpath.split(d)
        if name in (ftp.listdir(parent) or {}):
            raise Stop(f"計画の後にフォルダができています：{d}")


def probe(ftp, site, base_url, token):
    """接続先が公開URLのサイトであることを、確認用のファイルを置いて確かめる（すぐに削除）"""
    name = f"deploy-probe-{token}.txt"
    body = f"fukushin-deploy {site.id} {secrets.token_hex(16)}\n".encode()
    ftp.put_new(name, body)
    try:
        st, _, got = web.fetch(web.bust(web.path_to_url(base_url, name), token))
        if st != 200 or got != body:
            raise Stop(f"確認用のファイルが公開URLで見えません（HTTP {st}）。接続先が違う可能性があるため止めます")
    finally:
        if ftp.sha256(name) == sha256(body):
            ftp.delete(name)


def apply(ftp, site, plan_, dist, backup_dir, base_url, log):
    """公開する。途中で失敗したら、ここまでの書き込みを元に戻して Stop を投げ直す。
    戻り値：done（行った操作の記録）"""
    dist, b = Path(dist), Path(backup_dir)
    token = plan_["token"]
    recheck(ftp, plan_)
    probe(ftp, site, base_url, token)
    done = []
    try:
        for d in plan_["new_dirs"]:
            ftp.mkdir(d)
            done.append({"op": "mkdir", "path": d})
        items = sorted(plan_["items"], key=_order)
        before_index = [i for i in items if i["path"] != "index.html"]
        last = [i for i in items if i["path"] == "index.html"]
        for i in before_index:
            put_verified(ftp, i["path"], (dist / i["path"]).read_bytes(), token)
            done.append(i)
            log(f"{'追加' if i['op'] == 'add' else '置き換え'}：{i['path']}")
        if plan_["htaccess"]:
            put_verified(ftp, ".htaccess", (b / "htaccess.new").read_bytes(), token)
            done.append({"op": "htaccess", **plan_["htaccess"]})
            log("切り替え設定：.htaccess の先頭に追加")
        for i in last:
            put_verified(ftp, i["path"], (dist / i["path"]).read_bytes(), token)
            done.append(i)
            log(f"{'追加' if i['op'] == 'add' else '置き換え'}：{i['path']}（最後）")
    except Exception as e:
        raise WriteFailed(str(e), done) from e
    return done


class WriteFailed(Exception):
    def __init__(self, msg, done):
        super().__init__(msg)
        self.done = done


# ---------------------------------------------------------------- 公開後の検証（読み取り）
def verify_live(site, plan_, dist, base_url, token, *, cache_wait=15):
    dist = Path(dist)
    errors, warnings = [], []
    changed = [i["path"] for i in plan_["items"]]

    def want(path):
        return plan_["build_files"].get(path, {}).get("sha256")

    def check_file(path, url):
        st, _, body = web.fetch(web.bust(url, token))
        if st != 200:
            errors.append(f"HTTP {st}：{url}")
        elif sha256(body) != want(path):
            errors.append(f"内容がビルドと違う：{url}")
        return body

    for p in changed:
        if p.endswith(".php") or p.startswith(".") or "/." in p:
            continue
        check_file(p, web.path_to_url(base_url, p))
        if p.endswith("/index.html") or p == "index.html":
            check_file(p, base_url.rstrip("/") + "/" + p[: -len("index.html")])
    pages_html = {}
    for pg in site.pages:
        f = web.page_file(pg)
        url = base_url.rstrip("/") + pg
        if want(f):
            pages_html[pg] = check_file(f, url)
        else:
            st, _, _ = web.fetch(web.bust(url, token))
            if st != 200:
                errors.append(f"HTTP {st}：{url}")
    # ページの中の参照（CSS・JS・画像・同じサイトへのリンク）
    to_scan = {pg: h for pg, h in pages_html.items() if h and (web.page_file(pg) in changed or pg == "/")}
    refs = set()
    for pg, h in to_scan.items():
        refs.update(web.same_site_refs(h, base_url.rstrip("/") + pg, base_url))
    base_path = urllib.parse.urlparse(base_url).path.rstrip("/")
    for u in sorted(refs)[:400]:
        st, hd, body = web.fetch(web.bust(u, token), method="GET")
        if st != 200:
            errors.append(f"参照先が開けない（HTTP {st}）：{u}")
            continue
        path = urllib.parse.unquote(urllib.parse.urlparse(u).path)[len(base_path):]
        f = web.page_file(path)
        if want(f):
            # ビルドにあるファイルは中身まで確かめる（WordPress は、ないファイルにも 200 でページを返すため）
            if sha256(body) != want(f):
                errors.append(f"参照先の内容がビルドと違う：{u}")
        elif Path(path).suffix.lower() in ASSET_EXT and "text/html" in hd.get("content-type", ""):
            errors.append(f"参照先が画像・CSS・JS ではなくページを返す（ファイルがない可能性）：{u}")
    for h in site.hidden:
        st, _, body = web.fetch(web.bust(base_url.rstrip("/") + h, token))
        if not (st in (403, 404) or (st == 200 and not body.strip())):
            errors.append(f"見えてはいけないURLが見える（HTTP {st}）：{h}")
    for path, status, loc in site.redirects:
        st, hd, _ = web.fetch(base_url.rstrip("/") + path)
        got = hd.get("location", "")
        if st != status or not (got == loc or got.endswith("://" + base_url.split("://")[1].rstrip("/") + loc)):
            errors.append(f"転送が違う：{path} → HTTP {st} {got or '-'}（期待：{status} {loc}）")
    for pr in site.probes:
        st, _, _ = web.fetch(base_url.rstrip("/") + pr.path, method=pr.method, headers=pr.headers, body=pr.body or None)
        if st not in pr.expect_status:
            errors.append(f"{pr.note or pr.path}：HTTP {st}（期待：{pr.expect_status}）")
    if errors:
        return errors, warnings
    # サーバーキャッシュ（nginx）に古いページが残っていないか（残っていても元に戻す理由にはしない）
    stale = []
    for p in [x for x in changed if x.endswith(".html")][:30]:
        url = base_url.rstrip("/") + "/" + (p[: -len("index.html")] if p.endswith("index.html") else p)
        for k in range(4):
            st, _, body = web.fetch(url)
            if st == 200 and sha256(body) == want(p):
                break
            if k < 3:
                time.sleep(cache_wait)
        else:
            stale.append(url)
    if stale:
        warnings.append("サーバーキャッシュに古いページが残っています（コントロールパネルで「キャッシュの削除」）：" + "、".join(stale[:5]))
    return errors, warnings


# ---------------------------------------------------------------- 元に戻す
def rollback(ftp, plan_, done, backup_dir, base_url, log):
    """done を逆順に戻す。戻せなかったものの一覧を返す（空なら成功）"""
    b = Path(backup_dir)
    token = plan_["token"] + "r"
    problems = []
    for d in reversed(done):
        try:
            if d["op"] == "replace":
                cur = ftp.sha256(d["path"])
                if cur == d["old"]:
                    continue
                if cur not in (d["new"], None) and cur != d["old"]:
                    problems.append(f"{d['path']}：想定外の内容のため戻していません")
                    continue
                put_verified(ftp, d["path"], (b / "files" / d["path"]).read_bytes(), token)
                log(f"元に戻した：{d['path']}")
            elif d["op"] == "add":
                cur = ftp.sha256(d["path"])
                if cur is None:
                    continue
                if cur != d["new"]:
                    problems.append(f"{d['path']}：想定外の内容のため削除していません")
                    continue
                ftp.delete(d["path"])
                log(f"追加したファイルを削除：{d['path']}")
            elif d["op"] == "htaccess":
                cur = ftp.sha256(".htaccess")
                if cur == d["old"]:
                    continue
                if cur != d["new"]:
                    problems.append(".htaccess：想定外の内容のため戻していません")
                    continue
                if d["existed"]:
                    put_verified(ftp, ".htaccess", (b / "htaccess.orig").read_bytes(), token)
                else:
                    ftp.delete(".htaccess")
                log("元に戻した：.htaccess")
            elif d["op"] == "mkdir":
                pass  # 作ったフォルダは残す（空。表示には影響しない）
        except Exception as e:  # 1件の失敗で止めず、ほかも戻す
            problems.append(f"{d.get('path', '.htaccess')}：{type(e).__name__} {e}")
    # 戻したことの確認（FTPS）
    for d in done:
        try:
            if d["op"] == "replace" and ftp.sha256(d["path"]) != d["old"]:
                problems.append(f"{d['path']}：公開前の内容に戻っていません")
            elif d["op"] == "add" and ftp.sha256(d["path"]) is not None:
                problems.append(f"{d['path']}：追加したファイルが残っています")
            elif d["op"] == "htaccess" and ftp.sha256(".htaccess") != d["old"]:
                problems.append(".htaccess：公開前の内容に戻っていません")
        except Exception as e:
            problems.append(f"確認できません：{type(e).__name__}")
    # 公開URLでの確認（トップページが公開前と同じ状態か・置き換えたファイルが元の中身か）
    rb = plan_["destination"].get("root_before") or {}
    st, _, body = web.fetch(web.bust(base_url.rstrip("/") + "/", token))
    if st != rb.get("status") or (st == 200 and rb.get("sha256") and sha256(body) != rb.get("sha256")):
        problems.append(f"トップページが公開前と違います（HTTP {st}）")
    for d in done:
        if d["op"] == "replace" and not d["path"].endswith(".php") and not d["path"].startswith("."):
            st, _, body = web.fetch(web.bust(web.path_to_url(base_url, d["path"]), token))
            if st != 200 or sha256(body) != d["old"]:
                problems.append(f"公開URLが元に戻っていません：{d['path']}")
    return problems


def done_from_plan(plan_):
    """手動の復元用：その公開で行うはずだった操作の一覧"""
    done = [{"op": "mkdir", "path": d} for d in plan_["new_dirs"]]
    items = sorted(plan_["items"], key=_order)
    done += [i for i in items if i["path"] != "index.html"]
    if plan_["htaccess"]:
        done.append({"op": "htaccess", **plan_["htaccess"]})
    done += [i for i in items if i["path"] == "index.html"]
    return done


def check_restorable(ftp, plan_):
    """手動の復元の前に：サーバーが「その公開の後の状態」か「すでに戻った状態」であること"""
    for i in plan_["items"]:
        cur = ftp.sha256(i["path"])
        ok = {i["new"], i["old"]} if i["op"] == "replace" else {i["new"], None}
        if cur not in ok:
            raise Stop(f"その公開の後に、別の変更があります：{i['path']}（新しい公開を先に戻すか、手で確かめてください）")
    if plan_["htaccess"] and ftp.sha256(".htaccess") not in (plan_["htaccess"]["new"], plan_["htaccess"]["old"]):
        raise Stop(".htaccess がその公開の後に変わっています")
