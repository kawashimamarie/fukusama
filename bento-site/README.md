# ebisenka.jp — 弁当事業サイト

- `src/` … 編集用ソース（ページ本文 `src/pages/`、共通ヘッダー・フッター `src/layout.html`、CSS/JS/画像 `src/assets/`、そのまま公開するファイル `src/static/`）
- `dist/` … 公開用ファイル一式（`python3 build.py` で生成。サーバーにはこの中身をアップロード）

## 更新手順
1. `src/pages/` のHTMLを編集（各ファイル先頭の `<!--meta {...} -->` がタイトル・説明文・パンくず）
2. `python3 build.py` を実行（Python 3.8 以上、追加ライブラリ不要）
3. `dist/` の中身をアップロード

公開手順・確認事項はリポジトリ直下の `docs/` を参照。
