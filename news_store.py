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
