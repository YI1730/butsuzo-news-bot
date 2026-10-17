"""docs/data/news.json の件数上限を「取得元（source）ごと」に適用する共通処理。

以前は各スクリプトが全体で 500 件に切り詰めていたため、Google ニュースの
取り込みが増えると、特別公開（kanbutsu 等）や書籍・グッズ（amazon_goods）の
記事が押し出されて消えていた。取得元ごとに上限を持たせ、互いに影響しないようにする。
"""

from __future__ import annotations

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


def clean_news_title(title: str) -> str:
    """Google ニュースのタイトル末尾の「 - 配信元名」を除く。"""
    head, sep, _ = title.rpartition(" - ")
    return head.strip() if sep and head.strip() else title.strip()


def compose_news_post(comment: str, title: str, header: str = NEWS_POST_HEADER) -> str:
    """ニュース投稿の本文。文章 → 【見出し】記事タイトル の順（URL は X が末尾に付ける）。

    見出しは通常【仏像速報】。御開帳カレンダーのリマインドでは【あすから公開】などに変わる。
    """
    return f"{comment.strip()}\n\n{header}{clean_news_title(title)}"


# ── 書籍の並び順（rank_books.py が採点、generate_dashboard.py が並べる） ──
BOOK_NEW_RELEASE_DAYS = 60   # 発売後この日数までを「新刊」とする
BOOK_NEW_MIN_SCORE = 50      # 新刊欄に出す最低点（塗り絵などは新刊でも下段へ）
BOOK_HIDE_BELOW = 10         # これ未満は仏像と無関係とみなし表示しない


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
