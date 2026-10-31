# RSS to Discord 通知

`config/feeds.yml` に登録したRSS/Atomフィードの新着を、毎時23分にDiscordへ通知します。通知処理はGitHub Actionsのmainブランチ限定です。

## 対応フィード

| ID | 内容 | 通知先のSecret |
|---|---|---|
| `ja-blog` | テスト・QAブログ | `DISCORD_WEBHOOK_BLOG` または従来の `DISCORD_WEBHOOK_URL` |
| `release-playwright` | Playwrightリリース | `DISCORD_WEBHOOK_RELEASE` |
| `release-cypress` | Cypressリリース | `DISCORD_WEBHOOK_RELEASE` |
| `release-pytest` | pytestリリース | `DISCORD_WEBHOOK_RELEASE` |
| `release-k6` | k6リリース | `DISCORD_WEBHOOK_RELEASE` |
| `release-playwright-python` | Playwright for Pythonリリース | `DISCORD_WEBHOOK_RELEASE` |
| `arxiv-testing` | arXivのSoftware Engineering論文からテスト関連を選択 | `DISCORD_WEBHOOK_ARXIV` |
| `youtube-jasst` | JaSST動画 | `DISCORD_WEBHOOK_VIDEO` |

`DISCORD_WEBHOOK_URL` だけが設定されている既存環境では、従来どおり `ja-blog` のみを処理します。追加カテゴリは対応するSecretを設定すると有効になります。未設定の追加カテゴリは `WARNING: missing_webhook` を出してスキップします。JaSST動画のチャンネルIDは `config/feeds.yml` に記載しています。

## セットアップ

1. Discordの通知先チャンネルでWebhookを作成します。
2. GitHubリポジトリの **Settings → Secrets and variables → Actions** に、上表のSecretを登録します。既存の `DISCORD_WEBHOOK_URL` は引き続き使用できます。
3. **Actions → Check RSS Feed → Run workflow** でmainブランチから実行します。実行すると実際に投稿・状態更新が発生します。

各カテゴリを別チャンネルへ送る場合は、カテゴリごとにWebhookを作成してください。Secretの値をコードや設定ファイルに書かないでください。

## 処理と状態

1. 有効なフィードとWebhookを選び、`data/sent_articles.json` を読み込みます。
2. フィードごとに未送信GUIDを選び、古い順に処理します。初回はフィードごとに最新5件、arXivとYouTubeは最新3件を送信対象とし、除いた過去分のGUIDも先に記録します。
3. arXivはタイトルと概要のテスト関連語をスコアで判定します。Embedはカテゴリ別の色を使い、リリース名・出典・日時・サムネイルを表示します。
4. 送信前に状態を原子的に保存できることを確認し、Discordの2xx応答を得た記事だけを各送信直後に保存します。429は待機値が0〜60秒のときだけ1回再試行します。
5. あるフィードの取得や配信が失敗しても他フィードの処理を続け、全体はexit 1にします。保存失敗時は以後の送信を止めます。

従来形式の `sent_guids` と `last_checked` は `ja-blog` の履歴としてそのまま読み込みます。フィードを正常に取得できた実行で `version: 2` とフィード別の `feeds` を追加します。既存の状態ファイルを手作業で変更する必要はありません。`ja-blog` の履歴は互換性のためトップレベルに残ります。他フィードは `feeds.<id>.sent_guids` と `last_checked` を使います。

stdout末尾のJSONには `status`, `selected`, `sent`, `failed`, `delivery_unknown`, `unpersisted_sent`, `errors` を出力します。Actionsではフィード別の処理結果をジョブサマリにも書きます。例外本文、Webhook URL、フィードURL、応答本文、記事タイトル、GUIDはログに出しません。

部分失敗・状態破損・ブログWebhook不足は非ゼロ終了します。通知stepが失敗しても `persist_state.py` が成功済みの状態を最新mainにGUID和集合で反映し、jobは失敗のままです。永続化失敗時は検証済み状態だけを7日間の回復artifactに保管します。タイムアウトなどは配送結果が不明で、exactly-onceは保証できません。

## カスタマイズ

フィードの追加・URL・カテゴリ・通知先環境変数・初回上限・送信間隔は `config/feeds.yml` を編集します。フィードIDは一意にし、追加カテゴリ用WebhookのSecretも設定してください。頻度は `.github/workflows/check-rss.yml` のcronを編集します。

## ローカル開発

Python 3.12と [uv 0.12.19](https://docs.astral.sh/uv/getting-started/installation/) を使います。依存は `pyproject.toml` と `uv.lock` で固定し、PyPIとuv既定の `first-index` 方針を使います。Requestsは2.33.0です。依存を変えるときは宣言を編集して `uv lock` を実行します。

```sh
uv sync --locked
uv run python -m unittest discover -s tests -v
```

テストは通信を遮断し、実際のWebhookやSecretsを使いません。送信・状態保存をせずに取得件数を確認する場合は明示的なdry-runを使います。

```sh
uv run --locked --no-dev python scripts/check_rss.py --dry-run
```

実際に送信する場合だけWebhook環境変数を設定し、`uv run --locked --no-dev python scripts/check_rss.py` を実行します。状態ファイルも更新されます。

uvへの移行だけで依存が安全になるわけではありません。Requests 2.33.0はCVE-2024-47081とCVE-2026-25645の対象範囲を外しています。詳細は[公式のセキュリティ情報](https://github.com/psf/requests/security/advisories)を参照してください。

## トラブルシューティング

| コード・症状 | 対処 |
|---|---|
| `missing_webhook` | ブログ用の `DISCORD_WEBHOOK_BLOG` または `DISCORD_WEBHOOK_URL` を設定 |
| `WARNING: missing_webhook` | 対応する追加カテゴリのSecretを設定すると処理開始 |
| `feeds_config_invalid` | `config/feeds.yml` のID、URL、カテゴリ、設定値を確認 |
| `state_unreadable_or_invalid` | 空状態へリセットせず、正常な履歴と実投稿を照合して復旧 |
| `delivery_unknown` | Discordの実投稿を確認してから再実行・復旧を判断 |
| `state_save_failed_after_send` | 以後の送信は停止。`unpersisted_sent` と実投稿を照合して状態を修復 |
| `state_persistence_failed` | `sent-state-recovery-<run_id>` artifactと最新mainのGUID和集合を確認。古いsnapshotで上書きしない |

失敗時の意味と復旧条件は[ADR 001](docs/adr/001-failure-and-state.md)、検証範囲は[機能別テスト方針](docs/testing-failure-contract.md)を参照してください。
