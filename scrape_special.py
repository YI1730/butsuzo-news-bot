"""仏像専門サイトと国立博物館の特別公開情報をスクレイピングして
docs/data/news.json に蓄積する。

X API は一切使用しない。投稿はダッシュボード（docs/index.html）から手動で行う。

ターゲット:
1. 観仏三昧（仏像の公開情報）
2. 国立博物館（東京・奈良・京都・九州）のRSS
3. 京都非公開文化財特別公開
4. 祈りの回廊（奈良県秘宝・秘仏特別開帳）
"""

import hashlib
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup, NavigableString

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15
SLEEP_BETWEEN_REQUESTS = 3  # 秒

NEWS_JSON_FILE = Path(__file__).parent / "docs" / "data" / "news.json"
MAX_TOTAL_ITEMS = 500  # JSON に保持する最大件数

JST = timezone(timedelta(hours=9))

# 国立博物館の更新情報フィルタ用
RELEVANT_KEYWORDS = [
    "仏像", "如来", "菩薩", "観音", "明王", "天部", "羅漢",
    "秘仏", "開帳", "開扉", "特別公開", "特別展", "御開帳", "本尊", "曼荼羅",
]

EXCLUDE_KEYWORDS = [
    "グラビア", "ストリップ", "ヌード", "ギャンブル",
    "クラブツーリズム", "賭博", "ゲーム",
]


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
    """URL（+ タイトル）のハッシュから12文字の一意IDを生成"""
    return hashlib.md5((url + title).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# HTTP ユーティリティ
# ---------------------------------------------------------------------------


def decode_html(content: bytes) -> str:
    """日本語サイト向けの堅牢な文字コードデコード。

    UTF-8 → Shift-JIS(CP932) → EUC-JP の順に厳格モードで試行し、
    最初に成功したエンコーディングでデコードした文字列を返す。
    chardet が Shift-JIS を GBK 等として誤検出する問題を回避する。
    """
    for encoding in ("utf-8", "cp932", "euc_jp"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def fetch_html(url: str) -> str | None:
    try:
        response = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return decode_html(response.content)
    except requests.RequestException as e:
        print(f"取得失敗 {url}: {e}", file=sys.stderr)
        return None


def fetch_og_image(url: str) -> str:
    """指定 URL のページから OGP / Twitter Card 画像 URL を取得する（失敗時は空文字）。"""
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


def is_relevant(text: str) -> bool:
    if not text:
        return False
    if any(kw in text for kw in EXCLUDE_KEYWORDS):
        return False
    return any(kw in text for kw in RELEVANT_KEYWORDS)


def is_excluded(text: str) -> bool:
    return any(kw in text for kw in EXCLUDE_KEYWORDS)


# ---------------------------------------------------------------------------
# 各ターゲットのスクレイパー
# ---------------------------------------------------------------------------

# 公開期間が終了したイベントは紫系の色で表記される
KANBUTSU_ENDED_COLORS = {"#191970", "#7b68ee", "#0000ff"}
# 全角・半角の数字を含む日付パターン
KANBUTSU_DATE_RE = re.compile(
    r"[０-９0-9]+月[０-９0-9]+日(?:[〜～~][０-９0-9]+月[０-９0-9]+日)?"
)


def scrape_kanbutsuzanmai() -> list[dict]:
    """仏像の公開情報ページから個別イベント（寺院・展示名・期間・公式URL）を抽出。"""
    source_url = "http://www.kanbutuzanmai.com/butsuzoukoukai.html"
    html = fetch_html(source_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    seen_keys: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith(("http://", "https://")):
            continue
        if "kanbutuzanmai" in href.lower():
            continue

        # 終了済みエントリ（紫系の色）はスキップ
        ended = False
        for ancestor in a.parents:
            if ancestor.name == "font":
                color = (ancestor.get("color") or "").lower()
                if color in KANBUTSU_ENDED_COLORS:
                    ended = True
                    break
        if ended:
            continue

        temple = a.get_text(" ", strip=True)
        if not temple:
            continue

        # 直前のテキストから日付を抽出
        prev_text = ""
        for sib in a.previous_siblings:
            if getattr(sib, "name", None) == "br":
                break
            if isinstance(sib, NavigableString):
                prev_text = str(sib) + prev_text
            elif hasattr(sib, "get_text"):
                prev_text = sib.get_text() + prev_text
            if len(prev_text) > 100:
                break
        m = KANBUTSU_DATE_RE.search(prev_text)
        # NFKC正規化で全角数字を半角に統一（例：５月１日 → 5月1日）
        date_range = unicodedata.normalize("NFKC", m.group(0)) if m else ""

        # 直後のテキストから展示名を抽出
        title_text = ""
        for sib in a.next_siblings:
            if getattr(sib, "name", None) in ("br", "a"):
                break
            if isinstance(sib, NavigableString):
                title_text += str(sib)
            elif hasattr(sib, "get_text"):
                title_text += sib.get_text()
            if len(title_text) > 300:
                break
        title_text = title_text.strip()

        body_parts: list[str] = [temple]
        if title_text:
            body_parts.append(title_text)
        body = " ".join(body_parts).strip()
        if date_range:
            body = f"{body}（{date_range}）"

        if not body or len(body) < 5:
            continue
        if is_excluded(body):
            continue
        if "観仏三昧" in body or "kanbutuzanmai" in body.lower():
            continue

        key = f"{href}|{body}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        items.append({
            "title": body,
            "url": href,
            "source": "kanbutsu",
            "header": "【仏像特別公開情報】",
            "hashtags": "#仏像 #特別公開",
        })

    return items


def scrape_museum_rss(museum_name: str, rss_candidates: list[str]) -> list[dict]:
    """国立博物館のRSSフィードから仏像関連エントリを抽出。"""
    items: list[dict] = []
    for rss_url in rss_candidates:
        feed = feedparser.parse(rss_url)
        if not feed.entries:
            continue
        for entry in feed.entries[:30]:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            summary = getattr(entry, "summary", "")
            if not (title and link):
                continue
            if not is_relevant(f"{title} {summary}"):
                continue
            items.append({
                "title": f"{museum_name}：{title}",
                "url": link,
                "source": museum_name,
                "header": "【仏像特別公開情報】",
                "hashtags": "#仏像 #特別公開",
            })
        if items:
            break
    return items


def scrape_souda_kyoto() -> list[dict]:
    """京都非公開文化財特別公開（そうだ京都）"""
    url = "https://souda-kyoto.jp/event/detail/autumn-cultural-properties.html"
    html = fetch_html(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    title_text = h1.get_text(strip=True) if h1 else None
    if not title_text:
        title_tag = soup.find("title")
        title_text = title_tag.get_text(strip=True) if title_tag else "京都非公開文化財特別公開"

    period = ""
    for p in soup.find_all(["p", "div", "span"]):
        text = p.get_text(strip=True)
        if text and ("月" in text and "日" in text) and len(text) < 80:
            period = text
            break

    full_title = f"{title_text}{(' ' + period) if period else ''}"
    return [{
        "title": full_title,
        "url": url,
        "source": "京都非公開文化財特別公開",
        "header": "【仏像特別公開情報】",
        "hashtags": "#仏像 #特別公開",
    }]


def scrape_inori_nara() -> list[dict]:
    """祈りの回廊 奈良県 秘宝・秘仏特別開帳"""
    url = "https://inori.nara-kankou.or.jp/inori/hihou/"
    html = fetch_html(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    seen_links: set[str] = set()

    keywords = ["秘", "開帳", "開扉", "公開", "如来", "菩薩", "観音", "仏", "御本尊", "明王"]
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        if not title or len(title) < 3 or len(title) > 100:
            continue
        if is_excluded(title):
            continue
        href = urljoin(url, a["href"])
        if "inori.nara-kankou.or.jp" not in href:
            continue
        if href.rstrip("/") == url.rstrip("/"):
            continue
        if href in seen_links:
            continue
        if not any(kw in title for kw in keywords):
            continue
        seen_links.add(href)
        items.append({
            "title": title,
            "url": href,
            "source": "祈りの回廊",
            "header": "【仏像特別公開情報】",
            "hashtags": "#仏像 #特別公開",
        })

    if not items:
        h1 = soup.find("h1")
        page_title = h1.get_text(strip=True) if h1 else "祈りの回廊 秘宝・秘仏特別開帳"
        items.append({
            "title": page_title,
            "url": url,
            "source": "祈りの回廊",
            "header": "【仏像特別公開情報】",
            "hashtags": "#仏像 #特別公開",
        })

    return items[:10]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    data = load_news_data()
    existing_ids = {item["id"] for item in data["items"]}

    all_items: list[dict] = []

    print("[1/4] 観仏三昧を取得中...")
    try:
        all_items.extend(scrape_kanbutsuzanmai())
    except Exception as e:
        print(f"観仏三昧スクレイピング失敗: {e}", file=sys.stderr)
    time.sleep(SLEEP_BETWEEN_REQUESTS)

    print("[2/4] 国立博物館RSSを取得中...")
    museums = [
        ("東京国立博物館", [
            "https://www.tnm.jp/uploads/rss/news.xml",
            "https://www.tnm.jp/rss/news.xml",
        ]),
        ("奈良国立博物館", [
            "https://www.narahaku.go.jp/rss/news.xml",
            "https://www.narahaku.go.jp/news.xml",
        ]),
        ("京都国立博物館", [
            "https://www.kyohaku.go.jp/jp/rss/news.xml",
            "https://www.kyohaku.go.jp/rss/news.xml",
        ]),
        ("九州国立博物館", [
            "https://www.kyuhaku.jp/news/news.xml",
            "https://www.kyuhaku.jp/rss/news.xml",
        ]),
    ]
    for name, candidates in museums:
        try:
            all_items.extend(scrape_museum_rss(name, candidates))
        except Exception as e:
            print(f"{name} RSS取得失敗: {e}", file=sys.stderr)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    print("[3/4] 京都非公開文化財特別公開を取得中...")
    try:
        all_items.extend(scrape_souda_kyoto())
    except Exception as e:
        print(f"そうだ京都スクレイピング失敗: {e}", file=sys.stderr)
    time.sleep(SLEEP_BETWEEN_REQUESTS)

    print("[4/4] 祈りの回廊を取得中...")
    try:
        all_items.extend(scrape_inori_nara())
    except Exception as e:
        print(f"祈りの回廊スクレイピング失敗: {e}", file=sys.stderr)

    print(f"取得アイテム数: {len(all_items)}")

    # 新規アイテムのみ先頭に追記（OGP 画像も取得）
    added_count = 0
    for item in all_items:
        uid = item_id(item.get("url", ""), item["title"])
        if uid in existing_ids:
            continue
        if is_excluded(item["title"]):
            continue
        item["id"] = uid
        item["fetched_at"] = datetime.now(JST).isoformat()
        # 新規アイテムの OGP 画像を取得（URL がある場合のみ）
        if item.get("url"):
            image_url = fetch_og_image(item["url"])
            item["image_url"] = image_url
            if image_url:
                print(f"  画像取得: {image_url[:60]}")
        else:
            item["image_url"] = ""
        data["items"].insert(0, item)
        existing_ids.add(uid)
        added_count += 1
        print(f"追加: [{item['source']}] {item['title']}")

    data["items"] = data["items"][:MAX_TOTAL_ITEMS]
    data["last_updated"] = datetime.now(JST).isoformat()
    save_news_data(data)
    print(f"追加件数: {added_count} / 合計: {len(data['items'])}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
