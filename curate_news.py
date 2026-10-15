"""Google ニュースを Claude で採点し、配信すべき記事の X 投稿文を作って通知する。

news_hourly.yml から post_news.py の直後に毎時実行される。

1. 採点: 未採点のニュース（直近 SCORE_WINDOW_HOURS 時間）を、タイトル・要約・配信元で
   0〜100 点に採点する。複数件をまとめて 1 回の API 呼び出しで処理する。
   - 仏像と無関係な記事（「顔面重要文化財」等の比喩、近代建築の重文など）は低得点になり、
     ダッシュボードのニュースタブから外れる（NOISE_THRESHOLD 未満）
   - 同じ出来事の記事が複数あれば、最も情報量の多い 1 件以外を重複扱いにする
2. 投稿文: CANDIDATE_THRESHOLD 以上の記事は本文を読み、仏像リンクの投稿文を作る
3. 通知: 新しく配信候補になった記事を ntfy でスマホに通知する（NTFY_TOPIC 設定時のみ）

採点結果は各記事の "ai" フィールドに保存する（docs/data/news.json）。
ANTHROPIC_API_KEY が無い場合は何もせず正常終了する（ニュース取得自体は止めない）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from news_store import CANDIDATE_THRESHOLD, NOISE_THRESHOLD
from post_news import JST, RESOLVE_USER_AGENT, load_news_data, save_news_data

MODEL = "claude-opus-5-5"
# 価格（USD / 100万トークン）。実行ごとの費用をログに出すために使う
PRICE_INPUT, PRICE_OUTPUT, PRICE_CACHE_READ, PRICE_CACHE_WRITE = 4.00, 20.00, 0.20, 5.00

SCORE_WINDOW_HOURS = 48    # これより前に取り込んだ記事は採点しない（速報として扱わない）
SCORE_BATCH_SIZE = 20      # 1 回の API 呼び出しで採点する件数
MAX_SCORE_PER_RUN = 80
MAX_POSTS_PER_RUN = 8
ARTICLE_BODY_CHARS = 4000  # 投稿文作成時に渡す本文の上限文字数

PWA_URL = "https://yi1730.github.io/butsuzo-news-bot/"
TARGET_SOURCES = {"google_news"}

CATEGORIES = [
    "秘仏・御開帳・特別公開", "展覧会", "発見・研究・新指定", "修理・保存",
    "事件・被害", "寺院の行事", "その他の仏像関連", "無関係",
]

SCORE_SYSTEM = """あなたは X アカウント「仏像リンク」のニュース編集者です。
仏像リンクは仏像好きが集まるコミュニティで、フォロワーは寺院や博物館へ実際に仏像を見に行く人たちです。
渡されたニュース記事の一覧を、仏像リンクが「いま配信すべきか」で 0〜100 点に採点してください。

# 高く評価する記事
- 秘仏の御開帳・特別公開（特に「◯十年ぶり」「初公開」、公開期間が近いもの）
- 仏像が主役の展覧会の開幕・告知
- 仏像の新発見、新たな国宝・重要文化財指定、研究による新事実
- 仏像の修理完了・お披露目
- 仏像の盗難・損傷・被災などの事件（読者が知っておくべき情報）
- 仏像に関わる寺院の行事

# 低く評価する記事
- 仏像と無関係なもの: 「顔面重要文化財」「菩薩のような美しさ」などの比喩、芸能人・グラビア、
  近代建築や庭園など仏像以外の文化財、人名・地名・作品名にたまたま語が入っているもの、ゲーム
- 寺社の観光・グルメ・行事でも仏像に触れていないもの
- 商品やサービスの宣伝が主目的のもの

# 点数の目安
- 90〜100: 速報価値が非常に高い（数十年ぶりの開帳、重大な新発見、重大な事件）
- 70〜89: 配信すべき
- 40〜69: 仏像に関係はあるが優先度は低い
- 0〜39: 無関係・ノイズ

# 重複
- 一覧の中に同じ出来事を報じた記事が複数ある場合、最も情報量が多い 1 件だけを通常どおり採点し、
  残りは duplicate を true にして点数を 20 点以上下げてください。
- 「配信済み・配信候補の記事」に同じ出来事がある場合も duplicate を true にしてください。

reason には判断理由を日本語 40 字以内で書いてください。"""

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "duplicate": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "score", "category", "duplicate", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

POST_SYSTEM = """あなたは X アカウント「仏像リンク」の中の人です。仏像好きのフォロワーに向けて、
ニュース記事を紹介する X の投稿文を 1 つ書いてください。

# 書き方
- 1 行目は【仏像速報】で始め、続けて何が起きたかを一言で
- 「何が（仏像名）」「どこで（寺・博物館名と都道府県）」「いつ（公開期間・日付）」を、記事に書かれている範囲で必ず入れる
- 見どころを 1 つだけ具体的に（像の特徴、◯十年ぶり、など）
- 最後に仏像好きの目線のひとことを短く。押しつけや誇張はしない
- 末尾にハッシュタグを 2〜3 個（#仏像 と、寺名や仏像名など記事に合うもの）
- 絵文字は使っても 1〜2 個
- URL は書かない（システムが末尾に付ける）
- 本文全体（ハッシュタグ込み）で全角 115 字以内

# 守ること
- 記事に書かれていない事実（日付、所在地、像の名前、指定区分など）を推測で書かない
- 記事の文章をそのまま長く引用しない。自分の言葉で要約する"""

POST_SCHEMA = {
    "type": "object",
    "properties": {"post_text": {"type": "string"}},
    "required": ["post_text"],
    "additionalProperties": False,
}

# X の文字数制限（重み付きで 280）。URL は長さにかかわらず 23 として数える。
X_LIMIT = 280
X_URL_WEIGHT = 23


def x_weighted_length(text: str) -> int:
    """X（twitter-text v3）の重み付き文字数。ASCII 等は 1、日本語や絵文字は 2。"""
    n = 0
    for ch in text:
        cp = ord(ch)
        if cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247:
            n += 1
        else:
            n += 2
    return n


class Usage:
    def __init__(self) -> None:
        self.input = self.output = self.cache_read = self.cache_write = 0

    def add(self, u) -> None:
        self.input += u.input_tokens or 0
        self.output += u.output_tokens or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0

    def cost(self) -> float:
        return (self.input * PRICE_INPUT + self.output * PRICE_OUTPUT
                + self.cache_read * PRICE_CACHE_READ + self.cache_write * PRICE_CACHE_WRITE) / 1e6


def call_json(client, usage: Usage, system: str, user: str, schema: dict, effort: str) -> dict | None:
    """構造化出力で 1 回呼び出し、JSON を返す。拒否や形式不備のときは None。"""
    import anthropic

    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            # 安全分類で拒否されたときに API 側で別モデルに切り替えて続行する
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.RateLimitError as e:
        print(f"::warning::Claude API レート制限: {e.message}", file=sys.stderr)
        return None
    except anthropic.APIStatusError as e:
        print(f"::warning::Claude API エラー ({e.status_code}): {e.message}", file=sys.stderr)
        return None
    except anthropic.APIConnectionError as e:
        print(f"::warning::Claude API 接続エラー: {e}", file=sys.stderr)
        return None

    usage.add(resp.usage)
    if resp.stop_reason == "refusal":
        print("::warning::Claude が応答を拒否しました", file=sys.stderr)
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"::warning::JSON 解析に失敗 (stop_reason={resp.stop_reason})", file=sys.stderr)
        return None


def domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except Exception:
        return ""


def fetch_article_text(url: str) -> str:
    """記事本文を段落単位で抽出する（失敗時は空文字）。"""
    try:
        resp = requests.get(url, headers={"User-Agent": RESOLVE_USER_AGENT}, timeout=10)
        soup = BeautifulSoup(resp.content, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        root = soup.find("article") or soup.find("main") or soup.body or soup
        paras = [" ".join(p.get_text(" ", strip=True).split()) for p in root.find_all("p")]
        text = "\n".join(p for p in paras if len(p) >= 20)
        return text[:ARTICLE_BODY_CHARS]
    except Exception:
        return ""


def score_items(client, usage: Usage, items: list[dict], recent_candidates: list[dict]) -> int:
    """items を SCORE_BATCH_SIZE 件ずつ採点し、各記事の "ai" に書き込む。採点件数を返す。"""
    known = list(recent_candidates)  # 重複判定用。採点が進むたびに新しい候補を足していく
    scored = 0
    for i in range(0, len(items), SCORE_BATCH_SIZE):
        batch = items[i:i + SCORE_BATCH_SIZE]
        # 前のまとまりで候補になった記事も渡す（まとまりをまたいだ重複を見逃さないため）
        context = "\n".join(f"- {c['title']}" for c in known) or "（なし）"
        articles = [
            {
                "id": it["id"],
                "title": it.get("title", ""),
                "description": it.get("description", ""),
                "source": domain(it.get("url", "")),
                "published": (it.get("published_at") or "")[:16],
            }
            for it in batch
        ]
        user = (
            "## 配信済み・配信候補の記事（直近）\n" + context
            + "\n\n## 採点する記事\n" + json.dumps(articles, ensure_ascii=False, indent=1)
        )
        data = call_json(client, usage, SCORE_SYSTEM, user, SCORE_SCHEMA, effort="low")
        if not data:
            continue
        by_id = {it["id"]: it for it in batch}
        now = datetime.now(JST).isoformat(timespec="seconds")
        for r in data.get("results", []):
            it = by_id.get(r.get("id"))
            if not it:
                continue
            it["ai"] = {
                "score": max(0, min(100, int(r["score"]))),
                "category": r["category"],
                "duplicate": bool(r["duplicate"]),
                "reason": r["reason"],
                "model": MODEL,
                "scored_at": now,
            }
            scored += 1
            if it["ai"]["score"] >= CANDIDATE_THRESHOLD and not it["ai"]["duplicate"]:
                known.append(it)
    return scored


def write_post(client, usage: Usage, item: dict) -> str | None:
    body = fetch_article_text(item["url"]) or item.get("description", "")
    user = (
        f"記事タイトル: {item.get('title', '')}\n"
        f"配信元: {domain(item['url'])}\n"
        f"公開日時: {(item.get('published_at') or '')[:16]}\n\n"
        f"記事本文:\n{body or '（本文を取得できませんでした。タイトルの範囲で書いてください）'}"
    )
    for _ in range(2):
        data = call_json(client, usage, POST_SYSTEM, user, POST_SCHEMA, effort="medium")
        if not data:
            return None
        text = data["post_text"].strip()
        total = x_weighted_length(text) + 1 + X_URL_WEIGHT  # 改行 + URL
        if total <= X_LIMIT:
            return text
        user += (
            f"\n\n（前回の案は X の文字数制限を超えました: {total}/{X_LIMIT}。"
            "もっと短く、全角 100 字以内で書き直してください）"
        )
    return None


def notify(topic: str, item: dict) -> None:
    ai = item["ai"]
    try:
        requests.post(
            "https://ntfy.sh/",
            json={
                "topic": topic,
                "title": f"⭐ 配信候補 {ai['score']}点（{ai['category']}）",
                "message": ai.get("post_text") or item.get("title", ""),
                "click": PWA_URL,
                "tags": ["star"],
                "priority": 5 if ai["score"] >= 90 else 4,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"::warning::通知の送信に失敗: {e}", file=sys.stderr)


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("::warning::ANTHROPIC_API_KEY が未設定のため AI 採点をスキップ", file=sys.stderr)
        return 0
    import anthropic

    client = anthropic.Anthropic()
    usage = Usage()
    data = load_news_data()
    now = datetime.now(JST)
    window = now - timedelta(hours=SCORE_WINDOW_HOURS)

    def recent(it: dict) -> bool:
        try:
            return datetime.fromisoformat(it.get("fetched_at", "")) >= window
        except ValueError:
            return False

    news = [it for it in data["items"] if it.get("source") in TARGET_SOURCES and recent(it)]
    unscored = [it for it in news if "ai" not in it][:MAX_SCORE_PER_RUN]
    recent_candidates = [
        it for it in news
        if it.get("ai", {}).get("score", 0) >= CANDIDATE_THRESHOLD and not it["ai"].get("duplicate")
    ]
    print(f"採点対象: {len(unscored)}件（直近{SCORE_WINDOW_HOURS}時間のニュース {len(news)}件中）")

    scored = score_items(client, usage, unscored, recent_candidates) if unscored else 0

    # 投稿文が未作成の配信候補（新しい順）
    candidates = [
        it for it in news
        if it.get("ai", {}).get("score", 0) >= CANDIDATE_THRESHOLD
        and not it["ai"].get("duplicate")
        and "post_text" not in it["ai"]
    ][:MAX_POSTS_PER_RUN]
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    written = 0
    for it in candidates:
        text = write_post(client, usage, it)
        it["ai"]["post_text"] = text or ""
        if text:
            written += 1
            print(f"⭐ {it['ai']['score']}点 {it['title'][:40]}\n   {text}")
        if topic and not it["ai"].get("notified"):
            notify(topic, it)
            it["ai"]["notified"] = True

    save_news_data(data)
    noise = sum(1 for it in news if it.get("ai", {}).get("score", 100) < NOISE_THRESHOLD)
    print(
        f"採点 {scored}件 / 配信候補の投稿文 {written}件 / ノイズ判定（直近）{noise}件\n"
        f"トークン: 入力 {usage.input:,} / 出力 {usage.output:,} / "
        f"キャッシュ読込 {usage.cache_read:,} / キャッシュ書込 {usage.cache_write:,} "
        f"→ 約 ${usage.cost():.4f}"
    )
    print(f"::notice::AI採点 {scored}件・投稿文 {written}件・費用 約${usage.cost():.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
