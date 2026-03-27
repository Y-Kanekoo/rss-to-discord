# RSS to Discord 通知

テスト・QA関連ブログの[RSSフィード](https://yoshikiito.github.io/test-qa-rss-feed/)を1時間ごとにチェックし、新着記事をDiscordに通知します。

## セットアップ

### 1. Discord Webhookの作成

1. Discordで通知先チャンネルの設定（歯車アイコン）を開く
2. **連携サービス** → **ウェブフック** → **新しいウェブフック** をクリック
3. 名前を設定（例: `RSS通知Bot`）
4. **ウェブフックURLをコピー** をクリック

### 2. GitHubリポジトリの設定

1. このリポジトリをGitHubにプッシュ
2. **Settings** → **Secrets and variables** → **Actions** を開く
3. **New repository secret** をクリック
4. Name: `DISCORD_WEBHOOK_URL`、Secret: コピーしたWebhook URL を入力

### 3. 動作確認

**Actions** → **Check RSS Feed** → **Run workflow** で手動実行し、Discordに通知が届くことを確認してください。

## 仕組み

- GitHub Actionsが毎時0分にRSSフィードをチェック
- 送信済み記事のIDを `data/sent_articles.json` で管理し、重複送信を防止
- 新着記事はDiscord Embed形式で送信（タイトル・リンク・要約・サムネイル付き）
- 初回実行時は最新5件のみ送信（チャンネルが埋まるのを防止）

## 設計図

### クラス図

```mermaid
classDiagram
    class GitHubActions {
        +cron: 毎時0分
        +workflow_dispatch: 手動実行
        +concurrency: rss-check
        checkout()
        setup_python()
        run_script()
        commit_and_push()
    }

    class CheckRSS {
        -RSS_URL: str
        -STATE_FILE: str
        -MAX_ARTICLES_FIRST_RUN: int
        -RATE_LIMIT_INTERVAL: float
        +main()
        +load_state(path) dict
        +save_state(path, state)
        +fetch_feed(url) FeedParserDict
        +build_embed(entry) dict
        +send_to_discord(webhook_url, embed)
    }

    class State {
        +sent_guids: list~str~
        +last_checked: str
    }

    class RSSEntry {
        +title: str
        +link: str
        +guid: str
        +published: str
        +description: str
        +enclosures: list
    }

    class DiscordEmbed {
        +title: str
        +url: str
        +description: str
        +color: int
        +footer: dict
        +timestamp: str
        +thumbnail: dict
    }

    class DiscordWebhook {
        +url: str
        +post(embeds)
        +レート制限: 30msg/min
    }

    class RSSFeed {
        +entries: list~RSSEntry~
        +bozo: bool
    }

    GitHubActions --> CheckRSS : 実行
    CheckRSS --> State : 読み書き
    CheckRSS --> RSSFeed : フェッチ
    RSSFeed "1" *-- "*" RSSEntry
    CheckRSS --> DiscordEmbed : 構築
    RSSEntry ..> DiscordEmbed : 変換
    CheckRSS --> DiscordWebhook : 送信
    DiscordWebhook --> DiscordEmbed : 受信
```

### シーケンス図

```mermaid
sequenceDiagram
    participant Cron as GitHub Actions<br/>(毎時0分)
    participant Script as check_rss.py
    participant JSON as sent_articles.json
    participant RSS as RSSフィード<br/>(GitHub Pages)
    participant Discord as Discord Webhook

    Cron->>Script: python scripts/check_rss.py

    Script->>JSON: load_state()
    JSON-->>Script: sent_guids, last_checked

    Script->>RSS: feedparser.parse(RSS_URL)
    RSS-->>Script: feed.entries (45件)

    Script->>Script: 新着記事を抽出<br/>(guid ∉ sent_guids)

    alt 初回実行 かつ 新着 > 5件
        Script->>Script: 最新5件のみ送信対象<br/>残りはguid記録のみ
    end

    loop 新着記事ごと
        Script->>Script: build_embed(entry)<br/>タイトル分離・要約切り詰め
        Script->>Discord: POST /webhooks/{id}/{token}<br/>{"embeds": [embed]}
        alt 成功 (200)
            Discord-->>Script: OK
            Script->>Script: sent_guidsにguidを追加
        else レート制限 (429)
            Discord-->>Script: retry_after
            Script->>Script: retry_after秒待機
            Script->>Discord: 再送信
            Discord-->>Script: OK
        else エラー (4xx/5xx)
            Discord-->>Script: エラー
            Script->>Script: スキップ（次回リトライ）
        end
        Script->>Script: 2秒待機
    end

    Script->>JSON: save_state()<br/>sent_guids + last_checked更新

    Cron->>Cron: git add → commit → push<br/>(変更がある場合のみ)
```

## ローカルテスト

```bash
pip install -r requirements.txt
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." python scripts/check_rss.py
```
