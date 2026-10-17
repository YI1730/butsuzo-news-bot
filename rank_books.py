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


def main() -> int:
    data = load_news_data()
    books = [it for it in data["items"] if it.get("item_type") == "book"]
    todo = [b for b in books if (b.get("book_ai") or {}).get("version", 0) < BOOK_SCORE_VERSION]
    if not todo:
        print("書籍の採点: 新しい本なし")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
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
    save_news_data(data)
    print(f"::notice::書籍の採点 {done}件・費用 約${usage.cost():.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
