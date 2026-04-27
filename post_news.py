import os
import sys
import urllib.parse
from pathlib import Path

import feedparser
import requests
import tweepy

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:  # 保険: ライブラリ未インストール時でもHTTPフォールバックで動作する
    gnewsdecoder = None

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

RESOLVE_TIMEOUT = 10  # 秒
RESOLVE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


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


def resolve_original_url(google_news_url: str) -> str:
    """GoogleニュースのRSS URLから最終的な配信元メディアのURLを返す。
    googlenewsdecoder で base64 エンコードされたIDをデコードして取得。
    失敗した場合は HTTP リダイレクト追跡 → 元URLの順にフォールバック。
    """
    # 1. googlenewsdecoder でデコード（現行のGoogleニュースURL形式に対応）
    if gnewsdecoder is not None:
        try:
            decoded = gnewsdecoder(google_news_url, interval=1)
            if decoded.get("status") and decoded.get("decoded_url"):
                return decoded["decoded_url"]
            print(
                f"gnewsdecoderでデコード不可: {decoded.get('message')}",
                file=sys.stderr,
            )
        except Exception as e:
            print(f"gnewsdecoder例外: {e}", file=sys.stderr)

    # 2. HTTPリダイレクト追跡（旧形式URL向けフォールバック）
    try:
        response = requests.get(
            google_news_url,
            headers={"User-Agent": RESOLVE_USER_AGENT},
            timeout=RESOLVE_TIMEOUT,
            allow_redirects=True,
        )
        final_url = response.url
        if final_url and not final_url.startswith("https://news.google.com"):
            return final_url
    except requests.RequestException as e:
        print(f"HTTP展開失敗: {e}", file=sys.stderr)

    # 3. すべて失敗した場合は元のURLを返す
    print(f"URL展開失敗、元のURLを使用: {google_news_url}", file=sys.stderr)
    return google_news_url


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

        original_url = resolve_original_url(link)
        # 重複判定は展開後URLでも行う
        if original_url != link and original_url in posted_urls:
            append_posted_url(link)
            posted_urls.add(link)
            continue

        tweet_text = build_tweet(title, original_url)
        try:
            client.create_tweet(text=tweet_text)
        except tweepy.TweepyException as e:
            err_str = str(e)
            print(f"投稿失敗: {title} ({err_str})", file=sys.stderr)
            if any(code in err_str for code in ("429", "Too Many Requests", "402", "Payment Required", "403", "Forbidden")):
                print(f"致命的なAPIエラーを検出。ループを中断します: {err_str}", file=sys.stderr)
                break
            continue

        append_posted_url(link)
        posted_urls.add(link)
        if original_url != link:
            append_posted_url(original_url)
            posted_urls.add(original_url)
        posted_count += 1
        print(f"投稿成功: {title} → {original_url}")

    print(f"投稿数: {posted_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
