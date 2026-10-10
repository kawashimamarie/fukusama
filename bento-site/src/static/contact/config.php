<?php
// お問い合わせフォームの設定（公開前に必ず確認・変更してください）
// このファイルは .htaccess で直接アクセスを禁止しています。

return [
    // 問い合わせの受信先（※未確認：弁当事業の受付窓口アドレスを確認のうえ設定）
    'to' => 'info@fukushin-e.co.jp',

    // 送信元アドレス。サーバーのドメイン（ebisenka.jp）のアドレスを推奨。
    // ※ebisenka.jp のメールアカウント／SPF設定を確認してください。
    'from' => 'noreply@ebisenka.jp',
    'from_name' => '海老専家 弁当事業サイト',

    'subject' => '【ebisenka.jp】弁当卸・大口注文のお問い合わせ',

    // 受付確認メールを入力者へ自動返信するか（初期値：送らない）
    'auto_reply' => false,

    // 同一IPからの連続送信を制限（秒）
    'rate_limit_seconds' => 60,

    // テストモード：true にするとメールを送信せず、送信されるはずの内容を画面に表示する（保存もしない）
    // ※プレビュー環境専用。本番では必ず false
    'test_mode' => false,

    // サイトを置くディレクトリ（本番はドメイン直下なので空文字）。プレビューでは例 '/_preview-fk2026/bento'
    'base_path' => '',

    // 許可する送信元ホスト（Origin/Referer チェック）
    'allowed_hosts' => ['ebisenka.jp', 'www.ebisenka.jp'],
];
