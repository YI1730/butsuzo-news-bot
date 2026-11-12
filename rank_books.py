"""書籍（Amazon）の「紹介する価値」を Claude で採点し、PWA の書籍一覧の並び順に使う。

news_hourly.yml から curate_news.py の後に実行される。未採点（または採点基準が古い）の書籍だけを
採点するので、新しい本が入ったとき以外は API を呼ばない。

結果は各書籍の "book_ai" に {score, reason, version} として保存する。
並び順（generate_dashboard.py）: 発売日の新しい順（発売日不明は最後）。
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


MAX_BOOK_POSTS_PER_RUN = 15   # 費用を抑えるため 1 回の実行で作る上限（仏像リンク側と j 側の紹介文の合計）。残りは次の実行で
MAX_BOOK_POST_ATTEMPTS = 3
BOOK_COMMENT_CHARS = 100      # 書名・見出し・タグ・リンクと合わせて X の 280 字に収めるための、コメントの目安

# 2つめのアカウント（美しい日本の仏像）の紹介文。仏像を淡々と紹介するアカウントなので、勧めや感想を入れず、
# どんな本・グッズかだけを書く。投稿は news_store.j_book_post の目録の形（見出し → 『書名』 → 著者・発売日 → 内容 → タグ）
J_FEELING_WORDS = ("おすすめ", "オススメ", "楽しみ", "気になる", "向き", "人に", "眺め", "味わ", "ぜひ", "！", "!", "？", "?",
                   "美しい", "見応え", "魅力", "静かに")
J_POST_FORMAT = 2   # 上げると、2つめのアカウント側の内容の行を作り直す
J_POST_SYSTEM = """X アカウント「美しい日本の仏像」で書籍・雑誌・グッズを紹介する投稿の、「内容：」の行だけを書いてください。
投稿はシステムが次の形に組み立てます。見出し・書名・著者・発売日・ハッシュタグ・URL は書かないでください。
  【仏像の本】
  『書名』
  著者：◯◯
  発売日：◯年◯月◯日
  内容：（あなたが書く行）

# 書くこと
- どんな本・グッズかを、事実だけで説明する。扱う主題（時代・地域・寺・像・テーマ）、形式（新書・図録・写真集・
  カレンダー・特集号など）、載っているもの。書名と説明文に書かれている範囲だけ。分からないことは書かない
- 体言止めの説明文 1 文（長くても 2 文）。全角 60 字以内。例:「ガンダーラ美術の成り立ちを、ギリシア彫刻との関わりから解説する新書。」

# 書かないこと
- 勧め・評価・感想・気持ち（「おすすめ」「〜な人に」「楽しみ」「気になる」「美しい」「見応え」「眺めたい」など）
- 読者への呼びかけ、問いかけ、一人称、「！」、絵文字、ハッシュタグ
- 像の姿の描写や情景描写（「衣のひだ」「静かに向き合う」のような文学的な書き方）
- 「仏様」「仏さま」「仏さん」「癒やし」「感動」「圧倒」「驚き」「最高」
- 書名の言い換えだけの文。Amazon の定型文（「アマゾンならポイント還元本が多数」など）
- 運慶・快慶などの作者の帰属は、説明文どおりに（「伝」があれば付ける）"""


def _book_extra(b: dict, today, own: bool) -> str:
    from news_store import book_release_date

    rd = book_release_date(b)
    if not rd:
        when = "発売日は不明"
    elif rd > today:
        when = f"{rd:%Y年%m月%d日}発売予定（予約受付中）"
    else:
        when = f"{rd:%Y年%m月%d日}発売"
    mag = b.get("book_kind") == "magazine"
    if not own:   # j 側: 材料だけ渡す（書き方は J_POST_SYSTEM）
        return (f"{'雑誌の特集号' if mag else '書籍・グッズ'}: {b.get('title', '')}（{when}）\n"
                f"説明: {b.get('description', '')}\n"
                "「内容：」の行だけを、全角 60 字以内の体言止め 1 文で書く。")
    if mag:
        kind = (f"これは雑誌の仏像特集号を紹介する投稿です。{when}。誌名と号: {b.get('title', '')}。\n"
                f"特集の内容: {b.get('description', '')}\n"
                "書かれている特集の範囲だけで書き、載っていない寺や仏像を推測で足さない。")
    else:
        kind = (f"これは書籍を紹介する投稿です。{when}。書名: {b.get('title', '')}\n"
                "本の内容（著者・章立て・書かれていること）は、書名と説明に書かれている範囲だけで書き、分からないことは"
                "推測で書かない（Amazon の定型文は無視）。")
    return (kind + "仏像好きがどんな人に向くかが伝わるように。"
            "紹介する価値の一言（" + (b.get("book_ai") or {}).get("reason", "") + "）は材料にしてよい。\n"
            f"書名と見出しはシステムが後ろに付けるので、コメントは全角 {BOOK_COMMENT_CHARS} 字以内、1〜2 文で書く。"
            "説明文の丸写しや、長い引用はしない。")


def write_book_posts(client, usage: Usage, books: list[dict]) -> int:
    """表示する書籍・雑誌（score が BOOK_HIDE_BELOW 以上）に、X の紹介投稿文を作る。
    仏像リンク側（book_post_text）と、2つめのアカウント側（j_post_text）を、発売日の新しいものから 1 冊ずつ順に。
    両方の合計が MAX_BOOK_POSTS_PER_RUN 件に達したら止める。グッズは作らない。
    2 つの文章が似すぎたとき（15 字以上の同じ並び、バイグラム類似度が高い）は作り直させる。"""
    from datetime import datetime

    from curate_news import write_post
    from news_store import (BOOK_HIDE_BELOW, JST_TZ, book_sort_key, book_hashtags, book_header,
                            too_similar)

    today = datetime.now(JST_TZ).date()
    shown = [b for b in books if (b.get("book_ai") or {}).get("score", 50) >= BOOK_HIDE_BELOW]
    shown.sort(key=book_sort_key, reverse=True)
    made = calls = 0
    for b in shown:
        for own in (True, False):
            field, tries = ("book_post_text", "book_post_attempts") if own else ("j_post_text", "j_post_attempts")
            if not own and b.get("j_post_format", 1) < J_POST_FORMAT:
                # 2つめのアカウント側は形を変えたので、古い文章は作り直す
                b.pop("j_post_text", None)
                b[tries] = 0
            if b.get(field) or b.get(tries, 0) >= MAX_BOOK_POST_ATTEMPTS:
                continue
            if calls >= MAX_BOOK_POSTS_PER_RUN:
                return made
            calls += 1
            account = "main" if own else "j"
            other = b.get("j_post_text" if own else "book_post_text", "")
            def reject(t, o=other, own=own):
                if o and too_similar(t, o):
                    return "もう一方のアカウントの文章と似すぎています。視点・言葉・書き出しを変える"
                hits = [] if own else [w for w in J_FEELING_WORDS if w in t]
                if hits:
                    return ("「" + "」「".join(hits) + "」は勧めや感想の言い方です。どんな本・グッズかの事実だけを、"
                            "体言止めの 1 文で書いてください")
                return None
            text = write_post(client, usage, b, header=book_header(b, account),
                              extra=_book_extra(b, today, own), fetch_body=False,
                              max_chars=BOOK_COMMENT_CHARS if own else 60,
                              system=None if own else J_POST_SYSTEM,
                              tail_extra="\n" + book_hashtags(b, account), reject=reject)
            b[tries] = b.get(tries, 0) + 1
            if not own:
                b["j_post_format"] = J_POST_FORMAT
            if text:
                b[field] = text
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
