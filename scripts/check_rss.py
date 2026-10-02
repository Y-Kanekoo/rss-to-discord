"""RSSフィードの新着記事をDiscord Webhookに送信するスクリプト"""

import argparse
import json
import math
import os
import sys
import time
import tempfile
from pathlib import Path
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
    return state


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
    feed = feedparser.parse(url)
    if (feed.get("status", 200) >= 400 or not feed.get("version")
            or (feed.bozo and not feed.entries)):
        raise RuntimeError("feed_unavailable_or_invalid")
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

    def fail(code: str) -> None:
        if code not in result["errors"]:
            result["errors"].append(code)
        # Never print exception text, URLs, response bodies, titles or GUIDs.
        print(f"エラー: {code}", file=sys.stderr)

    def finish() -> int:
        failed = bool(result["errors"])
        result["status"] = "failed" if failed else ("dry_run" if dry_run else "ok")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if failed else 0

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not dry_run and not webhook_url:
        fail("missing_webhook")
        return finish()
    try:
        state = load_state(STATE_FILE)
    except StateError:
        fail("state_unreadable_or_invalid")
        return finish()
    sent_guids = set(state["sent_guids"])
    is_first_run = state["last_checked"] is None

    try:
        feed = fetch_feed(RSS_URL)
        # Deduplicate before selection, including failed IDs, so a feed cannot
        # cause an immediate retry of an unknown delivery in the same run.
        seen = set(sent_guids)
        new_entries = []
        for entry in feed.entries:
            guid = entry.get("id") or entry.get("link")
            if not isinstance(guid, str) or not guid:
                result["failed"] += 1
                fail("entry_invalid")
                continue
            if guid not in seen:
                seen.add(guid)
                new_entries.append((guid, entry))
        new_entries.sort(key=lambda item: item[1].get("published_parsed", ()))
    except Exception:
        fail("feed_unavailable_or_invalid")
        return finish()

    if is_first_run and len(new_entries) > MAX_ARTICLES_FIRST_RUN:
        # Keep the existing first-run latest-five policy. Record the skipped
        # backlog before sending, so failures are not skipped on the next run.
        sent_guids.update(guid for guid, _ in new_entries[:-MAX_ARTICLES_FIRST_RUN])
        new_entries = new_entries[-MAX_ARTICLES_FIRST_RUN:]
    result["selected"] = len(new_entries)
    if dry_run:
        return finish()

    state["sent_guids"] = sorted(sent_guids)
    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    try:
        # Prove state is writable before any irreversible external send.
        save_state(STATE_FILE, state)
    except Exception:
        fail("state_save_failed")
        return finish()

    for index, (guid, entry) in enumerate(new_entries):
        try:
            embed = build_embed(entry)
        except Exception:
            result["failed"] += 1
            fail("entry_invalid")
            continue
        try:
            send_to_discord(webhook_url, embed)
        except (requests.Timeout, requests.ConnectionError):
            # The server may have accepted the message. Leave it retryable, but
            # make ambiguity explicit: Discord has no idempotency key here.
            result["failed"] += 1
            result["delivery_unknown"] += 1
            fail("delivery_unknown")
        except Exception:
            result["failed"] += 1
            fail("delivery_failed")
        else:
            result["sent"] += 1
            sent_guids.add(guid)
            state["sent_guids"] = sorted(sent_guids)
            try:
                save_state(STATE_FILE, state)
            except Exception:
                result["unpersisted_sent"] += 1
                fail("state_save_failed_after_send")
                break
        if index < len(new_entries) - 1:
            time.sleep(RATE_LIMIT_INTERVAL)
    return finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and count only; never send or save state")
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
