# ローカルプレビュー

```
python3 preview/serve.py
```

- 弁当事業サイト http://localhost:8080/
- コーポレート http://localhost:8081/

詳しくは `docs/10_プレビュー確認方法.md`。インターネットには公開されません。

## サーバー用プレビュー（お名前.com）
- `python3 preview/build_package.py` … アップロード用一式（`preview-package/`、Git管理外）。鍵はコントロールパネルの「アクセス制限」（既定）。`--auth htaccess` で .htaccess 方式
- `python3 preview/check_live.py` … 公開中コーポレート（正本）と開発用コピーの差分確認（読み取りのみ）
- `python3 preview/check_preview.py <URL>` … パスワード無しで見られないかの確認（読み取りのみ）

手順は `docs/13_プレビュー_アップロードと停止手順.md`。
