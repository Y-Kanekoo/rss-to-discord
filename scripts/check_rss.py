"""RSSフィードの新着記事をDiscord Webhookに送信するスクリプト"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests

# 定数
RSS_URL = "https://yoshikiito.github.io/test-qa-rss-feed/feeds/rss.xml"
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "sent_articles.json")
MAX_ARTICLES_FIRST_RUN = 5
RATE_LIMIT_INTERVAL = 2.0
EMBED_COLOR = 0x5865F2
DESCRIPTION_MAX_LENGTH = 300


def load_state(path: str) -> dict:
    """送信済み記事IDをロード"""
    if not os.path.exists(path):
        return {"sent_guids": [], "last_checked": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print("警告: 状態ファイルが破損。初期状態で再開します", file=sys.stderr)
        return {"sent_guids": [], "last_checked": None}


def save_state(path: str, state: dict) -> None:
    """送信済み記事IDを保存"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_feed(url: str) -> feedparser.FeedParserDict:
    """RSSフィードを取得してパース"""
    feed = feedparser.parse(url)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSSフィードのパースに失敗: {feed.bozo_exception}")
    return feed


def build_embed(entry: feedparser.FeedParserDict) -> dict:
    """RSS記事からDiscord Embedオブジェクトを構築"""
    # タイトルから出典元を分離
    title_parts = entry.title.rsplit(" | ", 1)
    article_title = title_parts[0]
    source = title_parts[1] if len(title_parts) > 1 else ""

    # descriptionの切り詰め
    description = entry.get("description", "")
    if len(description) > DESCRIPTION_MAX_LENGTH:
        description = description[: DESCRIPTION_MAX_LENGTH - 3] + "..."

    # pubDateのパース
    timestamp = None
    pub_date = entry.get("published", "")
    if pub_date:
        try:
            dt = parsedate_to_datetime(pub_date)
            timestamp = dt.isoformat()
        except Exception:
            pass

    embed: dict = {
        "title": article_title[:256],
        "url": entry.link,
        "description": description,
        "color": EMBED_COLOR,
    }

    if source:
        embed["footer"] = {"text": source}
    if timestamp:
        embed["timestamp"] = timestamp

    # enclosure（サムネイル画像）があれば追加
    if hasattr(entry, "enclosures") and entry.enclosures:
        img_url = entry.enclosures[0].get("url", "")
        if img_url and not img_url.startswith("data:") and len(img_url) < 2000:
            embed["thumbnail"] = {"url": img_url}

    return embed


def send_to_discord(webhook_url: str, embed: dict) -> None:
    """Discord WebhookにEmbed付きメッセージを送信"""
    payload = {"embeds": [embed]}
    response = requests.post(
        webhook_url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )

    # レート制限対応
    if response.status_code == 429:
        retry_after = response.json().get("retry_after", 5)
        print(f"レート制限。{retry_after}秒待機します...")
        time.sleep(retry_after)
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )

    response.raise_for_status()


def main() -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("エラー: DISCORD_WEBHOOK_URL環境変数が設定されていません", file=sys.stderr)
        sys.exit(1)

    state = load_state(STATE_FILE)
    sent_guids = set(state.get("sent_guids", []))
    is_first_run = state.get("last_checked") is None

    # RSSフィードを取得
    try:
        feed = fetch_feed(RSS_URL)
    except Exception as e:
        print(f"エラー: RSSフィードの取得に失敗: {e}", file=sys.stderr)
        sys.exit(1)

    # 新着記事を抽出（古い順にソート）
    new_entries = [e for e in feed.entries if e.get("id", e.get("link")) not in sent_guids]
    new_entries.sort(key=lambda e: e.get("published_parsed", ()), reverse=False)

    # 初回実行時は最新N件のみ送信
    if is_first_run and len(new_entries) > MAX_ARTICLES_FIRST_RUN:
        print(f"初回実行: {len(new_entries)}件中、最新{MAX_ARTICLES_FIRST_RUN}件のみ送信します")
        skipped = new_entries[:-MAX_ARTICLES_FIRST_RUN]
        for e in skipped:
            sent_guids.add(e.get("id", e.get("link")))
        new_entries = new_entries[-MAX_ARTICLES_FIRST_RUN:]

    if not new_entries:
        print("新着記事はありません")
    else:
        print(f"{len(new_entries)}件の新着記事を送信します")

    sent_count = 0
    for entry in new_entries:
        guid = entry.get("id", entry.get("link"))
        try:
            embed = build_embed(entry)
            send_to_discord(webhook_url, embed)
            sent_guids.add(guid)
            sent_count += 1
            print(f"送信完了: {entry.title[:60]}")
            # レート制限対策: 送信間隔を空ける
            if sent_count < len(new_entries):
                time.sleep(RATE_LIMIT_INTERVAL)
        except requests.exceptions.HTTPError as e:
            print(f"Discord送信エラー: {entry.title[:60]} - {e}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"予期しないエラー: {entry.title[:60]} - {e}", file=sys.stderr)
            continue

    # 状態を保存
    state["sent_guids"] = list(sent_guids)
    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    save_state(STATE_FILE, state)
    print(f"完了: {sent_count}/{len(new_entries)}件送信成功")


if __name__ == "__main__":
    main()
