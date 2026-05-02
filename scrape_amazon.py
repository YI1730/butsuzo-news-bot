"""Amazon Creators API を使って仏像関連商品（書籍・グッズ）の新着を取得し
docs/data/news.json に追記する。

OAuth 2.0 Client Credentials grant でアクセストークンを取得し、
SearchItems 相当のエンドポイントで商品を検索する。

環境変数:
  AMAZON_CLIENT_ID      OAuth Client ID
  AMAZON_CLIENT_SECRET  OAuth Client Secret
  AMAZON_STORE_ID       アソシエイトの Store ID（Partner Tag）

注意:
  Creators API の正式エンドポイント URL が不明なため、下記 CONFIG セクションの
  TOKEN_URL / SEARCH_ENDPOINT は仮値です。実際の URL に書き換えてください。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ===========================================================================
# CONFIG — Creators API の実際の URL に合わせて以下を修正してください
# ===========================================================================

# OAuth トークンエンドポイント（LWA 系の場合は https://api.amazon.com/auth/o2/token）
TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# OAuth スコープ（必要な場合のみ。Creators API のスコープ名が判明したら設定）
OAUTH_SCOPE = ""

# 商品検索 API エンドポイント（Creators API の正式 URL に修正してください）
SEARCH_ENDPOINT = "https://api.creator.amazon.com/v1/products/search"

# Marketplace（日本: www.amazon.co.jp）
MARKETPLACE = "www.amazon.co.jp"

# ===========================================================================
# スクレイパー設定
# ===========================================================================

NEWS_JSON_FILE = Path(__file__).parent / "docs" / "data" / "news.json"
MAX_TOTAL_ITEMS = 500
MAX_ITEMS_PER_QUERY = 10
JST = timezone(timedelta(hours=9))

REQUEST_TIMEOUT = 15
SLEEP_BETWEEN_REQUESTS = 1.5

USER_AGENT = "ButsuzoNewsBot/1.0 (+https://yi1730.github.io/butsuzo-news-bot/)"

SOURCE_AMAZON = "amazon_goods"

# 検索クエリ
BOOK_KEYWORD  = "仏像"
GOODS_KEYWORD = "仏像 -仏具 -神具"  # 信仰用品を除外

# ===========================================================================
# 認証情報
# ===========================================================================

CLIENT_ID     = os.environ.get("AMAZON_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("AMAZON_CLIENT_SECRET", "").strip()
STORE_ID      = os.environ.get("AMAZON_STORE_ID", "").strip()


def check_credentials() -> bool:
    missing = []
    if not CLIENT_ID:     missing.append("AMAZON_CLIENT_ID")
    if not CLIENT_SECRET: missing.append("AMAZON_CLIENT_SECRET")
    if not STORE_ID:      missing.append("AMAZON_STORE_ID")
    if missing:
        print(f"環境変数未設定（スキップ）: {', '.join(missing)}", file=sys.stderr)
        return False
    return True


# ===========================================================================
# JSON ユーティリティ（既存スクレイパーと同様）
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
# OAuth 2.0 アクセストークン取得
# ===========================================================================


def get_access_token() -> str | None:
    """Client Credentials grant でアクセストークンを取得。"""
    payload = {
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    if OAUTH_SCOPE:
        payload["scope"] = OAUTH_SCOPE

    try:
        resp = requests.post(
            TOKEN_URL,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent":   USER_AGENT,
                "Accept":       "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            print(
                f"トークン取得失敗 status={resp.status_code} body={resp.text[:300]}",
                file=sys.stderr,
            )
            return None
        body = resp.json()
        token = body.get("access_token")
        if not token:
            print(f"access_token がレスポンスに含まれず: {body}", file=sys.stderr)
            return None
        ttl = body.get("expires_in", "?")
        print(f"  アクセストークン取得成功（有効期限 {ttl} 秒）")
        return token
    except requests.RequestException as e:
        print(f"トークン取得例外: {e}", file=sys.stderr)
        return None
    except ValueError as e:
        print(f"トークンレスポンス JSON パース失敗: {e}", file=sys.stderr)
        return None


# ===========================================================================
# 商品検索
# ===========================================================================


def search_products(
    token: str,
    keyword: str,
    search_index: str | None = None,
    limit: int = MAX_ITEMS_PER_QUERY,
) -> list[dict]:
    """SearchItems 相当のエンドポイントを叩いて商品配列を返す。

    レスポンスのスキーマは API 仕様により異なる可能性があるため、
    複数の候補キー（items/Items/results/products）から取得を試みる。
    """
    params = {
        "keywords":     keyword,
        "sortBy":       "newest",
        "itemCount":    limit,
        "associateTag": STORE_ID,
        "marketplace":  MARKETPLACE,
    }
    if search_index:
        params["searchIndex"] = search_index

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "User-Agent":    USER_AGENT,
    }

    try:
        resp = requests.get(
            SEARCH_ENDPOINT,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 401:
            print("  認証エラー（401）: トークンが無効か期限切れ", file=sys.stderr)
            return []
        if resp.status_code == 403:
            print("  権限不足（403）: API 利用権限を確認してください", file=sys.stderr)
            return []
        if resp.status_code == 404:
            print(
                "  エンドポイント不正（404）: SEARCH_ENDPOINT を確認してください",
                file=sys.stderr,
            )
            return []
        if resp.status_code == 429:
            print("  レートリミット（429）: しばらく待って再試行", file=sys.stderr)
            return []
        if resp.status_code >= 400:
            print(
                f"  検索失敗 status={resp.status_code} body={resp.text[:200]}",
                file=sys.stderr,
            )
            return []
        body = resp.json()
        # 複数候補キーから抽出
        return (
            body.get("items")
            or body.get("Items")
            or body.get("results")
            or body.get("products")
            or body.get("data", {}).get("items")
            or []
        )
    except requests.RequestException as e:
        print(f"  検索例外 [{keyword}]: {e}", file=sys.stderr)
        return []
    except ValueError as e:
        print(f"  検索レスポンス JSON パース失敗: {e}", file=sys.stderr)
        return []


# ===========================================================================
# レスポンス → news.json アイテム形式に変換
# ===========================================================================


def _pick(d: dict, *keys: str, default=""):
    """ネスト dict から複数キー候補を順に試して取り出す。"""
    for key in keys:
        if "." in key:
            cur = d
            for part in key.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    cur = None
                    break
            if cur:
                return cur
        elif key in d and d[key]:
            return d[key]
    return default


def append_associate_tag(url: str) -> str:
    """URL にアソシエイトタグを付与する（既に付いていれば触らない）。"""
    if not url or not STORE_ID:
        return url
    if "tag=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}tag={STORE_ID}"


def normalize_item(raw: dict, item_type: str) -> dict | None:
    """API レスポンスのアイテム1件を news.json 形式に変換する。"""
    asin = _pick(raw, "asin", "ASIN", "productId", "id")

    title = _pick(
        raw,
        "title",
        "name",
        "ItemInfo.Title.DisplayValue",
    ).strip()

    # 商品ページ URL
    url = _pick(raw, "detailPageUrl", "DetailPageURL", "productUrl", "url")
    if not url and asin:
        url = f"https://www.amazon.co.jp/dp/{asin}"
    url = append_associate_tag(url)

    # 画像 URL
    image_url = ""
    img_candidates = [
        raw.get("image"),
        raw.get("mainImage"),
        raw.get("imageUrl"),
        raw.get("thumbnailUrl"),
        _pick(raw, "Images.Primary.Large.URL"),
        _pick(raw, "Images.Primary.Medium.URL"),
    ]
    for cand in img_candidates:
        if isinstance(cand, str) and cand.startswith("http"):
            image_url = cand
            break
        if isinstance(cand, dict):
            v = cand.get("url") or cand.get("URL")
            if v and v.startswith("http"):
                image_url = v
                break

    # 発売日
    released = _pick(
        raw,
        "releaseDate", "ReleaseDate",
        "publicationDate", "PublicationDate",
        "ItemInfo.ProductInfo.ReleaseDate.DisplayValue",
    )

    if not (title and (url or asin)):
        return None

    # ハッシュタグ＆ヘッダーを item_type で切り分け
    if item_type == "book":
        hashtags = "#仏像 #仏像新刊情報"
        header   = "【仏像新刊】"
    else:
        hashtags = "#仏像 #仏像のある暮らし"
        header   = "【仏像グッズ】"

    # released → published_at（ISO 8601 JST）
    published_at = ""
    if released:
        try:
            dt = datetime.fromisoformat(str(released)[:10])
            published_at = dt.replace(tzinfo=JST).isoformat()
        except (ValueError, TypeError):
            pass

    return {
        "title":        title,
        "url":          url,
        "source":       SOURCE_AMAZON,
        "item_type":    item_type,
        "asin":         asin,
        "header":       header,
        "hashtags":     hashtags,
        "image_url":    image_url,
        "published_at": published_at,
    }


# ===========================================================================
# main
# ===========================================================================


def main() -> int:
    if not check_credentials():
        # 認証情報がなくてもワークフロー全体は失敗させない
        return 0

    print("Amazon Creators API トークン取得中...")
    token = get_access_token()
    if not token:
        print("アクセストークン取得失敗のため処理中断", file=sys.stderr)
        return 0  # 他の scraper を妨げないため 0 終了

    data = load_news_data()
    existing_ids = {item["id"] for item in data["items"]}
    existing_asins = {
        item.get("asin") for item in data["items"]
        if item.get("source") == SOURCE_AMAZON and item.get("asin")
    }

    all_new: list[dict] = []

    print(f"[1/2] 書籍カテゴリー検索: keyword='{BOOK_KEYWORD}'")
    raw_books = search_products(token, BOOK_KEYWORD, search_index="Books")
    print(f"  取得件数: {len(raw_books)}")
    for raw in raw_books:
        norm = normalize_item(raw, "book")
        if norm:
            all_new.append(norm)
    time.sleep(SLEEP_BETWEEN_REQUESTS)

    print(f"[2/2] グッズ検索: keyword='{GOODS_KEYWORD}'")
    raw_goods = search_products(token, GOODS_KEYWORD, search_index="All")
    print(f"  取得件数: {len(raw_goods)}")
    for raw in raw_goods:
        norm = normalize_item(raw, "goods")
        if norm:
            all_new.append(norm)

    print(f"取得アイテム合計: {len(all_new)}")

    added_count = 0
    for item in all_new:
        # ASIN による重複排除（最優先）
        if item.get("asin") and item["asin"] in existing_asins:
            continue
        uid = item_id(item["url"], item["title"])
        if uid in existing_ids:
            continue

        item["id"] = uid
        item["fetched_at"] = datetime.now(JST).isoformat()

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
