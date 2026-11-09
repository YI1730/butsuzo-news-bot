"""docs/data/news.json の件数上限を「取得元（source）ごと」に適用する共通処理。

以前は各スクリプトが全体で 500 件に切り詰めていたため、Google ニュースの
取り込みが増えると、特別公開（kanbutsu 等）や書籍・グッズ（amazon_goods）の
記事が押し出されて消えていた。取得元ごとに上限を持たせ、互いに影響しないようにする。
"""

from __future__ import annotations

import re

from collections import defaultdict

MAX_ITEMS_PER_SOURCE = 300

# AI 採点（curate_news.py）のしきい値。ダッシュボード表示でも同じ値を使う。
CANDIDATE_THRESHOLD = 70   # これ以上を「⭐配信候補」として投稿文を作り通知する
NOISE_THRESHOLD = 30       # これ未満はダッシュボードのニュースタブに出さない


def trim_per_source(items: list[dict], cap: int = MAX_ITEMS_PER_SOURCE) -> list[dict]:
    """並び順（新しい順）を保ったまま、各 source を先頭から cap 件までに絞る。"""
    counts: dict[str, int] = defaultdict(int)
    kept = []
    for item in items:
        src = item.get("source", "")
        if counts[src] < cap:
            counts[src] += 1
            kept.append(item)
    return kept


NEWS_POST_HEADER = "【仏像速報】"

# PWA の「スキップ」で投稿済みとして扱う記事（記事の id と URL だけ。PWA が GitHub に直接書く）
from pathlib import Path as _Path  # noqa: E402
SKIPPED_FILE = _Path(__file__).parent / "docs" / "data" / "skipped_news.json"


def load_skipped() -> tuple[set[str], set[str]]:
    """スキップした記事の (id の集合, URL の集合)。ファイルが無ければ空。"""
    import json as _json
    try:
        items = _json.loads(SKIPPED_FILE.read_text(encoding="utf-8")).get("items", [])
    except Exception:
        return set(), set()
    return ({x["id"] for x in items if x.get("id")}, {x["url"] for x in items if x.get("url")})


def clean_news_title(title: str) -> str:
    """Google ニュースのタイトル末尾の「 - 配信元名」を除く。"""
    head, sep, _ = title.rpartition(" - ")
    return head.strip() if sep and head.strip() else title.strip()


def compose_news_post(comment: str, title: str, header: str = NEWS_POST_HEADER) -> str:
    """ニュース投稿の本文。文章 → 【見出し】記事タイトル の順（URL は X が末尾に付ける）。

    見出しは通常【仏像速報】。御開帳カレンダーのリマインドでは【あすから公開】などに変わる。
    """
    return f"{comment.strip()}\n\n{header}{clean_news_title(title)}"


# ── 書籍の並び順（generate_dashboard.py が並べる。rank_books.py が採点） ──
# 並び順は「取り込み日（fetched_at）の新しい順」だけ。発売日はバッジで見せる。
BOOK_HIDE_BELOW = 10         # これ未満は仏像と無関係とみなし表示しない
BOOK_KIND_MAGAZINE = "magazine"            # 雑誌の仏像特集号（scrape_magazines.py）
BOOK_HEADER_BOOK = "【仏像新刊】"
BOOK_HEADER_MAGAZINE = "【仏像特集】"


def book_header(item: dict) -> str:
    """書籍の投稿の見出し。雑誌は【仏像特集】、それ以外は【仏像新刊】（item の header には左右されない）。"""
    return BOOK_HEADER_MAGAZINE if item.get("book_kind") == BOOK_KIND_MAGAZINE else BOOK_HEADER_BOOK


def book_fetched_key(item: dict) -> str:
    """取り込み日時の並び替えキー（ISO 8601 の文字列。無ければ空）。"""
    return item.get("fetched_at") or ""


def book_release_date(item: dict):
    """書籍の発売日（date）。scrape_amazon.py が release_date を持たせる前の書籍は、
    published_at が 0 時ちょうど（＝発売日から作った値）なら発売日とみなす。"""
    from datetime import datetime as _dt
    s = item.get("release_date") or ""
    if not s:
        p = item.get("published_at") or ""
        if "T00:00:00" in p:
            s = p[:10]
    try:
        return _dt.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ── 古い記事の再配信の検出 ──
# 写真ページなどが、何年も前の記事のまま新しい日付で Google ニュースに出直すことがある
# （山陽新聞の photo/detail など）。記事ページ自身の日付を読んで、配信日より大きく古ければ古い記事とみなす
STALE_DAYS = 90
_DATE_RE = re.compile(r"(20\d\d)\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})")
_META_DATE_KEYS = ("article:published_time", "og:article:published_time", "datepublished", "pubdate",
                   "publishdate", "date", "dc.date.issued", "dc.date", "sailthru.date")


def _to_date(y: int, m: int, d: int):
    from datetime import date as _d
    try:
        return _d(y, m, d)
    except ValueError:
        return None


def detect_page_date(content: bytes):
    """記事ページ自身の公開日（date）。メタ情報・JSON-LD・time 要素・本文冒頭の日付の順に探す。見つからなければ None。"""
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(content[:400_000], "html.parser")
    except Exception:
        return None
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").lower()
        if key in _META_DATE_KEYS:
            hit = _DATE_RE.search((m.get("content") or "").replace("T", " "))
            if hit:
                d = _to_date(*(int(x) for x in hit.groups()))
                if d:
                    return d
    for sc in soup.find_all("script", type="application/ld+json"):
        hit = re.search(r'"datePublished"\s*:\s*"(\d{4})-(\d{2})-(\d{2})', sc.string or "")
        if hit:
            d = _to_date(*(int(x) for x in hit.groups()))
            if d:
                return d
    t = soup.find("time", attrs={"datetime": True})
    if t:
        hit = _DATE_RE.search(t["datetime"])
        if hit:
            d = _to_date(*(int(x) for x in hit.groups()))
            if d:
                return d
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    hit = _DATE_RE.search(" ".join(root.get_text(" ", strip=True).split())[:1500])
    return _to_date(*(int(x) for x in hit.groups())) if hit else None


def is_stale(page_date, ref_iso: str | None) -> bool:
    """記事ページの日付が、配信日（なければ今日）より STALE_DAYS 日以上古ければ True。"""
    from datetime import date as _d, datetime as _dt, timedelta as _td

    if page_date is None:
        return False
    try:
        ref = _dt.fromisoformat(ref_iso).date() if ref_iso else _d.today()
    except ValueError:
        ref = _d.today()
    return page_date < ref - _td(days=STALE_DAYS)


from datetime import timedelta as _td, timezone as _tz  # noqa: E402

JST_TZ = _tz(_td(hours=9))


# ── X の文字数（重み付き 280）と、投稿文の自動短縮 ──
# X（twitter-text v3）では ASCII 等は 1、日本語・全角・絵文字は 2、URL は長さにかかわらず 23 と数える。
# 投稿画面を開く URL（intent）は text と url を別に渡すので、url は「23 + 区切りの空白 1」として数える。
X_LIMIT = 280
X_URL_WEIGHT = 23


def x_weighted_length(text: str) -> int:
    """X の重み付き文字数。ASCII 等は 1、日本語や絵文字は 2。"""
    n = 0
    for ch in text:
        cp = ord(ch)
        if cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247:
            n += 1
        else:
            n += 2
    return n


def intent_weighted_length(text: str, url: str = "") -> int:
    """x.com/intent/post に text と url を渡したときの、投稿全体の重み付き文字数。"""
    return x_weighted_length(text) + ((X_URL_WEIGHT + 1) if url else 0)


_SENTENCE_END = "。！？!?\n"


def _split_sentences(text: str) -> list[str]:
    """句点・感嘆符・改行で文に分ける（区切りは前の文に残す）。"""
    out, cur = [], ""
    for ch in text:
        cur += ch
        if ch in _SENTENCE_END:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def shorten_to_weight(text: str, max_weight: int) -> str:
    """text を重み max_weight 以内に縮める。文の区切りで切り、1 文目だけで超えるときは読点で、
    それでも超えるときは文字単位で切って「…」を付ける。収まるなら text をそのまま返す。"""
    text = text.strip()
    if x_weighted_length(text) <= max_weight:
        return text
    if max_weight <= 0:
        return ""
    kept = ""
    for s in _split_sentences(text):
        if x_weighted_length((kept + s).strip()) > max_weight:
            break
        kept += s
    if kept.strip():
        return kept.strip()
    first = _split_sentences(text)[0]
    kept = ""
    for part in re.split(r"(?<=[、，,])", first):
        if x_weighted_length(kept + part) > max_weight - 2:
            break
        kept += part
    kept = kept.rstrip("、，, ")
    if kept:
        return kept + "。"
    kept = ""
    for ch in first:
        if x_weighted_length(kept + ch + "…") > max_weight:
            break
        kept += ch
    return kept + "…" if kept else ""


def fit_x_post(head: str, middle: str, tail: str, url: str = "", limit: int = X_LIMIT) -> str:
    """head + middle + tail を、url 込みで重み付き limit 以内にして返す。

    縮めるのは middle（コメントや紹介文）だけ。head（見出し・書名など）と tail、url は切らない。
    middle を空にしても収まらないときは、head と tail だけを返す（呼び出し側で警告する）。
    """
    full = head + middle + tail
    if intent_weighted_length(full, url) <= limit:
        return full
    room = limit - intent_weighted_length(head + tail, url)
    short = shorten_to_weight(middle, room) if room > 0 else ""
    if not short:
        return (head.rstrip("\n") + tail).strip("\n")
    return head + short + tail


def fit_headline_post(header: str, title: str, hashtags: str, url: str = "", limit: int = X_LIMIT) -> str:
    """文章の無い投稿（見出し・題名・ハッシュタグ）を重み付き limit 以内にする。
    収まらなければ、まずハッシュタグを外し、それでも超えるときだけ題名を「…」で縮める（最後の手段）。"""
    text = f"{header}\n{title}\n{hashtags}".rstrip("\n")
    if intent_weighted_length(text, url) <= limit:
        return text
    text = f"{header}\n{title}"
    if intent_weighted_length(text, url) <= limit:
        return text
    room = limit - intent_weighted_length(f"{header}\n", url)
    return f"{header}\n{shorten_to_weight(title, room)}"


# ── 投稿文に使ってはいけない語（生成した文の自動チェックと、プロンプトの規則の両方で使う） ──
BANNED_POST_WORDS = ("仏様", "仏さま", "仏さん", "癒やし", "癒し", "感動", "圧倒", "驚き", "最高", "興奮")


def find_banned_words(text: str) -> list[str]:
    """text に含まれる禁止語（BANNED_POST_WORDS）の一覧。"""
    return [w for w in BANNED_POST_WORDS if w in text]
