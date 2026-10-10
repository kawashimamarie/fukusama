<?php
// お問い合わせフォームの設定（公開前に必ず確認・変更してください）
// このファイルは .htaccess で直接アクセスを禁止しています。

return [
    // 問い合わせの受信先（確定：2026-10-10）
    'to' => 'info@fukushin-e.co.jp',

    // 送信元アドレス（確定：2026-10-10）。From と送信エラーの戻り先（-f）に使う。
    // お客様のメールアドレスは From に入れず、Reply-To にのみ設定する（send.php）。
    // fukushin-e.co.jp の SPF（include:_spf.onamae.ne.jp）に、このサーバーの送信元IPが含まれることを確認済み。
    'from' => 'info@fukushin-e.co.jp',
    'from_name' => '海老専家 弁当事業サイト',

    'subject' => '【ebisenka.jp】弁当卸・大口注文のお問い合わせ',

    // 受付確認メールを入力者へ自動返信するか（初期値：送らない）
    'auto_reply' => false,

    // 同一IPからの連続送信を制限（秒）
    'rate_limit_seconds' => 60,

    // テストモード：true にするとメールを送信せず、送信されるはずの内容を画面に表示する（保存もしない）
    // ※プレビュー環境専用。本番では必ず false
    'test_mode' => false,

    // サイトを置くディレクトリ。本番はドメイン直下なので空文字のまま（プレビュー用パッケージ作成時のみ自動で変更される）
    'base_path' => '',

    // 許可する送信元ホスト（Origin/Referer チェック）
    'allowed_hosts' => ['ebisenka.jp', 'www.ebisenka.jp'],
];
