"""Google News RSS から仏像関連ニュースを取得し docs/data/news.json に蓄積する。

X API は一切使用しない。投稿はダッシュボード（docs/index.html）から手動で行う。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time as time_module
import urllib.parse
from calendar import timegm
from datetime import datetime, timezone, timedelta
from pathlib import Path

from news_store import trim_per_source

import feedparser
import requests
from bs4 import BeautifulSoup

try:
    from googlenewsdecoder import gnewsdecoder
except Exception as _e:  # 依存の破壊的変更で import 自体が失敗することがある
    gnewsdecoder = None
    # 黙って HTTP フォールバックに落ちると、URL を一切解決できず全件スキップ
    # されても気づけない（2026-10-03 selectolax 1.0 で実際に発生）。
    # ::warning:: は GitHub Actions の実行サマリーに警告として表示される。
    print(f"::warning::googlenewsdecoder の読み込みに失敗: {_e}", file=sys.stderr)

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

# Google News RSS は 1 検索あたり最大 100 件で、しかも新着順ではなく関連度順に返す。
# 全キーワードを 1 本の OR 検索にすると、新着が後回しになったり 100 件の枠から
# こぼれたりする（実測: 配信から取り込みまで中央値 31 時間）。
# そこでキーワードを少数ずつに分けた複数の検索にし、各検索を直近に絞る。
KEYWORDS_PER_QUERY = 3
QUERY_RECENCY = "when:2d"


def build_search_queries(includes: list[str], excludes: list[str]) -> list[str]:
    """キーワードを KEYWORDS_PER_QUERY 個ずつに分けた検索クエリ群を返す。"""
    # 「国宝 (寺 OR 仏)」のように条件付きのキーワード（空白を含む）は、OR でつなぐと
    # 意味が変わるので 1 つで 1 検索にする
    simple = [k for k in includes if " " not in k]
    compound = [k for k in includes if " " in k]
    # （外側を括弧で包むと Google が件数を極端に絞るため、括弧なしで組み立てる）
    queries = [
        f"{build_search_query(simple[i:i + KEYWORDS_PER_QUERY], excludes)} {QUERY_RECENCY}"
        for i in range(0, len(simple), KEYWORDS_PER_QUERY)
    ]
    exc_part = " ".join(f"-{w}" for w in excludes)
    queries += [f"{k} {exc_part} {QUERY_RECENCY}".replace("  ", " ") for k in compound]
    return queries

# 配信元ドメインによる除外リスト（仏像と無関係なサイトを排除）。
# Google News はキーワードが本文に含まれるだけの無関係サイトも拾うため、
# ここに列挙したドメイン（およびそのサブドメイン）からの記事は収集しない。
EXCLUDE_DOMAINS = [
    "ppid.semarangkota.go.id",
    "cfecgc-orange.org",
]

NEWS_JSON_FILE = Path(__file__).parent / "docs" / "data" / "news.json"
# Google News の記事 ID → 解決済み URL のキャッシュ。毎時実行しても
# 同じ記事を何度もデコードしない（Google への負荷と実行時間を抑える）。
URL_CACHE_FILE = Path(__file__).parent / "data" / "gnews_url_cache.json"
URL_CACHE_MAX = 3000     # キャッシュの保持件数
MAX_ITEMS_PER_RUN = 50   # 1回の実行で追加する最大件数
MAX_ARTICLE_AGE_DAYS = 3   # 速報として扱う期間。これより古い記事は収集しない

RESOLVE_TIMEOUT = 10
RESOLVE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def build_feed_url(query: str = SEARCH_QUERY) -> str:
    return f"{RSS_BASE}?q={urllib.parse.quote_plus(query)}&{RSS_PARAMS}"


def load_url_cache() -> dict[str, str]:
    try:
        with URL_CACHE_FILE.open("r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_url_cache(cache: dict[str, str]) -> None:
    # dict は挿入順を保つので、末尾（新しい方）から URL_CACHE_MAX 件を残す
    items = list(cache.items())[-URL_CACHE_MAX:]
    URL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with URL_CACHE_FILE.open("w", encoding="utf-8") as f:
        json.dump(dict(items), f, ensure_ascii=False, indent=0)


def fetch_entries(queries: list[str]) -> list:
    """全クエリの RSS を取得し、記事 ID で重複を除いて新しい順に並べる。"""
    seen: set[str] = set()
    entries = []
    for q in queries:
        feed = feedparser.parse(build_feed_url(q))
        if feed.bozo and not feed.entries:
            print(f"::warning::RSS取得失敗: {q[:40]} ({feed.bozo_exception})", file=sys.stderr)
            continue
        for e in feed.entries:
            key = getattr(e, "id", "") or getattr(e, "link", "")
            if key and key not in seen:
                seen.add(key)
                entries.append(e)
        time_module.sleep(1)  # 連続リクエストを避ける
    entries.sort(
        key=lambda e: parse_published_at(e) or datetime.min.replace(tzinfo=JST),
        reverse=True,
    )
    return entries


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


_ARTICLE_URL_RULES = [
    # Google News は写真ギャラリーのページ URL を返すことがある。本文が無く（AI が読めない）、
    # 同じ記事が写真ごとに別記事として重複登録されるので、記事本体の URL に直す。
    (re.compile(r"^(https://news\.yahoo\.co\.jp/articles/[0-9a-f]+)/images/\d+/?$"), r"\1"),
    (re.compile(r"^https://news\.livedoor\.com/article/image_detail/(\d+)/.*$"),
     r"https://news.livedoor.com/article/detail/\1/"),
    (re.compile(r"^https://natalie\.mu/(\w+)/gallery/news/(\d+)/\d+/?$"), r"https://natalie.mu/\1/news/\2"),
]


def normalize_article_url(url: str) -> str:
    """写真ページ等の URL を記事本体の URL に直し、計測用の utm_* パラメータを除く。"""
    for pattern, repl in _ARTICLE_URL_RULES:
        if pattern.match(url):
            url = pattern.sub(repl, url)
            break
    parts = urllib.parse.urlsplit(url)
    if "utm_" in parts.query:
        q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not k.startswith("utm_")]
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))
    return url


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


def extract_og_description(content: bytes) -> str:
    """HTML バイト列から記事の要約（og:description / description）を抽出する。"""
    try:
        soup = BeautifulSoup(content[:200_000], "html.parser")
        for prop in ("og:description", "description", "twitter:description"):
            for attr in ("property", "name"):
                tag = soup.find("meta", attrs={attr: prop})
                if tag and tag.get("content", "").strip():
                    return " ".join(tag["content"].split())[:400]
    except Exception:
        pass
    return ""


def fetch_page_meta(url: str) -> tuple[str, str]:
    """記事ページから (OGP画像URL, 要約) を1回のリクエストで取得する。失敗時は空文字。"""
    try:
        resp = requests.get(url, headers={"User-Agent": RESOLVE_USER_AGENT}, timeout=8)
        return extract_og_image(resp.content), extract_og_description(resp.content)
    except Exception:
        return "", ""


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
            # 成功フラグのキーはバージョンで異なる:
            #   v0.1.x → "status" / v0.2.x 以降 → "success"
            # どちらでも動くよう両方を見る（片方しか無い版でも取りこぼさない）。
            ok = decoded.get("success")
            if ok is None:
                ok = decoded.get("status")
            if ok and decoded.get("decoded_url"):
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
    queries = build_search_queries(INCLUDE_KEYWORDS, EXCLUDE_KEYWORDS)
    entries = fetch_entries(queries)
    print(f"RSS取得: {len(queries)}クエリ / 重複除去後 {len(entries)}件")
    if not entries:
        # Google 側の一時的な制限などで全クエリが失敗することがある。ここで異常終了すると
        # 後続の AI 採点・返信の仕分けまで止まるため、警告にとどめて正常終了する
        print("::warning::RSSの取得に失敗しました（全クエリで0件）。次回の実行で再取得します",
              file=sys.stderr)
        return 0

    data = load_news_data()
    existing_ids = {item["id"] for item in data["items"]}
    url_cache = load_url_cache()

    added_count = 0
    unresolved_count = 0
    for entry in entries:
        if added_count >= MAX_ITEMS_PER_RUN:
            break

        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        if not title or not link:
            continue

        if contains_excluded_keyword(title):
            print(f"除外キーワードを含むためスキップ: {title}")
            continue

        # 公開日チェック：MAX_ARTICLE_AGE_DAYS より古い記事は除外
        published = parse_published_at(entry)
        if is_article_too_old(published):
            pub_str = published.strftime("%Y-%m-%d") if published else "不明"
            print(f"古い記事のためスキップ（{pub_str}）: {title}")
            continue

        cache_key = getattr(entry, "id", "") or link
        original_url = url_cache.get(cache_key) or resolve_original_url(link)
        if not original_url.startswith("https://news.google.com"):
            url_cache[cache_key] = original_url

        # URL を解決できなかった記事は保存しない。
        # Google News の RSS URL のまま保存すると、ダッシュボードの投稿リンクが
        # 元記事ではなく RSS URL になり、OGP 画像も取得できないため。
        # スキップしても次回以降の実行で再取得される（RSS に残っている限り）。
        if original_url.startswith("https://news.google.com"):
            print(f"URL解決に失敗したためスキップ: {title}", file=sys.stderr)
            unresolved_count += 1
            continue

        original_url = normalize_article_url(original_url)

        # 配信元ドメインが除外リストに該当する記事はスキップ
        if is_excluded_domain(original_url):
            print(f"除外ドメインのためスキップ: {original_url}")
            continue

        uid = item_id(original_url)

        if uid in existing_ids:
            continue

        # 新規アイテムのみ OGP 画像を取得
        image_url, description = fetch_page_meta(original_url)
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
            # AI 採点（curate_news.py）の判断材料。ダッシュボードの投稿文にも使われる
            "description": description,
        })
        existing_ids.add(uid)
        added_count += 1
        print(f"追加: {title}")

    # 古いアイテムを削除して上限を守る
    data["items"] = trim_per_source(data["items"])
    data["last_updated"] = datetime.now(JST).isoformat()
    save_news_data(data)
    save_url_cache(url_cache)
    print(f"追加件数: {added_count} / 合計: {len(data['items'])}件")
    if unresolved_count:
        print(
            f"::warning::Google News の URL 解決に {unresolved_count} 件失敗"
            "（googlenewsdecoder の不具合・仕様変更の可能性）",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
