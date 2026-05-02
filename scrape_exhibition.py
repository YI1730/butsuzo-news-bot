"""仏像関連の特別展・展覧会情報を収集し docs/data/news.json に蓄積する。

X API は一切使用しない。投稿はダッシュボード（docs/index.html）から手動で行う。

ターゲット:
1. RSS フィードリスト（Google Alert 等を追加可能）
2. PR TIMES「仏像」トピックページ（直接スクレイピング）
3. インターネットミュージアム museum.or.jp（全件取得＋タイトルフィルタ）
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

from calendar import timegm

import feedparser
import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15
SLEEP_BETWEEN_REQUESTS = 3

NEWS_JSON_FILE = Path(__file__).parent / "docs" / "data" / "news.json"
MAX_TOTAL_ITEMS = 500
MAX_ARTICLE_AGE_DAYS = 30  # これより古い記事は収集しない

JST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------------------
# 設定 — RSSリストは URL を追記するだけで拡張できる
# ---------------------------------------------------------------------------

# RSS フィードリスト（Google Alert の RSS URL 等を追加可能）
# 例: "https://www.google.com/alerts/feeds/<ID>/<TOKEN>"
RSS_FEED_URLS: list[str] = [
    # PR TIMES の仏像 RSS は現在 404 のためスクレイピングで代替
    # Google Alert RSS を追加する場合はここに URL を記載:
    # "https://www.google.com/alerts/feeds/XXXXX/YYYYY",
]

# 関連キーワード（いずれか 1 つが含まれれば収集対象）
RELEVANT_KEYWORDS = [
    "仏像", "如来", "菩薩", "観音", "明王", "天部", "羅漢",
    "秘仏", "開帳", "開扉", "特別公開", "御開帳", "本尊", "曼荼羅",
    "仏教美術", "大仏",
]

# ミュージアムサイト向け追加キーワード（より広め）
MUSEUM_KEYWORDS = RELEVANT_KEYWORDS + [
    "空海", "法隆寺", "正倉院", "真言", "天台", "禅", "浄土",
    "奈良時代", "平安時代", "鎌倉時代", "国宝",
]

EXCLUDE_KEYWORDS = [
    "グラビア", "ストリップ", "ヌード", "ギャンブル",
    "クラブツーリズム", "賭博", "ゲーム",
]

SOURCE_EXHIBITION = "exhibition"

# ---------------------------------------------------------------------------
# JSON ユーティリティ
# ---------------------------------------------------------------------------


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
    """URL（+ タイトル）の MD5 ハッシュから 12 文字の一意 ID を生成。"""
    return hashlib.md5((url + title).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# HTTP ユーティリティ
# ---------------------------------------------------------------------------


def decode_html(content: bytes) -> str:
    for encoding in ("utf-8", "cp932", "euc_jp"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def fetch_html(url: str) -> str | None:
    try:
        resp = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return decode_html(resp.content)
    except requests.RequestException as e:
        print(f"取得失敗 {url}: {e}", file=sys.stderr)
        return None


def fetch_og_image(url: str) -> str:
    """指定 URL の OGP / Twitter Card 画像 URL を返す（失敗時は空文字）。"""
    try:
        resp = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=8
        )
        soup = BeautifulSoup(resp.content[:200_000], "html.parser")
        for prop in ("og:image", "twitter:image"):
            for attr in ("property", "name"):
                tag = soup.find("meta", attrs={attr: prop})
                if tag and tag.get("content", "").startswith("http"):
                    return tag["content"].strip()
    except Exception:
        pass
    return ""


def is_relevant(text: str, keywords: list[str] | None = None) -> bool:
    kws = keywords if keywords is not None else RELEVANT_KEYWORDS
    if not text:
        return False
    if any(kw in text for kw in EXCLUDE_KEYWORDS):
        return False
    return any(kw in text for kw in kws)


def is_excluded(text: str) -> bool:
    return any(kw in text for kw in EXCLUDE_KEYWORDS)


def parse_prtimes_date(html: str) -> datetime | None:
    """PR TIMES 記事ページから公開日時を抽出する（JST aware datetime）。

    ページ内の time[datetime] 属性、または「YYYY年MM月DD日 HH時MM分」テキストを探す。
    """
    try:
        soup = BeautifulSoup(html[:200_000], "html.parser")
        # 1) <time datetime="2026-04-24T12:05:00+09:00"> 形式
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            dt_str = time_tag["datetime"]
            # ISO 8601 パース
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(dt_str[:25], fmt[:len(dt_str[:25])])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=JST)
                    return dt.astimezone(JST)
                except ValueError:
                    continue
        # 2) 「YYYY年MM月DD日 HH時MM分」テキスト
        text = soup.get_text()
        m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})時(\d{2})分", text)
        if m:
            y, mo, d, h, mi = (int(x) for x in m.groups())
            return datetime(y, mo, d, h, mi, tzinfo=JST)
    except Exception:
        pass
    return None


def is_article_too_old(published: datetime | None) -> bool:
    """公開日が MAX_ARTICLE_AGE_DAYS 日より前なら True。None なら False（スキップしない）。"""
    if published is None:
        return False
    cutoff = datetime.now(JST) - timedelta(days=MAX_ARTICLE_AGE_DAYS)
    return published < cutoff


# ---------------------------------------------------------------------------
# スクレイパー
# ---------------------------------------------------------------------------


def scrape_rss_feeds() -> list[dict]:
    """RSS フィードリストから仏像関連の特別展情報を取得。

    RSS_FEED_URLS にリストされた URL を順に読み込む。
    Google Alert 等の RSS URL をリストに追記するだけで拡張可能。
    """
    if not RSS_FEED_URLS:
        print("  RSS URLリストが空のためスキップ")
        return []

    items: list[dict] = []
    for rss_url in RSS_FEED_URLS:
        try:
            feed = feedparser.parse(rss_url)
            if not feed.entries:
                print(f"  エントリなし: {rss_url}", file=sys.stderr)
                continue
            for entry in feed.entries[:30]:
                title = getattr(entry, "title", "").strip()
                link  = getattr(entry, "link", "").strip()
                summary = getattr(entry, "summary", "")
                if not (title and link):
                    continue
                if not is_relevant(f"{title} {summary}"):
                    continue
                if is_excluded(title):
                    continue
                items.append({
                    "title": title,
                    "url": link,
                    "source": SOURCE_EXHIBITION,
                    "header": "【仏像特別展情報】",
                    "hashtags": "#仏像 #特別展 #展覧会",
                })
        except Exception as e:
            print(f"  RSS取得失敗 {rss_url}: {e}", file=sys.stderr)

    return items


def scrape_prtimes() -> list[dict]:
    """PR TIMES「仏像」トピックページからプレスリリースを取得。

    PR TIMES のキーワード RSS は現在 404 のため、HTML ページを直接スクレイピング。
    /main/html/rd/p/*.html 形式のリンクを記事 URL として扱う。
    """
    url = "https://prtimes.jp/topics/keywords/%E4%BB%8F%E5%83%8F/"
    html = fetch_html(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/main/html/rd/p/" not in href:
            continue
        full_url = urljoin("https://prtimes.jp", href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        # タイトル取得: a タグ直下 → 近隣 heading タグを探索
        title = a.get_text(strip=True)
        if len(title) < 10:
            for ancestor in list(a.parents)[:5]:
                h = ancestor.find(["h2", "h3", "h4", "p"])
                if h and len(h.get_text(strip=True)) > 10:
                    title = h.get_text(strip=True)
                    break

        title = unicodedata.normalize("NFKC", title).strip()
        if not title or len(title) < 5:
            continue
        if is_excluded(title):
            continue

        items.append({
            "title": title,
            "url": full_url,
            "source": SOURCE_EXHIBITION,
            "header": "【仏像特別展情報】",
            "hashtags": "#仏像 #特別展 #展覧会",
            # image_url は main() で OGP fetch
        })

    return items


def scrape_museum_or_jp(max_pages: int = 5) -> list[dict]:
    """インターネットミュージアム（museum.or.jp）から仏像関連展示を取得。

    NOTE: museum.or.jp の検索はクライアントサイドで行われるため、
    サーバーレスポンスにはキーワードフィルタが反映されない。
    そのため全件取得してタイトル・会場名で仏像関連キーワードをフィルタする。

    max_pages: 取得する最大ページ数（1 ページ 20 件）
    """
    base_url = "https://www.museum.or.jp/event"
    items: list[dict] = []
    seen_urls: set[str] = set()

    for page in range(1, max_pages + 1):
        try:
            html = fetch_html(f"{base_url}?per_page=20&page={page}")
            if not html:
                break

            soup = BeautifulSoup(html, "html.parser")
            event_items = soup.find_all(class_="c-eventItem")
            if not event_items:
                break  # ページ終端

            for item in event_items:
                a = item.find("a", href=True)
                if not a:
                    continue
                full_url = urljoin("https://www.museum.or.jp", a["href"])
                if full_url in seen_urls:
                    continue

                # タイトル＋会場名（blockExpand には "数字+タイトル+会場名" が入る）
                block = item.find(class_="c-eventItem_blockExpand")
                if not block:
                    continue
                raw = re.sub(r"^\d+\s*", "", block.get_text(" ", strip=True))
                raw = unicodedata.normalize("NFKC", raw).strip()

                # 仏像関連キーワードフィルタ（MUSEUM_KEYWORDS = 広めのリスト）
                if not is_relevant(raw, MUSEUM_KEYWORDS):
                    continue
                if is_excluded(raw):
                    continue

                seen_urls.add(full_url)

                # 開催期間: c-eventItem_duration クラスの要素から取得
                # blockFixed には「開催中[あと○日]」「開催まであと○日」等の
                # 動的テキストも含まれるため、duration クラスを優先する
                dur_el = item.find(class_="c-eventItem_duration")
                duration = ""
                if dur_el:
                    duration = unicodedata.normalize(
                        "NFKC", dur_el.get_text(strip=True)
                    )
                    # 曜日表記（Mo）(Tu) 等を除去
                    duration = re.sub(r"\([A-Za-z]{2}\)", "", duration).strip()
                else:
                    dur_block = item.find(class_="c-eventItem_blockFixed")
                    if dur_block:
                        raw_dur = dur_block.get_text(strip=True)
                        # 日付範囲のみ抽出（例: 2026年4月19日〜6月21日）
                        m_dur = re.search(
                            r"\d+年\d+月\d+日[〜～~]\d+月?\d*日?", raw_dur
                        )
                        duration = unicodedata.normalize(
                            "NFKC", m_dur.group(0) if m_dur else ""
                        )

                # サムネイル画像（noimage 以外）
                image_url = ""
                img = item.find("img")
                if img:
                    src = img.get("src", "").strip()
                    if src and "noimage" not in src.lower() and src.startswith("http"):
                        image_url = src

                full_title = f"{raw}（{duration}）" if duration else raw

                items.append({
                    "title": full_title,
                    "url": full_url,
                    "source": SOURCE_EXHIBITION,
                    "header": "【仏像特別展情報】",
                    "hashtags": "#仏像 #特別展 #展覧会",
                    "image_url": image_url,
                })

            time.sleep(SLEEP_BETWEEN_REQUESTS)

        except Exception as e:
            print(f"  museum.or.jp ページ {page} 取得失敗: {e}", file=sys.stderr)
            break

    return items


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    data = load_news_data()
    existing_ids = {item["id"] for item in data["items"]}

    all_items: list[dict] = []

    print("[1/3] RSSフィードを取得中...")
    try:
        rss_items = scrape_rss_feeds()
        all_items.extend(rss_items)
        print(f"  RSSアイテム数: {len(rss_items)}")
    except Exception as e:
        print(f"RSS取得失敗: {e}", file=sys.stderr)
    time.sleep(SLEEP_BETWEEN_REQUESTS)

    print("[2/3] PR TIMES（仏像トピック）を取得中...")
    try:
        pt_items = scrape_prtimes()
        all_items.extend(pt_items)
        print(f"  PR TIMESアイテム数: {len(pt_items)}")
    except Exception as e:
        print(f"PR TIMESスクレイピング失敗: {e}", file=sys.stderr)
    time.sleep(SLEEP_BETWEEN_REQUESTS)

    print("[3/3] インターネットミュージアムを取得中...")
    try:
        museum_items = scrape_museum_or_jp()
        all_items.extend(museum_items)
        print(f"  museum.or.jpアイテム数: {len(museum_items)}")
    except Exception as e:
        print(f"museum.or.jpスクレイピング失敗: {e}", file=sys.stderr)

    print(f"取得アイテム合計: {len(all_items)}")

    added_count = 0
    for item in all_items:
        uid = item_id(item.get("url", ""), item["title"])
        if uid in existing_ids:
            continue
        if is_excluded(item["title"]):
            continue

        item["id"] = uid
        item["fetched_at"] = datetime.now(JST).isoformat()

        # image_url が未設定の場合: ページを取得して OGP + 公開日を同時に取得
        if "image_url" not in item:
            url = item.get("url", "")
            if url:
                html_content = fetch_html(url)
                if html_content:
                    # OGP 画像
                    soup_tmp = BeautifulSoup(html_content[:200_000], "html.parser")
                    image_url = ""
                    for prop in ("og:image", "twitter:image"):
                        for attr in ("property", "name"):
                            tag = soup_tmp.find("meta", attrs={attr: prop})
                            if tag and tag.get("content", "").startswith("http"):
                                image_url = tag["content"].strip()
                                break
                        if image_url:
                            break
                    item["image_url"] = image_url
                    if image_url:
                        print(f"  画像取得: {image_url[:60]}")

                    # 公開日（PR TIMES など）
                    if "published_at" not in item:
                        pub_dt = parse_prtimes_date(html_content)
                        if pub_dt and is_article_too_old(pub_dt):
                            pub_str = pub_dt.strftime("%Y-%m-%d")
                            print(f"古い記事のためスキップ（{pub_str}）: {item['title'][:60]}")
                            continue
                        item["published_at"] = pub_dt.isoformat() if pub_dt else ""
                else:
                    item["image_url"] = ""
                    item.setdefault("published_at", "")
            else:
                item["image_url"] = ""
                item.setdefault("published_at", "")
        else:
            # image_url 事前取得済み（museum.or.jp 等）
            if item.get("image_url"):
                print(f"  画像（事前取得）: {item['image_url'][:60]}")
            item.setdefault("published_at", "")

        data["items"].insert(0, item)
        existing_ids.add(uid)
        added_count += 1
        print(f"追加: [{item['source']}] {item['title'][:70]}")

    data["items"] = data["items"][:MAX_TOTAL_ITEMS]
    data["last_updated"] = datetime.now(JST).isoformat()
    save_news_data(data)
    print(f"追加件数: {added_count} / 合計: {len(data['items'])}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
