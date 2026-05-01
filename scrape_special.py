"""仏像専門サイトと国立博物館の特別公開情報をスクレイピングしてXへ投稿する。

ターゲット:
1. 観仏三昧（仏像の公開情報）
2. 国立博物館（東京・奈良・京都・九州）のRSS
3. 京都非公開文化財特別公開
4. 祈りの回廊（奈良県秘宝・秘仏特別開帳）
"""

import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
import tweepy
from bs4 import BeautifulSoup, NavigableString

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15
SLEEP_BETWEEN_REQUESTS = 3  # 秒
MAX_POSTS_PER_RUN = 1  # 1日4回実行するため1回あたり1件に制限

SCRAPED_HISTORY_FILE = Path(__file__).parent / "scraped_history.txt"

# 国立博物館の更新情報フィルタ用
RELEVANT_KEYWORDS = [
    "仏像",
    "如来",
    "菩薩",
    "観音",
    "明王",
    "天部",
    "羅漢",
    "秘仏",
    "開帳",
    "開扉",
    "特別公開",
    "特別展",
    "御開帳",
    "本尊",
    "曼荼羅",
]

EXCLUDE_KEYWORDS = [
    "グラビア",
    "ストリップ",
    "ヌード",
    "ギャンブル",
    "クラブツーリズム",
    "賭博",
    "ゲーム",
]


# ---------------------------------------------------------------------------
# 履歴・HTTPユーティリティ
# ---------------------------------------------------------------------------


def load_history() -> set[str]:
    if not SCRAPED_HISTORY_FILE.exists():
        return set()
    with SCRAPED_HISTORY_FILE.open("r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_history(key: str) -> None:
    with SCRAPED_HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(key + "\n")


def decode_html(content: bytes) -> str:
    """日本語サイト向けの堅牢な文字コードデコード。

    UTF-8 → Shift-JIS(CP932) → EUC-JP の順に厳格モードで試行し、
    最初に成功したエンコーディングでデコードした文字列を返す。
    すべて失敗した場合は UTF-8 でエラー文字を置換しながらデコードする。

    chardet が Shift-JIS を GBK 等として誤検出する問題を回避するため、
    Pythonの標準デコーダで明示的にフォールバックを試行する設計。
    """
    for encoding in ("utf-8", "cp932", "euc_jp"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def fetch_html(url: str) -> str | None:
    """指定URLのHTMLを decode_html で適切にデコードした文字列で返す。

    UTF-8 / Shift-JIS / EUC-JP の順に厳格デコードを試行するため、
    chardet による Shift-JIS の GBK 誤検出問題が発生しない。
    """
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return decode_html(response.content)
    except requests.RequestException as e:
        print(f"取得失敗 {url}: {e}", file=sys.stderr)
        return None


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
    """仏像の公開情報ページから個別イベント（寺院・展示名・期間・公式URL）を抽出。

    - 投稿には情報元ページのURLや名称を一切含めない（公式URLが取れた場合のみ記載）
    - 公開期間が終了済みのエントリは除外
    """
    source_url = "http://www.kanbutuzanmai.com/butsuzoukoukai.html"
    html = fetch_html(source_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    seen_keys: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # 外部リンクのみ対象（情報元サイト内のリンクは除外）
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

        # 直前のテキストから日付を抽出（<br> または親要素の境界まで遡る）
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

        # 直後のテキストから展示名・タイトルを抽出（次の<br>または<a>まで）
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

        # 投稿文の本文を構築: 「寺院名 展示名（期間）」
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
        # 情報元サイト名・ドメインを文中から完全に排除
        if "観仏三昧" in body or "kanbutuzanmai" in body.lower():
            continue

        key = f"{href}|{body}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        items.append(
            {
                "title": body,
                "url": href,  # 公式URL（外部リンク）
                "source": "kanbutsu",  # 内部識別用。投稿文には現れない
                "header": "【仏像特別公開情報】",
            }
        )

    return items


def scrape_museum_rss(museum_name: str, rss_candidates: list[str]) -> list[dict]:
    """国立博物館のRSSフィードから「仏像」関連のエントリを抽出。

    複数のRSS候補を順に試し、最初にエントリが取れたものを使用する。
    """
    items: list[dict] = []
    for rss_url in rss_candidates:
        feed = feedparser.parse(rss_url)
        if not feed.entries:
            continue
        for entry in feed.entries[:30]:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            summary = getattr(entry, "summary", "")
            check_text = f"{title} {summary}"
            if not (title and link):
                continue
            if not is_relevant(check_text):
                continue
            items.append(
                {
                    "title": f"{museum_name}：{title}",
                    "url": link,
                    "source": museum_name,
                }
            )
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

    # ページタイトルを取得
    h1 = soup.find("h1")
    title_text = h1.get_text(strip=True) if h1 else None
    if not title_text:
        title_tag = soup.find("title")
        title_text = title_tag.get_text(strip=True) if title_tag else "京都非公開文化財特別公開"

    # 開催情報（期間など）を取得し、履歴キーをユニークにする
    period = ""
    for p in soup.find_all(["p", "div", "span"]):
        text = p.get_text(strip=True)
        if text and ("月" in text and "日" in text) and len(text) < 80:
            period = text
            break

    full_title = f"{title_text}{(' ' + period) if period else ''}"
    return [{"title": full_title, "url": url, "source": "京都非公開文化財特別公開"}]


def scrape_inori_nara() -> list[dict]:
    """祈りの回廊 奈良県 秘宝・秘仏特別開帳"""
    url = "https://inori.nara-kankou.or.jp/inori/hihou/"
    html = fetch_html(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    seen_links: set[str] = set()

    # サイト内の個別寺院ページへのリンクを抽出
    keywords = ["秘", "開帳", "開扉", "公開", "如来", "菩薩", "観音", "仏", "御本尊", "明王"]
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        if not title or len(title) < 3 or len(title) > 100:
            continue
        if is_excluded(title):
            continue
        href = urljoin(url, a["href"])
        # 同サイト内のリンクのみ対象
        if "inori.nara-kankou.or.jp" not in href:
            continue
        # トップページ自身は除外
        if href.rstrip("/") == url.rstrip("/"):
            continue
        if href in seen_links:
            continue
        if not any(kw in title for kw in keywords):
            continue
        seen_links.add(href)
        items.append({"title": title, "url": href, "source": "祈りの回廊"})

    # フォールバック
    if not items:
        h1 = soup.find("h1")
        page_title = h1.get_text(strip=True) if h1 else "祈りの回廊 秘宝・秘仏特別開帳"
        items.append({"title": page_title, "url": url, "source": "祈りの回廊"})

    return items[:10]


# ---------------------------------------------------------------------------
# X投稿
# ---------------------------------------------------------------------------


def build_tweet(
    title: str,
    url: str,
    header: str = "【仏像特別公開・イベント情報】",
) -> str:
    """投稿文を組み立てる。url が空文字なら URL 行は省略する。"""
    hashtags = "#仏像 #特別公開"
    if url:
        # 4行: header / title / url / hashtags
        reserved = len(header) + 1 + 1 + len(url) + 1 + len(hashtags)
    else:
        # 3行: header / title / hashtags（URL行なし）
        reserved = len(header) + 1 + 1 + len(hashtags)
    max_title_len = 280 - reserved
    if len(title) > max_title_len and max_title_len > 1:
        title = title[: max_title_len - 1] + "…"
    if url:
        return f"{header}\n{title}\n{url}\n{hashtags}"
    return f"{header}\n{title}\n{hashtags}"


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


def history_key(item: dict) -> str:
    """重複判定用キー: URL + タイトル の組み合わせ。
    URLが空（取得できない場合）でもタイトルでユニーク化される。"""
    url_part = item.get("url") or "__no_url__"
    return f"{url_part}\t{item['title']}"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    history = load_history()
    items: list[dict] = []

    print("[1/4] 観仏三昧を取得中...")
    try:
        items.extend(scrape_kanbutsuzanmai())
    except Exception as e:
        print(f"観仏三昧スクレイピング失敗: {e}", file=sys.stderr)
    time.sleep(SLEEP_BETWEEN_REQUESTS)

    print("[2/4] 国立博物館RSSを取得中...")
    museums = [
        (
            "東京国立博物館",
            [
                "https://www.tnm.jp/uploads/rss/news.xml",
                "https://www.tnm.jp/rss/news.xml",
            ],
        ),
        (
            "奈良国立博物館",
            [
                "https://www.narahaku.go.jp/rss/news.xml",
                "https://www.narahaku.go.jp/news.xml",
            ],
        ),
        (
            "京都国立博物館",
            [
                "https://www.kyohaku.go.jp/jp/rss/news.xml",
                "https://www.kyohaku.go.jp/rss/news.xml",
            ],
        ),
        (
            "九州国立博物館",
            [
                "https://www.kyuhaku.jp/news/news.xml",
                "https://www.kyuhaku.jp/rss/news.xml",
            ],
        ),
    ]
    for name, candidates in museums:
        try:
            items.extend(scrape_museum_rss(name, candidates))
        except Exception as e:
            print(f"{name} RSS取得失敗: {e}", file=sys.stderr)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    print("[3/4] 京都非公開文化財特別公開を取得中...")
    try:
        items.extend(scrape_souda_kyoto())
    except Exception as e:
        print(f"そうだ京都スクレイピング失敗: {e}", file=sys.stderr)
    time.sleep(SLEEP_BETWEEN_REQUESTS)

    print("[4/4] 祈りの回廊を取得中...")
    try:
        items.extend(scrape_inori_nara())
    except Exception as e:
        print(f"祈りの回廊スクレイピング失敗: {e}", file=sys.stderr)

    print(f"取得アイテム数: {len(items)}")

    if not items:
        print("投稿対象がありません")
        return 0

    # ===== 差分抽出フェーズ（X APIを一切呼ばない）=====
    # 履歴・除外チェックを先に済ませ、未投稿候補が無ければX API認証をスキップ
    candidates: list[dict] = []
    for item in items:
        if len(candidates) >= MAX_POSTS_PER_RUN:
            break
        key = history_key(item)
        if key in history:
            continue
        if is_excluded(item["title"]):
            continue
        candidates.append(item)

    if not candidates:
        print("新規アイテムなし。X API認証をスキップして終了します")
        return 0

    # ===== 投稿フェーズ（ここで初めてX API認証を発生させる）=====
    client = get_twitter_client()
    posted_count = 0
    for item in candidates:
        header = item.get("header") or "【仏像特別公開・イベント情報】"
        # テキスト＋URLのみのシンプル投稿（画像アップロードは行わない）
        tweet_text = build_tweet(item["title"], item.get("url") or "", header=header)
        try:
            client.create_tweet(text=tweet_text)
        except tweepy.TweepyException as e:
            # API無駄打ち防止: 失敗したら理由を問わず即終了。リトライ・続行は一切しない
            print(
                f"投稿失敗: [{item['source']}] {item['title']} ({e})",
                file=sys.stderr,
            )
            print("API無駄打ち防止のため、ここで処理を打ち切ります", file=sys.stderr)
            break

        key = history_key(item)
        append_history(key)
        history.add(key)
        posted_count += 1
        print(f"投稿成功: [{item['source']}] {item['title']}")

    print(f"投稿数: {posted_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
