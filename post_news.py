import os
import sys
import urllib.parse
from pathlib import Path

import feedparser
import tweepy

SEARCH_QUERY = (
    "(仏像 OR 如来 OR 開帳 OR 開扉 OR 菩薩 OR 秘仏) "
    "-グラビア -返還 -ストリップ -ヌード -ギャンブル "
    "-クラブツーリズム -賭博 -リアルタイム -ゲーム"
)
RSS_BASE = "https://news.google.com/rss/search"
RSS_PARAMS = "hl=ja&gl=JP&ceid=JP:ja"

EXCLUDE_KEYWORDS = [
    "グラビア",
    "返還",
    "ストリップ",
    "ヌード",
    "ギャンブル",
    "クラブツーリズム",
    "賭博",
    "リアルタイム",
    "ゲーム",
]

POSTED_URLS_FILE = Path(__file__).parent / "posted_urls.txt"
MAX_POSTS_PER_RUN = 3


def build_feed_url() -> str:
    query = urllib.parse.quote_plus(SEARCH_QUERY)
    return f"{RSS_BASE}?q={query}&{RSS_PARAMS}"


def load_posted_urls() -> set[str]:
    if not POSTED_URLS_FILE.exists():
        return set()
    with POSTED_URLS_FILE.open("r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_posted_url(url: str) -> None:
    with POSTED_URLS_FILE.open("a", encoding="utf-8") as f:
        f.write(url + "\n")


def contains_excluded_keyword(text: str) -> bool:
    return any(keyword in text for keyword in EXCLUDE_KEYWORDS)


def build_tweet(title: str, url: str) -> str:
    header = "【仏像速報】"
    hashtags = "#仏像 #仏像ニュース"
    # Xの文字数制限（280文字）に収まるようにタイトルを必要なら切り詰める
    # URLは t.co により 23 文字扱いになるが、安全マージンを取って実測で計算
    reserved = len(header) + 1 + 1 + len(url) + 1 + len(hashtags)
    max_title_len = 280 - reserved
    if len(title) > max_title_len and max_title_len > 1:
        title = title[: max_title_len - 1] + "…"
    return f"{header}\n{title}\n{url}\n{hashtags}"


def get_twitter_client() -> tweepy.Client:
    required = [
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"必須の環境変数が未設定です: {', '.join(missing)}")

    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )


def main() -> int:
    feed_url = build_feed_url()
    print(f"RSS取得: {feed_url}")
    feed = feedparser.parse(feed_url)

    if feed.bozo and not feed.entries:
        print(f"RSSの取得に失敗しました: {feed.bozo_exception}", file=sys.stderr)
        return 1

    posted_urls = load_posted_urls()
    client = get_twitter_client()

    posted_count = 0
    for entry in feed.entries:
        if posted_count >= MAX_POSTS_PER_RUN:
            break

        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        if not title or not link:
            continue

        if link in posted_urls:
            continue

        if contains_excluded_keyword(title):
            print(f"除外キーワードを含むためスキップ: {title}")
            continue

        tweet_text = build_tweet(title, link)
        try:
            client.create_tweet(text=tweet_text)
        except tweepy.TweepyException as e:
            print(f"投稿失敗: {title} ({e})", file=sys.stderr)
            continue

        append_posted_url(link)
        posted_urls.add(link)
        posted_count += 1
        print(f"投稿成功: {title}")

    print(f"投稿数: {posted_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
