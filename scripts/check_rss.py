"""複数RSSフィードの新着記事をDiscord Webhookに送信するスクリプト"""

import argparse
import json
import math
import os
import re
import socket
import sys
import time
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests
import yaml

# 定数
RSS_URL = "https://yoshikiito.github.io/test-qa-rss-feed/feeds/rss.xml"
FEEDS_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "feeds.yml")
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "sent_articles.json")
MAX_ARTICLES_FIRST_RUN = 5
RATE_LIMIT_INTERVAL = 2.0
EMBED_COLOR = 0x5865F2
DESCRIPTION_MAX_LENGTH = 300
FETCH_TIMEOUT = 20
CATEGORY_COLORS = {"ja_blog": 0x7ED321, "release": 0x2EA44F,
                   "arxiv": 0xB31B1B, "youtube": 0xFF0000, "event": 0xF5A623}
ARXIV_KEYWORDS = ("test", "testing", "fuzzing", "mutation", "coverage",
                  "regression", "assertion", "flaky", "oracle",
                  "test generation", "test automation", "test case")


class StateError(ValueError):
    """A state snapshot cannot safely be used; never reset it automatically."""


def validate_state(state: object) -> dict:
    if not isinstance(state, dict):
        raise StateError("state_invalid")
    guids = state.get("sent_guids")
    if not isinstance(guids, list) or any(not isinstance(g, str) or not g for g in guids):
        raise StateError("state_invalid")
    if "last_checked" not in state or not (
        state["last_checked"] is None
        or isinstance(state["last_checked"], str) and bool(state["last_checked"])
    ):
        raise StateError("state_invalid")
    if state["last_checked"] is not None:
        try:
            datetime.fromisoformat(state["last_checked"])
        except ValueError as error:
            raise StateError("state_invalid") from error
    if "version" in state or "feeds" in state:
        if type(state.get("version")) is not int or state["version"] != 2 or not isinstance(state.get("feeds"), dict):
            raise StateError("state_invalid")
        for feed_id, feed_state in state["feeds"].items():
            if (not isinstance(feed_id, str) or not re.fullmatch(r"[a-z0-9-]+", feed_id)
                    or feed_id == "ja-blog" or not isinstance(feed_state, dict)
                    or "version" in feed_state or "feeds" in feed_state):
                raise StateError("state_invalid")
            validate_state(feed_state)
    return state


def load_feeds_config(path: str) -> list[dict]:
    """有効フィードを読み込み、不正な設定は送信前に拒否する。"""
    try:
        with open(path, "r", encoding="utf-8") as source:
            config = yaml.safe_load(source)
        if not isinstance(config, dict) or not isinstance(config.get("feeds"), list):
            raise ValueError("invalid_config")
        defaults = config.get("defaults", {})
        if not isinstance(defaults, dict):
            raise ValueError("invalid_config")
        feeds = []
        ids = set()
        for item in config["feeds"]:
            if not isinstance(item, dict):
                raise ValueError("invalid_config")
            feed = {**defaults, **item}
            feed_id = feed.get("id")
            if (not isinstance(feed_id, str) or not re.fullmatch(r"[a-z0-9-]+", feed_id)
                    or feed_id in ids or not isinstance(feed.get("url"), str)
                    or not feed["url"].startswith("https://")
                    or not isinstance(feed.get("webhook_env"), str)
                    or not re.fullmatch(r"DISCORD_WEBHOOK_[A-Z_]+", feed["webhook_env"])
                    or not isinstance(feed.get("category"), str)
                    or feed["category"] not in CATEGORY_COLORS
                    or not isinstance(feed.get("enabled", True), bool)):
                raise ValueError("invalid_config")
            ids.add(feed_id)
            for key in ("max_articles_first_run", "rate_limit_interval"):
                value = feed.get(key)
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value < 0
                        or key == "max_articles_first_run" and (not isinstance(value, int) or value < 1)):
                    raise ValueError("invalid_config")
            if feed.get("enabled", True):
                feeds.append(feed)
        if not feeds or not any(feed["id"] == "ja-blog" for feed in feeds):
            raise ValueError("invalid_config")
        return feeds
    except FileNotFoundError:
        # 単一フィード構成で配布されたスクリプトも引き続き実行できる。
        return [{"id": "ja-blog", "name": "テスト・QAブログ", "url": RSS_URL,
                 "category": "ja_blog", "webhook_env": "DISCORD_WEBHOOK_BLOG",
                 "max_articles_first_run": MAX_ARTICLES_FIRST_RUN,
                 "rate_limit_interval": RATE_LIMIT_INTERVAL}]
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as error:
        raise ValueError("feeds_config_invalid") from error


def get_feed_state(state: dict, feed_id: str) -> dict:
    """旧状態は ja-blog の履歴としてそのまま使う。"""
    if feed_id == "ja-blog":
        return state
    return state.setdefault("feeds", {}).setdefault(feed_id,
        {"sent_guids": [], "last_checked": None})


def get_entry_guid(entry: feedparser.FeedParserDict) -> str | None:
    return entry.get("id") or entry.get("link") or entry.get("guid")


def is_arxiv_match(entry: feedparser.FeedParserDict) -> bool:
    title = entry.get("title") or ""
    summary = entry.get("summary") or ""
    description = entry.get("description") or ""
    body = summary if summary == description else summary + " " + description
    if not isinstance(title, str) or not isinstance(body, str):
        return False
    score = 0
    for keyword in ARXIV_KEYWORDS:
        pattern = r"\b" + re.escape(keyword) + r"\b"
        score += 2 * len(re.findall(pattern, title, re.IGNORECASE))
        score += len(re.findall(pattern, body, re.IGNORECASE))
    return score >= 2


def _unique_keys(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise StateError("state_invalid")
        result[key] = value
    return result


def load_state(path: str) -> dict:
    """Missing is a first run; unreadable, malformed or invalid state is fatal."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return validate_state(json.load(f, object_pairs_hook=_unique_keys))
    except FileNotFoundError:
        return {"sent_guids": [], "last_checked": None}
    except (OSError, ValueError, UnicodeError) as error:
        raise StateError("state_unreadable_or_invalid") from error


def save_state(path: str, state: dict) -> None:
    """Replace atomically in the same directory; a failure keeps the old file."""
    validate_state(state)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=".sent-state-", delete=False) as f:
            temporary = f.name
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def fetch_feed(url: str) -> feedparser.FeedParserDict:
    """Fetch RSS; HTTP errors and unparseable feeds must not look like no news."""
    if isinstance(url, str) and url.startswith("https://"):
        # 単独プロセス内のfeedparser取得に接続・読取タイムアウトを適用する。
        previous_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(FETCH_TIMEOUT)
        try:
            feed = feedparser.parse(url)
        finally:
            socket.setdefaulttimeout(previous_timeout)
    else:
        feed = feedparser.parse(url)
    if (feed.get("status", 200) >= 400 or not feed.get("version")
            or (feed.bozo and not feed.entries)):
        raise RuntimeError("feed_unavailable_or_invalid")
    return feed


def build_embed(entry: feedparser.FeedParserDict, feed_config: dict | None = None) -> dict:
    """RSS記事からDiscord Embedオブジェクトを構築"""
    # タイトルから出典元を分離
    category = feed_config.get("category", "ja_blog") if feed_config else "ja_blog"
    title_parts = entry.title.rsplit(" | ", 1) if category == "ja_blog" else [entry.title]
    article_title = title_parts[0]
    source = title_parts[1] if len(title_parts) > 1 else ""
    if feed_config:
        source = source or feed_config.get("name", "")
        if category == "release":
            article_title = f"{feed_config.get('name', '')}: {article_title}"

    # descriptionの切り詰め
    description = entry.get("description", "")
    if len(description) > DESCRIPTION_MAX_LENGTH:
        description = description[: DESCRIPTION_MAX_LENGTH - 3] + "..."

    # pubDateのパース
    timestamp = None
    pub_date = entry.get("published") or entry.get("updated") or ""
    if pub_date:
        try:
            dt = parsedate_to_datetime(pub_date)
            timestamp = dt.isoformat()
        except Exception:
            try:
                timestamp = datetime.fromisoformat(pub_date.replace("Z", "+00:00")).isoformat()
            except (ValueError, TypeError, AttributeError):
                pass

    embed: dict = {
        "title": article_title[:256],
        "url": entry.link,
        "description": description,
        "color": CATEGORY_COLORS.get(category, EMBED_COLOR) if feed_config else EMBED_COLOR,
    }

    if source:
        embed["footer"] = {"text": source}
    if timestamp:
        embed["timestamp"] = timestamp

    # enclosure（サムネイル画像）があれば追加
    thumbnails = entry.get("media_thumbnail") or [] if category == "youtube" else []
    img_url = thumbnails[0].get("url", "") if thumbnails else ""
    if not img_url and hasattr(entry, "enclosures") and entry.enclosures:
        img_url = entry.enclosures[0].get("url", "")
    if isinstance(img_url, str) and img_url and not img_url.startswith("data:") and len(img_url) < 2000:
        embed["thumbnail"] = {"url": img_url}

    return embed


def send_to_discord(webhook_url: str, embed: dict) -> None:
    """Retry only an explicit 429, once. Do not retry an ambiguous response."""
    payload = {"embeds": [embed]}
    for attempt in range(2):
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code == 429 and attempt == 0:
            retry_after = response.json().get("retry_after", 5)
            if (isinstance(retry_after, bool) or not isinstance(retry_after, (int, float))
                    or not math.isfinite(retry_after) or not 0 <= retry_after <= 60):
                raise ValueError("invalid_retry_after")
            time.sleep(retry_after)
            continue
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise requests.HTTPError("unexpected_discord_status")
        return


def main(*, dry_run: bool = False) -> int:
    result = {"status": "ok", "dry_run": dry_run, "selected": 0, "sent": 0,
              "failed": 0, "delivery_unknown": 0, "unpersisted_sent": 0, "errors": []}
    feed_results: dict[str, dict[str, int | str]] = {}

    def fail(code: str) -> None:
        if code not in result["errors"]:
            result["errors"].append(code)
        # Never print exception text, URLs, response bodies, titles or GUIDs.
        print(f"エラー: {code}", file=sys.stderr)

    def finish() -> int:
        failed = bool(result["errors"])
        result["status"] = "failed" if failed else ("dry_run" if dry_run else "ok")
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            try:
                with open(summary_path, "a", encoding="utf-8") as summary:
                    summary.write("## RSS フィード処理結果\n\n| フィード | 選択 | 送信 | 状態 |\n|---|---:|---:|---|\n")
                    for feed_id, feed_result in feed_results.items():
                        summary.write(f"| {feed_id} | {feed_result['selected']} | {feed_result['sent']} | {feed_result['status']} |\n")
            except OSError:
                fail("summary_write_failed")
                result["status"] = "failed"
                failed = True
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if failed else 0

    try:
        configured_feeds = load_feeds_config(FEEDS_CONFIG_PATH)
    except ValueError:
        fail("feeds_config_invalid")
        return finish()
    active_feeds = []
    for feed_config in configured_feeds:
        feed_id = feed_config["id"]
        webhook = os.environ.get(feed_config["webhook_env"], "").strip()
        if feed_id == "ja-blog":
            webhook = webhook or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if webhook or (dry_run and feed_id == "ja-blog"):
            active_feeds.append((feed_config, webhook))
        elif feed_id != "ja-blog":
            print(f"WARNING: missing_webhook feed={feed_id}", file=sys.stderr)
    if not active_feeds and not dry_run:
        fail("missing_webhook")
        return finish()
    if not dry_run and not any(feed["id"] == "ja-blog" for feed, _ in active_feeds):
        fail("missing_webhook")
    try:
        state = load_state(STATE_FILE)
    except StateError:
        fail("state_unreadable_or_invalid")
        return finish()
    for feed_config, webhook_url in active_feeds:
        feed_id = feed_config["id"]
        feed_results[feed_id] = {"selected": 0, "sent": 0, "status": "ok"}
        try:
            feed = fetch_feed(feed_config["url"])
            feed_state = get_feed_state(state, feed_id)
            sent_guids = set(feed_state["sent_guids"])
            is_first_run = feed_state["last_checked"] is None
            # 不明な配送を同じ実行中に再試行しないよう、選択前に重複を除く。
            seen = set(sent_guids)
            new_entries = []
            for entry in feed.entries:
                if feed_config["category"] == "arxiv" and not is_arxiv_match(entry):
                    continue
                guid = get_entry_guid(entry)
                if not isinstance(guid, str) or not guid:
                    result["failed"] += 1
                    feed_results[feed_id]["status"] = "entry_invalid"
                    fail("entry_invalid")
                    continue
                if guid not in seen:
                    seen.add(guid)
                    new_entries.append((guid, entry))
            new_entries.sort(key=lambda item: item[1].get("published_parsed")
                             or item[1].get("updated_parsed") or ())
        except Exception:
            feed_results[feed_id]["status"] = "feed_unavailable_or_invalid"
            fail("feed_unavailable_or_invalid")
            continue
        max_first = feed_config["max_articles_first_run"]
        skipped_count = 0
        if is_first_run and len(new_entries) > max_first:
            sent_guids.update(guid for guid, _ in new_entries[:-max_first])
            skipped_count = len(new_entries) - max_first
            new_entries = new_entries[-max_first:]
        result["selected"] += len(new_entries)
        feed_results[feed_id]["selected"] = len(new_entries)
        if dry_run:
            continue
        migrating = "version" not in state
        state.setdefault("version", 2)
        state.setdefault("feeds", {})
        feed_state["sent_guids"] = sorted(sent_guids)
        if is_first_run:
            feed_state["last_checked"] = datetime.now(timezone.utc).isoformat()
        if new_entries or is_first_run or skipped_count or migrating:
            try:
                # 各フィードの送信前に書込可能性を確認し、初回の除外分を保存する。
                save_state(STATE_FILE, state)
            except Exception:
                feed_results[feed_id]["status"] = "state_save_failed"
                fail("state_save_failed")
                break
        for index, (guid, entry) in enumerate(new_entries):
            try:
                embed = build_embed(entry, feed_config)
            except Exception:
                result["failed"] += 1
                feed_results[feed_id]["status"] = "entry_invalid"
                fail("entry_invalid")
                continue
            try:
                send_to_discord(webhook_url, embed)
            except (requests.Timeout, requests.ConnectionError):
                result["failed"] += 1
                result["delivery_unknown"] += 1
                feed_results[feed_id]["status"] = "delivery_unknown"
                fail("delivery_unknown")
            except Exception:
                result["failed"] += 1
                feed_results[feed_id]["status"] = "delivery_failed"
                fail("delivery_failed")
            else:
                result["sent"] += 1
                feed_results[feed_id]["sent"] += 1
                sent_guids.add(guid)
                feed_state["sent_guids"] = sorted(sent_guids)
                feed_state["last_checked"] = datetime.now(timezone.utc).isoformat()
                try:
                    save_state(STATE_FILE, state)
                except Exception:
                    result["unpersisted_sent"] += 1
                    feed_results[feed_id]["status"] = "state_save_failed_after_send"
                    fail("state_save_failed_after_send")
                    return finish()
            if index < len(new_entries) - 1:
                time.sleep(feed_config["rate_limit_interval"])
    return finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and count only; never send or save state")
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
