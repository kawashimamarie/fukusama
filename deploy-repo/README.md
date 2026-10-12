# fukushin-deploy（非公開・ご本人だけ）

福進発展グループ3サイト（ebisenka.jp・ebisenka-seavans.com・fukushin-e.co.jp）を、
fukusama（公開リポジトリ）の main の最新から FTPS で公開するためのリポジトリ。

- 本番の FTPS 情報（Secrets）はこのリポジトリにだけ置く。Claude の GitHub App の対象に入れない
- `.github/workflows/deploy.yml`：毎時の自動公開・手動の確認（check）／公開（publish）
- `.github/workflows/restore.yml`：手動の復元
- `deployer/`：公開の処理（Python 標準ライブラリだけ）。毎回の実行で `deployer/SHA256SUMS` と照合する

このフォルダの原本は fukusama の `deploy-repo/`。設計・準備・初回公開・通常更新・異常時の手順は
fukusama の `docs/17_自動公開システム.md`。
