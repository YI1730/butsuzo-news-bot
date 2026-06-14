"""Google News RSS から仏像関連ニュースを取得し docs/data/news.json に蓄積する。

X API は一切使用しない。投稿はダッシュボード（docs/index.html）から手動で行う。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time as time_module
import urllib.parse
from calendar import timegm
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:  # ライブラリ未インストール時でも HTTP フォールバックで動作する
    gnewsdecoder = None

RSS_BASE = "https://news.google.com/rss/search"
RSS_PARAMS = "hl=ja&gl=JP&ceid=JP:ja"

# 検索キーワードの設定ファイル（Streamlit から編集可能）
KEYWORDS_CONFIG_FILE = Path(__file__).parent / "config" / "news_keywords.json"

# 設定ファイルが存在しない／壊れている場合のフォールバック値
_FALLBACK_INCLUDE = [
    "仏像", "如来", "開帳", "開扉", "菩薩", "秘仏",
    "明王", "神像", "重要文化財", "木造", "木像",
]
_FALLBACK_EXCLUDE = [
    "グラビア", "返還", "ストリップ", "ヌード", "ギャンブル",
    "みほとけ", "クラブツーリズム", "賭博", "ゲーム",
]


def load_keywords() -> tuple[list[str], list[str]]:
    """config/news_keywords.json から含む/除外キーワードを読み込む。"""
    try:
        if KEYWORDS_CONFIG_FILE.exists():
            with KEYWORDS_CONFIG_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            inc = [k.strip() for k in data.get("include", []) if isinstance(k, str) and k.strip()]
            exc = [k.strip() for k in data.get("exclude", []) if isinstance(k, str) and k.strip()]
            if inc:  # 含むが空のときはフォールバックを使う（検索結果が膨大になるため）
                return inc, exc
            print("⚠ 含むキーワードが空のためフォールバックを使用", file=sys.stderr)
    except Exception as e:
        print(f"⚠ キーワード設定の読み込みに失敗: {e} → フォールバック使用", file=sys.stderr)
    return list(_FALLBACK_INCLUDE), list(_FALLBACK_EXCLUDE)


def build_search_query(includes: list[str], excludes: list[str]) -> str:
    """Google News RSS 用のクエリ文字列を組み立てる。"""
    inc_part = " OR ".join(includes)
    exc_part = " ".join(f"-{w}" for w in excludes)
    if exc_part:
        return f"({inc_part}) {exc_part}"
    return f"({inc_part})"


# モジュール読み込み時にキーワードを評価（GitHub Actions 実行時の値を固定）
INCLUDE_KEYWORDS, EXCLUDE_KEYWORDS = load_keywords()
SEARCH_QUERY = build_search_query(INCLUDE_KEYWORDS, EXCLUDE_KEYWORDS)

# 配信元ドメインによる除外リスト（仏像と無関係なサイトを排除）。
# Google News はキーワードが本文に含まれるだけの無関係サイトも拾うため、
# ここに列挙したドメイン（およびそのサブドメイン）からの記事は収集しない。
EXCLUDE_DOMAINS = [
    "ppid.semarangkota.go.id",
    "cfecgc-orange.org",
]

NEWS_JSON_FILE = Path(__file__).parent / "docs" / "data" / "news.json"
MAX_ITEMS_PER_RUN = 10   # 1回の実行で追加する最大件数
MAX_TOTAL_ITEMS = 500    # JSON に保持する最大件数（古いものは削除）
MAX_ARTICLE_AGE_DAYS = 14  # これより古い記事は収集しない

RESOLVE_TIMEOUT = 10
RESOLVE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def build_feed_url() -> str:
    query = urllib.parse.quote_plus(SEARCH_QUERY)
    return f"{RSS_BASE}?q={query}&{RSS_PARAMS}"


def load_news_data() -> dict:
    if not NEWS_JSON_FILE.exists():
        return {"last_updated": "", "items": []}
    with NEWS_JSON_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_news_data(data: dict) -> None:
    NEWS_JSON_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NEWS_JSON_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def item_id(url: str, title: str = "") -> str:
    """URL（+ タイトル）のハッシュから12文字の一意IDを生成"""
    return hashlib.md5((url + title).encode()).hexdigest()[:12]


def contains_excluded_keyword(text: str) -> bool:
    return any(keyword in text for keyword in EXCLUDE_KEYWORDS)


def is_excluded_domain(url: str) -> bool:
    """配信元ドメインが除外リストに該当するか判定する。

    Google News のキーワード検索では仏像系の語を含む無関係サイト
    （海外行政サイト・労組サイト等）が紛れ込むため、ドメイン単位で除外する。
    サブドメインも一致対象（例: news.example.com は example.com にマッチ）。
    """
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    for dom in EXCLUDE_DOMAINS:
        d = dom.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def parse_published_at(entry) -> datetime | None:
    """feedparser エントリから公開日時（aware datetime、JST）を取得する。

    feedparser は published_parsed を UTC の struct_time で返す。
    取得できない場合は None を返す。
    """
    pt = getattr(entry, "published_parsed", None)
    if pt is None:
        pt = getattr(entry, "updated_parsed", None)
    if pt is None:
        return None
    try:
        # struct_time (UTC) → timestamp → datetime (UTC) → JST
        ts = timegm(pt)
        return datetime.fromtimestamp(ts, tz=JST)
    except Exception:
        return None


def is_article_too_old(published: datetime | None) -> bool:
    """公開日が MAX_ARTICLE_AGE_DAYS 日より前なら True。published が None なら False（スキップしない）。"""
    if published is None:
        return False
    cutoff = datetime.now(JST) - timedelta(days=MAX_ARTICLE_AGE_DAYS)
    return published < cutoff


def extract_og_image(content: bytes) -> str:
    """HTML バイト列から OGP / Twitter Card 画像 URL を抽出する。"""
    try:
        soup = BeautifulSoup(content[:200_000], "html.parser")
        for prop in ("og:image", "twitter:image"):
            for attr in ("property", "name"):
                tag = soup.find("meta", attrs={attr: prop})
                if tag and tag.get("content", "").startswith("http"):
                    return tag["content"].strip()
    except Exception:
        pass
    return ""


def fetch_og_image(url: str) -> str:
    """指定 URL のページから OGP 画像 URL を取得する（失敗時は空文字）。"""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": RESOLVE_USER_AGENT},
            timeout=8,
        )
        return extract_og_image(resp.content)
    except Exception:
        return ""


def resolve_original_url(google_news_url: str) -> str:
    """Google News の RSS URL から配信元の最終 URL を取得する。"""
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

    return google_news_url


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    print(f"含むキーワード ({len(INCLUDE_KEYWORDS)}): {', '.join(INCLUDE_KEYWORDS)}")
    print(f"除外キーワード ({len(EXCLUDE_KEYWORDS)}): {', '.join(EXCLUDE_KEYWORDS)}")
    feed_url = build_feed_url()
    print(f"RSS取得: {feed_url}")
    feed = feedparser.parse(feed_url)

    if feed.bozo and not feed.entries:
        print(f"RSSの取得に失敗しました: {feed.bozo_exception}", file=sys.stderr)
        return 1

    data = load_news_data()
    existing_ids = {item["id"] for item in data["items"]}

    added_count = 0
    for entry in feed.entries:
        if added_count >= MAX_ITEMS_PER_RUN:
            break

        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        if not title or not link:
            continue

        if contains_excluded_keyword(title):
            print(f"除外キーワードを含むためスキップ: {title}")
            continue

        # 公開日チェック：30日より古い記事は除外
        published = parse_published_at(entry)
        if is_article_too_old(published):
            pub_str = published.strftime("%Y-%m-%d") if published else "不明"
            print(f"古い記事のためスキップ（{pub_str}）: {title}")
            continue

        original_url = resolve_original_url(link)

        # 配信元ドメインが除外リストに該当する記事はスキップ
        if is_excluded_domain(original_url):
            print(f"除外ドメインのためスキップ: {original_url}")
            continue

        uid = item_id(original_url)

        if uid in existing_ids:
            continue

        # 新規アイテムのみ OGP 画像を取得
        image_url = fetch_og_image(original_url)
        if image_url:
            print(f"  画像取得: {image_url[:60]}")

        published_at_str = published.isoformat() if published else ""

        data["items"].insert(0, {
            "id": uid,
            "title": title,
            "url": original_url,
            "source": "google_news",
            "header": "【仏像速報】",
            "hashtags": "#仏像 #仏像ニュース",
            "fetched_at": datetime.now(JST).isoformat(),
            "published_at": published_at_str,
            "image_url": image_url,
        })
        existing_ids.add(uid)
        added_count += 1
        print(f"追加: {title}")

    # 古いアイテムを削除して上限を守る
    data["items"] = data["items"][:MAX_TOTAL_ITEMS]
    data["last_updated"] = datetime.now(JST).isoformat()
    save_news_data(data)
    print(f"追加件数: {added_count} / 合計: {len(data['items'])}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
