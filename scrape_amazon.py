"""Amazon 検索結果をスクレイピングして仏像関連商品（書籍・グッズ）の新着を取得し
docs/data/news.json に追記する。

X API 不使用。API 認証不要。requests + BeautifulSoup による HTML スクレイピング。

検索対象 URL:
  ① 書籍（新着順）:
     https://www.amazon.co.jp/s?k=仏像&i=stripbooks&s=date-desc-rank
  ② ホビー/グッズ（新着順）:
     https://www.amazon.co.jp/s?k=仏像+-仏具+-神具&i=hobby&s=date-desc-rank

アソシエイトタグ: yoshikingnenj-22
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

# ===========================================================================
# 設定
# ===========================================================================

ASSOCIATE_TAG = "butsuzo03-22"

# 書籍（stripbooks）: 新着順
BOOK_URL = (
    "https://www.amazon.co.jp/s"
    "?k=%E4%BB%8F%E5%83%8F"
    "&i=stripbooks"
    "&s=date-desc-rank"
)

# グッズ（全カテゴリー/aps）: 仏像 -仏具 -神具 新着順
GOODS_URL = (
    "https://www.amazon.co.jp/s"
    "?k=%E4%BB%8F%E5%83%8F+-%E4%BB%8F%E5%85%B7+-%E7%A5%9E%E5%85%B7"
    "&i=aps"
    "&s=date-desc-rank"
)

NEWS_JSON_FILE = Path(__file__).parent / "docs" / "data" / "news.json"
MAX_TOTAL_ITEMS = 500
JST = timezone(timedelta(hours=9))

# リクエスト間隔（Amazon の負荷軽減・ブロック回避）
SLEEP_BETWEEN_PAGES = 4
SLEEP_BETWEEN_ITEMS = 0  # 検索結果ページは1リクエストで複数アイテム取得

SOURCE_AMAZON = "amazon_goods"

# ===========================================================================
# HTTP ヘッダー（ブラウザ偽装）
# ===========================================================================
# ※ Amazon は User-Agent 等を厳しく検査する。
#    GitHub Actions の IP から連続アクセスすると CAPTCHA / 空結果になる場合がある。

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Referer": "https://www.amazon.co.jp/",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

REQUEST_TIMEOUT = 20

# ===========================================================================
# JSON ユーティリティ
# ===========================================================================


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
    return hashlib.md5((url + title).encode()).hexdigest()[:12]


# ===========================================================================
# URL ユーティリティ
# ===========================================================================


def extract_asin(url: str) -> str:
    """URL から ASIN（10桁英数字）を抽出する。取得できなければ空文字。"""
    # /dp/XXXXXXXXXX または /gp/product/XXXXXXXXXX パターン
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    return m.group(1) if m else ""


def build_product_url(asin: str) -> str:
    """ASIN からアソシエイトタグ付きの商品 URL を生成する。"""
    return f"https://www.amazon.co.jp/dp/{asin}?tag={ASSOCIATE_TAG}"


def append_associate_tag(url: str) -> str:
    """既存 URL にアソシエイトタグを付与する（重複防止）。"""
    if not url:
        return url
    if f"tag={ASSOCIATE_TAG}" in url:
        return url
    # 既に tag= パラメーターがある場合は上書き
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["tag"] = [ASSOCIATE_TAG]
    new_query = urlencode({k: v[0] for k, v in qs.items()})
    return urlunparse(parsed._replace(query=new_query))


# ===========================================================================
# スクレイパー
# ===========================================================================


def fetch_page(session: requests.Session, url: str) -> BeautifulSoup | None:
    """指定 URL を取得して BeautifulSoup を返す。失敗・ブロック検知時は None。"""
    try:
        resp = session.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"  HTTP {resp.status_code}: {url[:80]}", file=sys.stderr)
            return None
        html = resp.text
        # CAPTCHA / ロボット判定ページの検知
        if "robot" in html.lower() and "captcha" in html.lower():
            print("  ⚠ CAPTCHA / Bot 検知ページが返されました", file=sys.stderr)
            return None
        if "api-services-support@amazon.com" in html:
            print("  ⚠ Amazon エラーページ（ブロック）が返されました", file=sys.stderr)
            return None
        return BeautifulSoup(html, "html.parser")
    except requests.RequestException as e:
        print(f"  取得失敗: {e}", file=sys.stderr)
        return None


# 仏像関連キーワード（いずれか含む商品のみ収集）
RELEVANT_KEYWORDS = [
    "仏像", "如来", "菩薩", "観音", "明王", "天部", "羅漢",
    "秘仏", "大仏", "仏教", "仏陀", "釈迦", "弥勒", "不動",
]

# 除外キーワード（タイトルに含まれていたら除外）
EXCLUDE_KEYWORDS = [
    "グラビア", "ストリップ", "ヌード", "ギャンブル", "賭博",
    "仏壇", "神棚", "仏具", "風水", "金運", "厄除け",
]


def is_relevant_title(title: str) -> bool:
    """仏像関連キーワードを含むか確認（無関係の商品を弾く）。"""
    if any(kw in title for kw in EXCLUDE_KEYWORDS):
        return False
    return any(kw in title for kw in RELEVANT_KEYWORDS)


def parse_search_results(soup: BeautifulSoup, item_type: str) -> list[dict]:
    """Amazon 検索結果ページから商品リストを抽出する。

    実際の HTML 構造（2026年5月確認）:
      - コンテナ: div[data-component-type="s-search-result"][data-asin]
      - タイトル: h2.get_text() 直接（h2 > a > span 構造ではない）
      - 発売日:   span.a-text-normal 内の YYYY/MM/DD テキスト
      - 画像:     img.s-image の src 属性
    """
    items: list[dict] = []

    cards = soup.find_all(
        "div",
        attrs={"data-component-type": "s-search-result", "data-asin": True},
    )
    if not cards:
        # フォールバック
        cards = soup.find_all("div", class_="s-result-item", attrs={"data-asin": True})
    if not cards:
        print("  商品カードが見つかりませんでした（HTML 構造変更 or ブロック）", file=sys.stderr)
        return []

    print(f"  カード数: {len(cards)}")

    for card in cards:
        asin = card.get("data-asin", "").strip()
        if not asin:
            continue  # data-asin が空（スポンサー枠等）はスキップ

        # --- タイトル（h2 直下のテキスト）---
        h2 = card.find("h2")
        if not h2:
            continue
        title = h2.get_text(strip=True)
        if not title:
            continue

        # 無関係の商品を除外（仏像キーワードフィルタ）
        if not is_relevant_title(title):
            continue

        # --- 商品 URL（ASIN から生成）---
        product_url = build_product_url(asin)

        # --- 画像 URL ---
        image_url = ""
        img_tag = card.find("img", class_="s-image")
        if img_tag:
            image_url = img_tag.get("src", "")
            srcset = img_tag.get("srcset", "")
            if srcset:
                # srcset の末尾（最高解像度）を取得
                parts = [p.strip() for p in srcset.split(",") if p.strip()]
                if parts:
                    image_url = parts[-1].split()[0]

        # --- 発売日（span.a-text-normal 内の YYYY/MM/DD）---
        # 商品検索結果ページには発売日が表示される
        release_date_str = ""
        for sp in card.find_all("span", class_="a-text-normal"):
            t = sp.get_text(strip=True)
            if re.match(r"\d{4}/\d{1,2}/\d{1,2}", t):
                release_date_str = t
                break

        # published_at: 発売日が取れた場合はその日付、なければ実行日（今日）
        # ※ scrape_amazon.py 内では 14日フィルターは使わないが、
        #    ダッシュボードで 📅 表示されるよう設定する
        if release_date_str:
            try:
                y, m, d = (int(x) for x in release_date_str.split("/"))
                published_at = datetime(y, m, d, tzinfo=JST).isoformat()
            except Exception:
                published_at = datetime.now(JST).isoformat()
        else:
            published_at = datetime.now(JST).isoformat()

        # --- ハッシュタグ / ヘッダー ---
        if item_type == "book":
            hashtags = "#仏像 #仏像新刊情報"
            header   = "【仏像新刊】"
        else:
            hashtags = "#仏像 #仏像のある暮らし"
            header   = "【仏像グッズ】"

        items.append({
            "title":        title,
            "url":          product_url,
            "asin":         asin,
            "source":       SOURCE_AMAZON,
            "item_type":    item_type,
            "header":       header,
            "hashtags":     hashtags,
            "image_url":    image_url,
            "published_at": published_at,
        })

    return items


def fetch_book_description(asin: str, session: requests.Session) -> str:
    """書籍詳細ページから紹介文を取得し、100文字程度に丸めて返す。"""
    url = f"https://www.amazon.co.jp/dp/{asin}"
    try:
        soup = fetch_page(session, url)
        if not soup:
            return ""

        # 1) <meta name="description"> を最初に試す
        meta = soup.find("meta", attrs={"name": "description"})
        if meta:
            content = meta.get("content", "").strip()
            if content and len(content) > 10:
                return content[:100]

        # 2) 書籍説明ブロック #bookDescription_feature_div
        book_div = soup.find(id="bookDescription_feature_div")
        if book_div:
            text = book_div.get_text(" ", strip=True)
            text = re.sub(r"\s+", " ", text)
            if text:
                return text[:100]

        # 3) 商品説明 #productDescription
        prod_div = soup.find(id="productDescription")
        if prod_div:
            text = prod_div.get_text(" ", strip=True)
            text = re.sub(r"\s+", " ", text)
            if text:
                return text[:100]

    except Exception as e:
        print(f"  書籍説明取得失敗 ({asin}): {e}", file=sys.stderr)
    return ""


def build_page_url(base_url: str, page: int) -> str:
    """検索 URL に page パラメーターを付与する（page=1 はそのまま）。"""
    if page <= 1:
        return base_url
    parsed = urlparse(base_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["page"] = [str(page)]
    new_query = urlencode({k: v[0] for k, v in qs.items()})
    return urlunparse(parsed._replace(query=new_query))


def scrape_amazon_search(url: str, item_type: str,
                          session: requests.Session,
                          pages: int = 1) -> list[dict]:
    """Amazon 検索結果ページをスクレイピングして商品リストを返す。

    pages>1 のときは複数ページを取得して新着の取りこぼしを減らす。
    ページ間は ASIN で重複排除する。
    """
    collected: list[dict] = []
    seen_asins: set[str] = set()
    for p in range(1, pages + 1):
        page_url = build_page_url(url, p)
        print(f"  URL (p{p}): {page_url[:90]}")
        soup = fetch_page(session, page_url)
        if not soup:
            # ブロック・空ページ。後続ページも失敗する可能性が高いので打ち切り
            break
        page_items = parse_search_results(soup, item_type)
        for it in page_items:
            asin = it.get("asin", "")
            if asin and asin in seen_asins:
                continue
            if asin:
                seen_asins.add(asin)
            collected.append(it)
        if p < pages:
            time.sleep(SLEEP_BETWEEN_PAGES)
    return collected


# ===========================================================================
# main
# ===========================================================================


def main() -> int:
    # セッションを使い回してクッキーを保持（ブロック回避の一助）
    session = requests.Session()
    # まず Amazon トップページを訪問してクッキーを取得
    print("Amazon トップページを訪問中（クッキー取得）...")
    try:
        session.get(
            "https://www.amazon.co.jp/",
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        print(f"  トップページ訪問失敗（続行）: {e}", file=sys.stderr)
    time.sleep(2)

    data = load_news_data()
    existing_ids   = {item["id"]   for item in data["items"]}
    existing_asins = {
        item.get("asin") for item in data["items"]
        if item.get("source") == SOURCE_AMAZON and item.get("asin")
    }

    all_new: list[dict] = []

    print("[1/2] 書籍カテゴリー（仏像 新着順）を取得中...")
    # 新刊の取りこぼし防止のため書籍は2ページ取得
    book_items = scrape_amazon_search(BOOK_URL, "book", session, pages=2)
    print(f"  取得: {len(book_items)}件")
    all_new.extend(book_items)
    time.sleep(SLEEP_BETWEEN_PAGES)

    print("[2/2] ホビー/グッズ（仏像 -仏具 -神具 新着順）を取得中...")
    goods_items = scrape_amazon_search(GOODS_URL, "goods", session, pages=2)
    print(f"  取得: {len(goods_items)}件")
    all_new.extend(goods_items)

    print(f"取得アイテム合計: {len(all_new)}")

    added_count = 0
    for item in all_new:
        # ASIN 重複排除（最優先）
        if item.get("asin") and item["asin"] in existing_asins:
            continue
        uid = item_id(item["url"], item["title"])
        if uid in existing_ids:
            continue

        item["id"]         = uid
        item["fetched_at"] = datetime.now(JST).isoformat()

        # 書籍のみ詳細ページから紹介文を取得
        if item.get("item_type") == "book" and item.get("asin"):
            time.sleep(2)  # 詳細ページ取得前に少し待機
            desc = fetch_book_description(item["asin"], session)
            item["description"] = desc
            if desc:
                print(f"  紹介文取得: {desc[:40]}…")
        else:
            item["description"] = ""

        data["items"].insert(0, item)
        existing_ids.add(uid)
        if item.get("asin"):
            existing_asins.add(item["asin"])
        added_count += 1
        print(f"追加 [{item['item_type']}]: {item['title'][:60]}")

    data["items"] = data["items"][:MAX_TOTAL_ITEMS]
    data["last_updated"] = datetime.now(JST).isoformat()
    save_news_data(data)
    print(f"追加件数: {added_count} / 合計: {len(data['items'])}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
