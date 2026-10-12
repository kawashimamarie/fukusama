#!/usr/bin/env python3
"""本物の Apache（mod_rewrite・mod_php・.htaccess）を使った公開・復元のリハーサル（手元用。CI では動かさない）。

  sudo なしの root 環境で：python3 tests/deploy/apache_rehearsal.py
必要：apache2（rewrite・php8.x・headers・expires）、pyftpdlib、pyOpenSSL、openssl

確かめること
1. 福記美食：WordPress（実際と同じ形の .htaccess）のフォルダへ初回公開 → 新しいトップ・旧URLの 301・WordPress のファイルは不変
   → 手動の復元で WordPress の状態に完全に戻る（トップが WordPress に戻る）
2. 福記美食：公開後の検証で不合格 → 自動で WordPress に戻る
3. ebisenka.jp：空のフォルダへ初回公開 → .htaccess（config.php の拒否・404 ページ）・send.php（他サイトからの送信を 403）が本物の Apache・PHP で動く
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy-repo"))
sys.path.insert(0, str(Path(__file__).parent))
import harness as H  # noqa: E402
from test_deployer import env  # noqa: E402
from deployer import cli  # noqa: E402

MODS = "/usr/lib/apache2/modules"
WP_HTACCESS = """# BEGIN WordPress
<IfModule mod_rewrite.c>
RewriteEngine On
RewriteBase /
RewriteRule ^index\\.php$ - [L]
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME} !-d
RewriteRule . /index.php [L]
</IfModule>
# END WordPress
"""


def apache(docroot, port, workdir):
    php = sorted(Path(MODS).glob("libphp*.so"))[-1]
    conf = workdir / "httpd.conf"
    conf.write_text(f"""
ServerRoot /etc/apache2
Listen 127.0.0.1:{port}
ServerName localhost
User www-data
Group www-data
PidFile {workdir}/httpd.pid
ErrorLog {workdir}/error.log
LoadModule mpm_prefork_module {MODS}/mod_mpm_prefork.so
LoadModule authz_core_module {MODS}/mod_authz_core.so
LoadModule authz_host_module {MODS}/mod_authz_host.so
LoadModule dir_module {MODS}/mod_dir.so
LoadModule mime_module {MODS}/mod_mime.so
LoadModule rewrite_module {MODS}/mod_rewrite.so
LoadModule headers_module {MODS}/mod_headers.so
LoadModule expires_module {MODS}/mod_expires.so
LoadModule alias_module {MODS}/mod_alias.so
LoadModule php_module {php}
TypesConfig /etc/mime.types
DocumentRoot {docroot}
DirectoryIndex index.html index.php
<Directory {docroot}>
  AllowOverride All
  Options -Indexes
  Require all granted
</Directory>
<FilesMatch "^\\.ht">
  Require all denied
</FilesMatch>
<FilesMatch "\\.php$">
  SetHandler application/x-httpd-php
</FilesMatch>
""")
    subprocess.run(["apache2", "-f", str(conf), "-k", "start"], check=True)
    time.sleep(1)
    return conf


def wp(root):
    files = {"index.php": "<?php echo '<html>WordPress ' . bin2hex(random_bytes(6)) . '</html>';",
             "wp-config.php": "<?php // secret", ".htaccess": WP_HTACCESS,
             "wp-includes/js/wp-emoji-loader.min.js": "/* emoji */", "wp-admin/index.php": "<?php echo 'admin';",
             "wp-content/uploads/2026/10/photo.jpg": "JPEGDATA"}
    for f, body in files.items():
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def run(site, root, port, ftp, dist, mode, confirm, work, auto=""):
    p = {"fukuki": "FTPS_FUKUKI", "ebisenka": "FTPS_EBISENKA"}[site]
    args = ["--test-cafile", str(CA / "ca.pem"), "--test-port", str(ftp.port), "--test-base-url", f"http://localhost:{port}"]
    with env(**{p + "_HOST": "localhost", p + "_USER": ftp.user, p + "_PASSWORD": ftp.password, "DEPLOYER_TEST_MODE": "1",
                "GITHUB_ACTIONS": None, "GITHUB_STEP_SUMMARY": None, "GITHUB_OUTPUT": None}):
        cli.main(["prepare", "--site", site, "--dist", str(dist), "--work", str(work), "--mode", mode, "--confirm", confirm,
                  "--auto-sites", auto, "--token", work.name] + args)
        d = json.loads((work / "decision.json").read_text())
        if d["decision"] != "apply":
            return d, None
        vb = work.parent / (work.name + "-verified")
        shutil.copytree(work / "backup", vb)
        cli.main(["apply", "--site", site, "--dist", str(dist), "--work", str(work), "--backup", str(vb), "--sha", "r" * 40,
                  "--cache-wait", "0"] + args)
        return d, json.loads((work / "result.json").read_text())


def restore(site, ftp, port, backup, confirm):
    p = {"fukuki": "FTPS_FUKUKI", "ebisenka": "FTPS_EBISENKA"}[site]
    with env(**{p + "_HOST": "localhost", p + "_USER": ftp.user, p + "_PASSWORD": ftp.password, "DEPLOYER_TEST_MODE": "1",
                "GITHUB_ACTIONS": None}):
        return cli.main(["restore", "--site", site, "--backup", str(backup), "--confirm", confirm, "--test-cafile",
                         str(CA / "ca.pem"), "--test-port", str(ftp.port), "--test-base-url", f"http://localhost:{port}"])


def curl(port, path, *extra):
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{redirect_url}", *extra,
                        f"http://localhost:{port}{path}"], capture_output=True, text=True)
    return r.stdout


CA = None


def main():
    global CA
    build = H.tmpdir() / "out"
    subprocess.run([sys.executable, str(ROOT / "scripts/build_sites.py"), str(build)], check=True, capture_output=True)
    CA = H.make_ca(H.tmpdir())
    ok = True

    # 1・2 福記美食
    for scenario in ("switch-and-restore", "fail-and-rollback"):
        root = Path(f"/var/www/rehearsal-fukuki-{scenario}")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        wp(root)
        before = H.tree(root)
        work = H.tmpdir()
        port = 18080 if scenario == "switch-and-restore" else 18081
        conf = apache(root, port, work)
        ftp = H.FtpsServer(root, CA)
        try:
            print(f"\n=== 福記美食：{scenario}")
            print("切り替え前  / :", curl(port, "/"), "  /party/ :", curl(port, "/party/"))
            dist = build / "ebisenka-seavans.com"
            if scenario == "fail-and-rollback":
                # 公開後の検証で必ず不合格になるよう、ビルドの1ページを壊す（公開URLの内容がビルドと違うのではなく、参照切れ）
                dist = work / "dist"
                shutil.copytree(build / "ebisenka-seavans.com", dist)
                (dist / "kitchen/index.html").write_text((dist / "kitchen/index.html").read_text().replace(
                    "</body>", '<img src="/assets/img/does-not-exist.webp" alt=""></body>'))
            d, res = run("fukuki", root, port, ftp, dist, "publish", "ebisenka-seavans.com", work / "w1")
            print("結果：", res["status"], res.get("reason", ""))
            print("切り替え後  / :", curl(port, "/"), "  /party/ :", curl(port, "/party/"), "  /lunch/ :", curl(port, "/lunch/"),
                  "  /wp-admin/ :", curl(port, "/wp-admin/"))
            after = H.tree(root)
            wp_same = all(after.get(k) == v for k, v in before.items() if k != ".htaccess")
            print("WordPress のファイル（.htaccess 以外）は不変：", wp_same)
            if scenario == "switch-and-restore":
                ok &= res["status"] == "published" and wp_same and curl(port, "/party/").startswith("301")
                rc = restore("fukuki", ftp, port, work / "w1-verified", "ebisenka-seavans.com")
                print("手動の復元：", rc, "  / :", curl(port, "/"), "  /party/ :", curl(port, "/party/"))
                ok &= rc == 0 and H.tree(root) == before
                print("復元後のフォルダ＝公開前と完全に同じ：", H.tree(root) == before)
            else:
                ok &= res["status"] == "rolled-back" and H.tree(root) == before
                print("自動で元に戻った（フォルダ＝公開前と完全に同じ）：", H.tree(root) == before)
        finally:
            ftp.stop()
            subprocess.run(["apache2", "-f", str(conf), "-k", "stop"])
            time.sleep(1)

    # 3 ebisenka.jp
    root = Path("/var/www/rehearsal-ebisenka")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    work = H.tmpdir()
    conf = apache(root, 18082, work)
    ftp = H.FtpsServer(root, CA)
    try:
        print("\n=== ebisenka.jp：空のフォルダへの初回公開")
        print("公開前  / :", curl(18082, "/"))
        d, res = run("ebisenka", root, 18082, ftp, build / "ebisenka.jp", "publish", "ebisenka.jp", work / "w1")
        print("結果：", res["status"], res.get("reason", ""), res.get("warnings", ""))
        print("公開後  / :", curl(18082, "/"), " config.php :", curl(18082, "/contact/config.php"),
              " .htaccess :", curl(18082, "/.htaccess"), " 存在しないページ :", curl(18082, "/nope/"),
              " www 付き :", curl(18082, "/", "-H", "Host: www.ebisenka.jp"))
        ok &= res["status"] == "published"
    finally:
        ftp.stop()
        subprocess.run(["apache2", "-f", str(conf), "-k", "stop"])
    for p in Path("/var/www").glob("rehearsal-*"):
        shutil.rmtree(p, ignore_errors=True)
    print("\nリハーサル：", "合格" if ok else "【不合格】")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
