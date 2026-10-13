# 19. SEO の初期設定と Google Search Console（2026-10-13）

## 1. 調査（公開URL・DNS・Google の検索結果を読むだけ）
| 項目 | fukushin-e.co.jp（コーポレート） | ebisenka.jp | ebisenka-seavans.com（福記美食） |
|---|---|---|---|
| HTTPS | http → https 301 | 同左 | 同左 |
| www | `www.` も 200（同じ中身）。**canonical は全ページ `https://www.fukushin-e.co.jp/…`**（移行前から） | `www.` → `https://ebisenka.jp/` に 301 | `www.` も 200。canonical は `www` なし |
| canonical・noindex | 全ページ自己参照（www）。thanks.html は noindex | 全ページ自己参照。送信完了ページは noindex | 全ページ自己参照 |
| robots.txt | **なし（404）** | あり（送信処理・完了ページを除外・sitemap 記載） | あり（すべて許可・sitemap 記載） |
| sitemap.xml | **なし（404）** | 7件・すべて 200・canonical と一致 | 6件・すべて 200・旧 WordPress の URL なし |
| Google の検索結果（site: 検索・DataForSEO 2026-10-13） | 7件（ほぼ www。新しいページ） | 1件（旧サイトの `http://www.ebisenka.jp/Jiman.htm`・現在 404） | 10件（旧 WordPress のページ。すべて新しいページへ 301 転送済み） |
| Search Console の確認用の記録 | DNS の TXT・ページの meta にはなし | 同左 | 同左 |

その他：コーポレートのサーバーに、ビルドの外の旧ページ（brands.html・ebisenka.html・thanks.html）と README.md 2件がある（削除しない・報告のみ）。brands.html・ebisenka.html は Google の検索結果になく、どこからもリンクされていない。福記美食では WordPress の `/sample-page/`（中身のない見本ページ・索引可能）・フィード・`/wp-json/` が残っている（検索結果にはない）。

## 2. 修正（2026-10-13・通常の自動公開 実行 38362655808・公開後の検証に合格）
- コーポレート：`robots.txt`（すべて許可・mail.php は巡回しない・sitemap 記載）と `sitemap.xml`（8ページ・canonical と同じ www の URL・lastmod なし）を追加
- 福記美食：`robots.txt` に、旧 WordPress の残り（`/sample-page/`・`/feed/`・`/comments/feed/`・`/wp-json/`・`/wp-admin/`・`/?s=`）を巡回しない指定を追加。旧ページの URL（/lunch/ など）は転送を Google が読めるよう止めていない。sitemap は日付（lastmod）だけ更新
- 公開前の検査に追加：sitemap の URL が正規のホスト（コーポレートは www）・ページの canonical と一致・noindex でない・robots.txt で止めていないこと
- 変えていないもの：canonical・既存ページ・WordPress・.htaccess・フォーム・メール送信処理・ebisenka.jp

**検索順位への影響の確認**：コーポレートは Google が www の URL で登録しているため、canonical は www のまま（変えると評価が分散するおそれ）。sitemap は canonical と同じ URL だけを載せた。福記美食で巡回を止めたパスは、いずれも検索結果に出ておらず、現在のページからも使っていない。

## 3. Search Console（ご本人の Google アカウントでの操作）
登録状況は Claude からは見えない（**未確認**）。DNS・ページに確認用の記録がないため、少なくとも「DNS による所有権の確認」はされていない。

**おすすめ：URL プレフィックスのプロパティ＋HTML タグ（DNS の操作なし）**
1. https://search.google.com/search-console を開き、「プロパティを追加」→「URL プレフィックス」に、次を入力：
   - `https://www.fukushin-e.co.jp/`（www 付き）
   - `https://ebisenka.jp/`
   - `https://ebisenka-seavans.com/`
2. 所有権の確認方法で「HTML タグ」を選び、表示された `<meta name="google-site-verification" content="…">` を Claude に伝える（公開ページに載せる値で、秘密ではない）
3. Claude がトップページに載せて、通常の自動公開で公開・確認する → ご本人が「確認」を押す
4. 確認できたら「サイトマップ」に `sitemap.xml` を送信（コーポレートは `https://www.fukushin-e.co.jp/sitemap.xml`）
5. ebisenka.jp・コーポレートは「URL 検査」でトップページの「インデックス登録をリクエスト」

**別の方法：ドメイン プロパティ（DNS の TXT）**：http/https・www をまとめて扱える。3ドメインとも DNS はレンタルサーバー（`ns-rs1/2.gmoserver.jp`）のため、TXT レコードはレンタルサーバー側の DNS 設定に追加する（画面の場所はお名前.com の RS のヘルプで要確認）。
