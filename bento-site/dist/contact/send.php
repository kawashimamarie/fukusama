<?php
// ebisenka.jp お問い合わせフォーム送信処理（PHP 7.4+ / mbstring 必須）
declare(strict_types=1);

mb_language('Japanese');
mb_internal_encoding('UTF-8');
date_default_timezone_set('Asia/Tokyo');

$config = require __DIR__ . '/config.php';
$base = rtrim((string) ($config['base_path'] ?? ''), '/');
$testMode = !empty($config['test_mode']);

function h(string $s): string
{
    return htmlspecialchars($s, ENT_QUOTES, 'UTF-8');
}

// 改行・制御文字を除去（ヘッダーインジェクション対策を兼ねる）
function one_line(string $s): string
{
    return trim(preg_replace('/[\x00-\x1F\x7F]+/u', ' ', $s) ?? '');
}

function multi_line(string $s): string
{
    $s = str_replace(["\r\n", "\r"], "\n", $s);
    return trim(preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]+/u', '', $s) ?? '');
}

function field(string $key, bool $multi = false, int $max = 200): string
{
    $v = isset($_POST[$key]) && is_string($_POST[$key]) ? $_POST[$key] : '';
    if (!mb_check_encoding($v, 'UTF-8')) {
        $v = '';
    }
    $v = $multi ? multi_line($v) : one_line($v);
    return mb_substr($v, 0, $max);
}

function fail(array $errors, int $status = 400): void
{
    global $base;
    http_response_code($status);
    header('Content-Type: text/html; charset=UTF-8');
    $items = '';
    foreach ($errors as $e) {
        $items .= '<li>' . h($e) . '</li>';
    }
    echo '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        . '<meta name="viewport" content="width=device-width, initial-scale=1">'
        . '<meta name="robots" content="noindex"><title>入力内容をご確認ください｜海老専家 弁当事業</title>'
        . '<link rel="stylesheet" href="' . h($base) . '/assets/css/site.css"></head><body>'
        . '<main class="section"><div class="wrap" style="max-width:760px">'
        . '<p class="eyebrow">CONTACT</p><h1 class="h2">入力内容をご確認ください</h1>'
        . '<div class="form-errors" style="margin-top:24px"><ul style="padding-left:1.2em">' . $items . '</ul></div>'
        . '<p class="btn-row"><a class="btn btn--ghost" href="javascript:history.back()">入力画面へ戻る</a></p>'
        . '</div></main></body></html>';
    exit;
}

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    header('Location: ' . $base . '/contact/', true, 303);
    exit;
}

// 送信元チェック
$origin = $_SERVER['HTTP_ORIGIN'] ?? ($_SERVER['HTTP_REFERER'] ?? '');
$host = $origin !== '' ? (string) parse_url($origin, PHP_URL_HOST) : '';
if ($host === '' || !in_array(strtolower($host), $config['allowed_hosts'], true)) {
    fail(['不正な送信元からのリクエストです。お手数ですが、フォームから再度送信してください。'], 403);
}

// ボット対策（入力されていたら送信したように見せて終了）
if (!empty($_POST['website'])) {
    header('Location: ' . $base . '/contact/thanks/', true, 303);
    exit;
}

// 連続送信の制限
$ip = $_SERVER['REMOTE_ADDR'] ?? 'unknown';
$lockFile = sys_get_temp_dir() . '/ebisenka_contact_' . hash('sha256', $ip);
if (!$testMode && is_file($lockFile) && (time() - (int) filemtime($lockFile)) < (int) $config['rate_limit_seconds']) {
    fail(['短時間に連続して送信されました。しばらく経ってから再度お試しください。'], 429);
}

$data = [
    'company'  => field('company', false, 100),
    'name'     => field('name', false, 60),
    'email'    => field('email', false, 200),
    'tel'      => field('tel', false, 30),
    'type'     => field('type', false, 100),
    'quantity' => field('quantity', false, 100),
    'area'     => field('area', false, 100),
    'start'    => field('start', false, 50),
    'message'  => field('message', true, 3000),
];
$agree = isset($_POST['agree']) && $_POST['agree'] === '1';

$types = [
    '継続的な卸取引・仕入れのご相談',
    '大口注文（会議・イベントなど）のご相談',
    '弁当販売の新規事業についてのご相談',
    'その他',
];
$starts = ['', 'できるだけ早く', '1か月以内', '3か月以内', '半年以内', '時期は未定'];

$errors = [];
if ($data['company'] === '') {
    $errors[] = '会社名を入力してください。';
}
if ($data['name'] === '') {
    $errors[] = 'ご担当者名を入力してください。';
}
if (!filter_var($data['email'], FILTER_VALIDATE_EMAIL)) {
    $errors[] = 'メールアドレスを正しく入力してください。';
}
if ($data['tel'] !== '' && !preg_match('/^[0-9０-９+\-－ー()（） ]{6,30}$/u', $data['tel'])) {
    $errors[] = '電話番号を正しく入力してください。';
}
if (!in_array($data['type'], $types, true)) {
    $errors[] = 'お問い合わせ種別を選択してください。';
}
if (!in_array($data['start'], $starts, true)) {
    $errors[] = '希望開始時期を選択し直してください。';
}
if ($data['message'] === '') {
    $errors[] = 'お問い合わせ内容を入力してください。';
}
if (!$agree) {
    $errors[] = 'プライバシーポリシーへの同意が必要です。';
}
if ($errors) {
    fail($errors);
}

$or = static function (string $v): string {
    return $v === '' ? '（未記入）' : $v;
};

$body = "ebisenka.jp のお問い合わせフォームから送信がありました。\n\n"
    . "■会社名\n{$data['company']}\n\n"
    . "■ご担当者名\n{$data['name']}\n\n"
    . "■メールアドレス\n{$data['email']}\n\n"
    . "■電話番号\n" . $or($data['tel']) . "\n\n"
    . "■お問い合わせ種別\n{$data['type']}\n\n"
    . "■ご希望の数量\n" . $or($data['quantity']) . "\n\n"
    . "■希望納品地域\n" . $or($data['area']) . "\n\n"
    . "■希望開始時期\n" . $or($data['start']) . "\n\n"
    . "■お問い合わせ内容\n{$data['message']}\n\n"
    . "----\n送信日時：" . date('Y-m-d H:i:s') . "\n";

$fromHeader = 'From: ' . mb_encode_mimeheader($config['from_name']) . ' <' . $config['from'] . '>';
$headers = $fromHeader . "\r\n" . 'Reply-To: ' . $data['email'];

// テストモード：送信せず、送信されるはずの内容を表示して終了（入力内容は保存しない）
if ($testMode) {
    header('Content-Type: text/html; charset=UTF-8');
    header('X-Robots-Tag: noindex, nofollow');
    echo '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        . '<meta name="viewport" content="width=device-width, initial-scale=1">'
        . '<meta name="robots" content="noindex, nofollow"><title>テスト送信の結果｜プレビュー</title>'
        . '<link rel="stylesheet" href="' . h($base) . '/assets/css/site.css"></head><body>'
        . '<main class="section"><div class="wrap" style="max-width:760px">'
        . '<p class="eyebrow">TEST MODE</p><h1 class="h2">テスト送信を受け付けました</h1>'
        . '<p class="notice" style="margin:20px 0">プレビュー環境のため、<strong>メールは送信していません</strong>。入力内容はサーバーに保存していません。'
        . '本番では、次の内容のメールが ' . h($config['to']) . ' に届きます。</p>'
        . '<pre style="white-space:pre-wrap;background:#fff;border:1px solid var(--line);border-radius:10px;padding:20px;font-size:14px">'
        . h('件名：' . $config['subject'] . "\n\n" . $body) . '</pre>'
        . '<p class="btn-row" style="margin-top:28px"><a class="btn btn--red" href="' . h($base) . '/contact/thanks/">完了ページを確認する</a>'
        . '<a class="btn btn--ghost" href="' . h($base) . '/contact/">フォームへ戻る</a></p>'
        . '</div></main></body></html>';
    exit;
}

$sent = mb_send_mail($config['to'], $config['subject'], $body, $headers, '-f' . $config['from']);
if (!$sent) {
    fail(['送信に失敗しました。お手数ですが、時間をおいて再度お試しいただくか、お電話（03-3269-1461）でお問い合わせください。'], 500);
}

@touch($lockFile);

if (!empty($config['auto_reply'])) {
    $reply = "{$data['company']}\n{$data['name']} 様\n\n"
        . "お問い合わせいただき、ありがとうございます。\n"
        . "以下の内容で受け付けました。内容を確認のうえ、担当者よりご連絡します。\n\n"
        . $body
        . "\n株式会社福進発展\nhttps://ebisenka.jp/\n";
    mb_send_mail($data['email'], '【海老専家 弁当事業】お問い合わせを受け付けました', $reply, $fromHeader, '-f' . $config['from']);
}

header('Location: ' . $base . '/contact/thanks/', true, 303);
exit;
