"""書籍（Amazon）の「紹介する価値」を Claude で採点し、PWA の書籍一覧の並び順に使う。

news_hourly.yml から curate_news.py の後に実行される。未採点（または採点基準が古い）の書籍だけを
採点するので、新しい本が入ったとき以外は API を呼ばない。

結果は各書籍の "book_ai" に {score, reason, version} として保存する。
並び順（generate_dashboard.py）:
  1. 新刊: 発売予定〜発売後 BOOK_NEW_RELEASE_DAYS 日で、score >= BOOK_NEW_MIN_SCORE の本を発売日の新しい順
  2. おすすめ: 残りの本を score の高い順
  score < BOOK_HIDE_BELOW の本（仏像と無関係）は表示しない
"""

from __future__ import annotations

import json
import os
import sys

from curate_news import Usage, call_json
from post_news import load_news_data, save_news_data

BOOK_SCORE_VERSION = 1
BATCH_SIZE = 25
# 並び順の閾値（新刊の日数・最低点など）は news_store.py の BOOK_* を参照

SYSTEM = """あなたは仏像好きのコミュニティ「仏像リンク」の選書担当です。
運営者は仏像、とくに平安・鎌倉期の彫刻や地方仏に詳しく、フォロワーは寺院や博物館へ実際に
仏像を見に行く人たちです。Amazon の書籍の一覧を、仏像リンクとして「紹介する価値」で 0〜100 点に採点してください。

# 高い（70〜100）
- 仏像・秘仏・地方仏そのものを扱う本、仏像の研究書・専門書・論文集
- 展覧会の図録、寺院の公式本、仏師（運慶・快慶・定朝など）の本
- 平安・鎌倉期の彫刻、地域の仏像を丁寧に紹介する本
- 実際に仏像を見に行くためのガイド（仏像めぐり、地域別の案内）

# 中くらい（40〜69）
- 入門書、ムック、仏像を題材にした漫画・エッセイ
- 仏教美術全般や寺院建築の本で、仏像の話も多いもの

# 低い（10〜39）
- 塗り絵、カレンダー、写経・なぞり書き、瞑想・スピリチュアル・開運が主の本
- 自費出版の作品集（仏像彫刻の作品集など）、子ども向けの絵本

# 表示しない（0〜9）
- 仏像と無関係なもの（タイトルに語が入っているだけの本など）

reason は判断理由を日本語 25 字以内で。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "score", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


BOOK_POST_HEADER = "【仏像新刊】"
MAX_BOOK_POSTS_PER_RUN = 5
MAX_BOOK_POST_ATTEMPTS = 3
BOOK_POST_AHEAD_DAYS = 120   # 発売予定がこの日数先までの本も紹介文を作る（予約受付中）


def write_book_posts(client, usage: Usage, books: list[dict]) -> int:
    """直近で発売された（または発売予定の）書籍の、X の紹介投稿文を作る。グッズは作らない。"""
    from datetime import datetime, timedelta

    from curate_news import write_post
    from news_store import (BOOK_NEW_MIN_SCORE, BOOK_NEW_RELEASE_DAYS, JST_TZ, book_release_date)

    today = datetime.now(JST_TZ).date()
    cands = []
    for b in books:
        rd = book_release_date(b)
        score = (b.get("book_ai") or {}).get("score", 0)
        if (rd and today - timedelta(days=BOOK_NEW_RELEASE_DAYS) <= rd <= today + timedelta(days=BOOK_POST_AHEAD_DAYS)
                and score >= BOOK_NEW_MIN_SCORE and not b.get("book_post_text")
                and b.get("book_post_attempts", 0) < MAX_BOOK_POST_ATTEMPTS):
            cands.append((rd, b))
    cands.sort(key=lambda t: t[0], reverse=True)
    made = 0
    for rd, b in cands[:MAX_BOOK_POSTS_PER_RUN]:
        when = (f"{rd:%Y年%m月%d日}発売予定（予約受付中）" if rd > today else f"{rd:%Y年%m月%d日}発売")
        extra = (
            f"これは書籍（新刊）を紹介する投稿です。{when}。書名: {b.get('title', '')}\n"
            "本の内容（著者・章立て・書かれていること）は、書名と説明に書かれている範囲だけで書き、分からないことは"
            "推測で書かない（Amazon の定型文は無視）。発売日と、仏像好きがどんな人に向くかが伝わるように。"
            "紹介する価値の一言（" + (b.get("book_ai") or {}).get("reason", "") + "）は材料にしてよい。")
        text = write_post(client, usage, b, header=BOOK_POST_HEADER, extra=extra, fetch_body=False)
        b["book_post_attempts"] = b.get("book_post_attempts", 0) + 1
        if text:
            b["book_post_text"] = text
            made += 1
    return made


def main() -> int:
    data = load_news_data()
    books = [it for it in data["items"] if it.get("item_type") == "book"]
    todo = [b for b in books if (b.get("book_ai") or {}).get("version", 0) < BOOK_SCORE_VERSION]
    if not os.environ.get("ANTHROPIC_API_KEY"):
        if todo:
            print("::warning::ANTHROPIC_API_KEY が無いため書籍を採点できません", file=sys.stderr)
        return 0
    import anthropic

    client, usage = anthropic.Anthropic(), Usage()
    done = 0
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        payload = [{"id": b["id"], "title": b.get("title", ""),
                    "description": (b.get("description") or "")[:300]} for b in batch]
        res = call_json(client, usage, SYSTEM, json.dumps(payload, ensure_ascii=False, indent=1),
                        SCHEMA, effort="low")
        if not res:
            continue
        by_id = {b["id"]: b for b in batch}
        for r in res.get("results", []):
            b = by_id.get(r.get("id"))
            if b:
                b["book_ai"] = {"score": max(0, min(100, int(r["score"]))),
                                "reason": r["reason"], "version": BOOK_SCORE_VERSION}
                done += 1
    posts = write_book_posts(client, usage, books)
    if done or posts:
        save_news_data(data)
    print(f"::notice::書籍の採点 {done}件・紹介文 {posts}件・費用 約${usage.cost():.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
