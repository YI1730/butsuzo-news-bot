"""書籍（Amazon）の「紹介する価値」を Claude で採点し、PWA の書籍一覧の並び順に使う。

news_hourly.yml から curate_news.py の後に実行される。未採点（または採点基準が古い）の書籍だけを
採点するので、新しい本が入ったとき以外は API を呼ばない。

結果は各書籍の "book_ai" に {score, reason, version} として保存する。
並び順（generate_dashboard.py）: 取り込み日（fetched_at）の新しい順。発売日はバッジで見せる。
  score < BOOK_HIDE_BELOW の本（仏像と無関係）は表示しない
紹介文（book_post_text）: 表示する本・雑誌すべてに作る（1 回の実行で最大 MAX_BOOK_POSTS_PER_RUN 件、
  取り込みの新しいものから）。X の文字数に収まるよう、コメントは全角 BOOK_COMMENT_CHARS 字以内で書かせる
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

# 雑誌（book_kind が magazine のもの。仏像特集号だけが入っている）
- 仏像・寺院・仏教美術が特集の中心の号は 70 点以上（仏像が主役なら 80 点以上）
- 特集の一部（第 2 特集・連載）が仏像なら 40〜60 点

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


MAX_BOOK_POSTS_PER_RUN = 15   # 費用を抑えるため 1 回の実行で作る上限。残りは次の実行で
MAX_BOOK_POST_ATTEMPTS = 3
BOOK_COMMENT_CHARS = 100      # 書名・見出し・リンクと合わせて X の 280 字に収めるための、コメントの目安


def write_book_posts(client, usage: Usage, books: list[dict]) -> int:
    """表示する書籍・雑誌（score が BOOK_HIDE_BELOW 以上）のうち紹介文が無いものに、X の紹介投稿文を作る。
    取り込みの新しいものから最大 MAX_BOOK_POSTS_PER_RUN 件。グッズは作らない。"""
    from datetime import datetime

    from curate_news import write_post
    from news_store import BOOK_HIDE_BELOW, JST_TZ, book_fetched_key, book_header, book_release_date

    today = datetime.now(JST_TZ).date()
    cands = [b for b in books
             if (b.get("book_ai") or {}).get("score", 50) >= BOOK_HIDE_BELOW
             and not b.get("book_post_text")
             and b.get("book_post_attempts", 0) < MAX_BOOK_POST_ATTEMPTS]
    cands.sort(key=book_fetched_key, reverse=True)
    made = 0
    for b in cands[:MAX_BOOK_POSTS_PER_RUN]:
        rd = book_release_date(b)
        if not rd:
            when = "発売日は不明"
        elif rd > today:
            when = f"{rd:%Y年%m月%d日}発売予定（予約受付中）"
        else:
            when = f"{rd:%Y年%m月%d日}発売"
        if b.get("book_kind") == "magazine":
            kind = (f"これは雑誌の仏像特集号を紹介する投稿です。{when}。誌名と号: {b.get('title', '')}。\n"
                    f"特集の内容: {b.get('description', '')}\n"
                    "書かれている特集の範囲だけで書き、載っていない寺や仏像を推測で足さない。")
        else:
            kind = (f"これは書籍を紹介する投稿です。{when}。書名: {b.get('title', '')}\n"
                    "本の内容（著者・章立て・書かれていること）は、書名と説明に書かれている範囲だけで書き、分からないことは"
                    "推測で書かない（Amazon の定型文は無視）。")
        extra = (kind + "仏像好きがどんな人に向くかが伝わるように。"
                 "紹介する価値の一言（" + (b.get("book_ai") or {}).get("reason", "") + "）は材料にしてよい。\n"
                 f"書名と見出しはシステムが後ろに付けるので、コメントは全角 {BOOK_COMMENT_CHARS} 字以内、1〜2 文で書く。"
                 "説明文の丸写しや、長い引用はしない。")
        text = write_post(client, usage, b, header=book_header(b), extra=extra, fetch_body=False,
                          max_chars=BOOK_COMMENT_CHARS)
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
